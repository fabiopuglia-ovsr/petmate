"""Shared H.264 encoder and per-viewer WebRTC video tracks (INTERFACES.md §4.1).

The Pi 5 has no hardware H.264 encoder, so the selected camera is encoded
**once** with libx264 (Constrained Baseline, ultrafast, zerolatency, 1.5 Mb/s,
1280x720@25, GOP 2 s, SPS/PPS repeated before every IDR) and the resulting
``av.Packet`` objects are fanned out to:

* every viewer's :class:`EncodedVideoTrack` — aiortc's RTCRtpSender accepts
  pre-encoded packets (``MediaStreamTrack.recv`` may return ``av.Packet``) and
  only packetizes them, so N viewers cost one encode;
* the clip recorder ring buffer.

Switching camera only changes which camera feeds the encoder (same resolution
and codec), followed by a forced IDR — no renegotiation needed.

Because aiortc does not forward PLI/FIR to a packet-producing track, keyframes
are forced by pm-core: on viewer join, on camera switch, on viewer queue
overflow, and every GOP (2 s).
"""

from __future__ import annotations

import asyncio
import fractions
import queue
import threading
import time
from typing import Callable, Optional

from .camera import MainFrame
from .log import get_logger, kv

log = get_logger(__name__)

VIDEO_CLOCK = 90000
TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK)

PacketSink = Callable[["object"], None]  # receives av.Packet (from encoder thread)


class SharedH264Encoder:
    def __init__(self, width: int, height: int, fps: int, bitrate: int, threads: int = 3):
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.threads = max(1, int(threads))
        self._q: "queue.Queue[MainFrame]" = queue.Queue(maxsize=2)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._force_kf = threading.Event()
        self._sinks: dict[int, PacketSink] = {}
        self._sinks_lock = threading.Lock()
        self._next_sink = 1
        self._codec = None
        self._reset_codec = False
        self._t0: Optional[float] = None
        self._last_pts = -1
        self._taps: list[PacketSink] = []  # passive consumers (don't keep encoder active)
        self.on_active_change: Optional[Callable[[bool], None]] = None
        self.stats = {"frames": 0, "dropped": 0, "enc_ms": 0.0}

    # ----------------------------------------------------------- consumers
    def add_sink(self, sink: PacketSink) -> int:
        with self._sinks_lock:
            sid = self._next_sink
            self._next_sink += 1
            was_empty = not self._sinks
            self._sinks[sid] = sink
        if was_empty:
            self._reset_codec = True  # fresh codec + pts origin for a new streaming period
            self._t0 = None
            self._last_pts = -1
        self.request_keyframe()
        if was_empty and self.on_active_change:
            self.on_active_change(True)
        return sid

    def remove_sink(self, sid: int) -> None:
        with self._sinks_lock:
            self._sinks.pop(sid, None)
            empty = not self._sinks
        if empty:
            if self.on_active_change:
                self.on_active_change(False)

    def add_tap(self, tap: PacketSink) -> None:
        """Passive consumer: receives packets only while the encoder runs anyway."""
        self._taps.append(tap)

    @property
    def active(self) -> bool:
        return bool(self._sinks)

    def request_keyframe(self) -> None:
        self._force_kf.set()

    # ----------------------------------------------------------- frames in
    def push(self, f: MainFrame) -> None:
        """Called from the camera thread; never blocks (drops oldest)."""
        if not self._sinks:
            return
        try:
            self._q.put_nowait(f)
        except queue.Full:
            try:
                self._q.get_nowait()
                self.stats["dropped"] += 1
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(f)
            except queue.Full:
                pass

    # ----------------------------------------------------------- thread
    def start(self) -> None:
        self._running = True
        self._reset_codec = False
        self._thread = threading.Thread(target=self._loop, name="h264-enc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _open_codec(self):
        import av

        cc = av.CodecContext.create("libx264", "w")
        cc.width = self.width
        cc.height = self.height
        cc.pix_fmt = "yuv420p"
        cc.time_base = TIME_BASE
        cc.framerate = fractions.Fraction(self.fps, 1)
        cc.bit_rate = self.bitrate
        cc.profile = "Baseline"  # no B-frames/CABAC → Constrained Baseline
        gop = self.fps * 2
        kbps = self.bitrate // 1000
        cc.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "level": "31",
            "threads": str(self.threads),
            "x264-params": (
                f"keyint={gop}:min-keyint={gop}:scenecut=0:repeat-headers=1:"
                f"vbv-maxrate={int(kbps * 1.2)}:vbv-bufsize={kbps // 2}:nal-hrd=none"
            ),
        }
        return cc

    def _loop(self) -> None:
        import av

        while self._running:
            try:
                f = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._reset_codec:
                self._reset_codec = False
                self._codec = None
            if not self._sinks:
                continue
            try:
                # The stream follows the source resolution (e.g. a UVC webcam that fell
                # back to 640x480): new codec → new SPS/PPS + IDR, decoders adapt.
                fw, fh = f.width & ~1, f.height & ~1
                if (fw, fh) != (self.width, self.height):
                    log.info("encoder resolution change", extra=kv(
                        old=f"{self.width}x{self.height}", new=f"{fw}x{fh}"))
                    self.width, self.height = fw, fh
                    self._codec = None
                if self._codec is None:
                    self._codec = self._open_codec()
                    if self._t0 is None or self._last_pts < 0:
                        self._t0 = f.t
                        self._last_pts = -1
                    self._force_kf.set()
                t0 = time.perf_counter()
                vf = self._to_video_frame(f, av)
                pts = int(round((f.t - (self._t0 if self._t0 is not None else f.t)) * VIDEO_CLOCK))
                if pts <= self._last_pts:
                    pts = self._last_pts + 1
                self._last_pts = pts
                vf.pts = pts
                vf.time_base = TIME_BASE
                if self._force_kf.is_set():
                    self._force_kf.clear()
                    vf.pict_type = av.video.frame.PictureType.I
                else:
                    vf.pict_type = av.video.frame.PictureType.NONE
                packets = self._codec.encode(vf)
                self.stats["frames"] += 1
                self.stats["enc_ms"] = 0.9 * self.stats["enc_ms"] + 0.1 * (time.perf_counter() - t0) * 1000
                with self._sinks_lock:
                    sinks = list(self._sinks.values()) + list(self._taps)
                for p in packets:
                    p.time_base = TIME_BASE
                    for s in sinks:
                        try:
                            s(p)
                        except Exception:  # noqa: BLE001
                            log.exception("packet sink failed")
            except Exception:  # noqa: BLE001
                log.exception("encoder error; resetting codec")
                self._codec = None

    def _to_video_frame(self, f: MainFrame, av):
        """MainFrame → yuv420p ``av.VideoFrame`` at the encoder size (≤ 1 swscale pass)."""
        import numpy as np

        w, h = self.width, self.height
        if f.vframe is not None:
            src = f.vframe
            if src.format.name == "yuv420p" and src.width == w and src.height == h:
                # e.g. lavfi/yuv420p sources: no conversion at all. A fresh frame object
                # is not needed: pts/pict_type are overwritten by the caller.
                return src
            # MJPEG (yuvj422p, full range) / YUYV → yuv420p limited range, one pass.
            return src.reformat(width=w, height=h, format="yuv420p")
        arr = f.yuv420
        if arr.shape != (h * 3 // 2, w):
            arr = np.ascontiguousarray(arr[: h * 3 // 2, : w])
        return av.VideoFrame.from_ndarray(arr, format="yuv420p")


class EncodedVideoTrack:
    """aiortc video track yielding pre-encoded H.264 packets from the shared encoder.

    Implemented as a factory to keep aiortc an import-time-optional dependency.
    """

    def __new__(cls, encoder: SharedH264Encoder, loop: asyncio.AbstractEventLoop, max_queue: int = 30):
        from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

        class _Track(MediaStreamTrack):
            kind = "video"

            def __init__(self) -> None:
                super().__init__()
                self._q: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
                self._need_kf = True
                self._last_kf_req = 0.0
                self._sid = encoder.add_sink(self._from_thread)

            def _from_thread(self, packet) -> None:
                loop.call_soon_threadsafe(self._enqueue, packet)

            def _enqueue(self, packet) -> None:
                if self.readyState != "live":
                    return
                if self._need_kf:
                    if not packet.is_keyframe:
                        return
                    self._need_kf = False
                if self._q.full():
                    # viewer too slow: flush, wait for next IDR
                    while not self._q.empty():
                        self._q.get_nowait()
                    self._need_kf = True
                    now = time.monotonic()
                    if now - self._last_kf_req > 1.0:
                        self._last_kf_req = now
                        encoder.request_keyframe()
                    return
                self._q.put_nowait(packet)

            async def recv(self):
                if self.readyState != "live":
                    raise MediaStreamError
                p = await self._q.get()
                if p is None:
                    raise MediaStreamError
                return p

            def stop(self) -> None:
                if self.readyState == "live":
                    encoder.remove_sink(self._sid)
                    try:
                        self._q.put_nowait(None)
                    except asyncio.QueueFull:
                        pass
                super().stop()

        return _Track()
