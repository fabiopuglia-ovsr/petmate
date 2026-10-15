"""Clip recorder (ring buffer + MP4 mux) and JPEG snapshots.

* While the shared encoder runs (someone is watching), packets go to a ring
  buffer of ``pre_roll_s`` seconds (passive tap), so a clip can include the
  moments before the request.
* ``record(seconds)`` holds the encoder active (as a sink) for the duration,
  so it also works when nobody is watching. The clip starts at the oldest
  keyframe in the pre-roll (or the forced IDR), total length capped at
  ``seconds``.
* Packets are muxed into MP4 with PyAV without re-encoding (Annex-B H.264 →
  mp4 via ffmpeg's bitstream handling), written to ``/var/lib/petmate/clips``
  then uploaded with ``POST /devices/{id}/clips`` (multipart ``file`` + ``meta``).
"""

from __future__ import annotations

import asyncio
import collections
import fractions
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .camera import CameraManager, MainFrame
from .log import get_logger, kv
from .video import TIME_BASE, VIDEO_CLOCK, SharedH264Encoder

log = get_logger(__name__)


def mux_mp4(packets: list, path: Path, width: int, height: int, fps: int) -> float:
    """Write H.264 ``av.Packet`` list (90 kHz pts) to MP4; returns duration in s."""
    import av

    if not packets:
        raise ValueError("no packets")
    t0 = packets[0].pts
    tmp = path.with_suffix(".part")
    out = av.open(str(tmp), "w", format="mp4", options={"movflags": "+faststart"})
    try:
        st = out.add_stream("h264", rate=fps)
        st.width, st.height = width, height
        st.time_base = TIME_BASE
        last = -1
        for p in packets:
            q = av.Packet(bytes(p))
            pts = max(last + 1, p.pts - t0)
            last = pts
            q.pts = q.dts = pts
            q.time_base = TIME_BASE
            q.is_keyframe = p.is_keyframe
            q.stream = st
            out.mux(q)
    finally:
        out.close()
    tmp.replace(path)
    return (last + VIDEO_CLOCK / fps) / VIDEO_CLOCK


def encode_jpeg(frame: MainFrame, quality: int = 85) -> bytes:
    """I420 frame → JPEG bytes using PyAV's mjpeg encoder."""
    import av

    if frame.vframe is not None:
        vf = frame.vframe.reformat(width=frame.width & ~1, height=frame.height & ~1, format="yuvj420p")
    else:
        vf = av.VideoFrame.from_ndarray(frame.yuv420[: frame.height * 3 // 2, : frame.width], format="yuv420p")
        vf = vf.reformat(format="yuvj420p")
    cc = av.CodecContext.create("mjpeg", "w")
    cc.width, cc.height = vf.width, vf.height
    cc.pix_fmt = "yuvj420p"
    cc.time_base = fractions.Fraction(1, 25)
    # map quality 1..100 → mjpeg qscale 31..2
    q = int(round(31 - (max(1, min(100, quality)) - 1) * 29 / 99))
    cc.qmin = cc.qmax = q
    data = b"".join(bytes(p) for p in cc.encode(vf))
    data += b"".join(bytes(p) for p in cc.encode(None))
    return data


class ClipRecorder:
    def __init__(
        self,
        encoder: SharedH264Encoder,
        clips_dir: Path,
        uploader: Optional[Callable[[Path, dict], Awaitable[Optional[str]]]],
        pre_roll_s: float = 5.0,
        max_seconds: float = 60.0,
        keep_local: int = 20,
        current_camera: Callable[[], str] = lambda: "front",
    ):
        self.encoder = encoder
        self.clips_dir = Path(clips_dir)
        self.uploader = uploader
        self.pre_roll_s = pre_roll_s
        self.max_seconds = max_seconds
        self.keep_local = keep_local
        self.current_camera = current_camera
        self._ring: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._active: list[list] = []
        self._busy = asyncio.Lock()
        encoder.add_tap(self._on_packet)

    def _on_packet(self, p: Any) -> None:
        with self._lock:
            self._ring.append(p)
            horizon = p.pts - int(self.pre_roll_s * VIDEO_CLOCK)
            while self._ring and self._ring[0].pts < horizon:
                self._ring.popleft()
            # drop a stale ring if the stream restarted (pts went backwards)
            if len(self._ring) > 1 and self._ring[0].pts > p.pts:
                self._ring.clear()
                self._ring.append(p)
            for buf in self._active:
                buf.append(p)

    async def record(self, seconds: float, reason: str = "manual") -> dict:
        seconds = max(1.0, min(float(seconds), self.max_seconds))
        if self._busy.locked():
            raise RuntimeError("a clip is already being recorded")
        async with self._busy:
            clip_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            cam = self.current_camera()
            started = time.time()
            with self._lock:
                pre = list(self._ring)
                # start from the oldest keyframe of the pre-roll
                ks = next((i for i, p in enumerate(pre) if p.is_keyframe), None)
                pre = pre[ks:] if ks is not None else []
                buf: list = list(pre)
                self._active.append(buf)
            pre_s = (pre[-1].pts - pre[0].pts) / VIDEO_CLOCK if len(pre) > 1 else 0.0
            sid = self.encoder.add_sink(lambda p: None)  # keep encoder running
            try:
                await asyncio.sleep(max(0.5, seconds - pre_s))
            finally:
                self.encoder.remove_sink(sid)
                with self._lock:
                    self._active.remove(buf)
            # ensure it starts with a keyframe
            ks = next((i for i, p in enumerate(buf) if p.is_keyframe), None)
            if ks is None:
                raise RuntimeError("no video available (camera not delivering frames)")
            buf = buf[ks:]
            self.clips_dir.mkdir(parents=True, exist_ok=True)
            path = self.clips_dir / f"{clip_id}.mp4"
            loop = asyncio.get_running_loop()
            dur = await loop.run_in_executor(
                None, mux_mp4, buf, path, self.encoder.width, self.encoder.height, self.encoder.fps
            )
            meta = {"ts": started, "duration_s": round(dur, 2), "camera": cam, "reason": reason, "local_id": clip_id}
            log.info("clip recorded", extra=kv(path=str(path), **meta))
            self._prune()
            if self.uploader is not None:
                asyncio.ensure_future(self._upload(path, meta))
            return {"clip": clip_id, "duration_s": meta["duration_s"]}

    async def _upload(self, path: Path, meta: dict) -> None:
        delay = 5.0
        for attempt in range(6):
            try:
                assert self.uploader is not None
                cid = await self.uploader(path, meta)
                log.info("clip uploaded", extra=kv(path=str(path), clip_id=cid))
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("clip upload failed", extra=kv(path=str(path), attempt=attempt, error=str(e)))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    def _prune(self) -> None:
        files = sorted(self.clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        for p in files[: max(0, len(files) - self.keep_local)]:
            try:
                p.unlink()
            except OSError:
                pass


async def snapshot(cameras: CameraManager, cam: str, quality: int = 85, timeout_s: float = 5.0) -> bytes:
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def cb(f: MainFrame) -> None:
        # copy: the camera buffer is released after the callback
        f2 = f.copy()
        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(f2))

    cameras.grab_main(cam, cb)
    frame = await asyncio.wait_for(fut, timeout_s)
    return await loop.run_in_executor(None, encode_jpeg, frame, quality)
