"""Audio I/O on the USB mic array (XVF3800-class, hardware AEC).

* Capture: one ALSA capture (``capture_device``) shared by all viewers via
  aiortc ``MediaRelay`` → Opus (encoded per peer by aiortc).
* Playback: incoming WebRTC audio tracks (one per viewer) are resampled to
  s16 mono ``sample_rate`` and *mixed* into a single ALSA playback stream
  (``playback_device``) by :class:`SpeakerMixer`. The XVF3800 uses the
  playback stream as AEC reference, so all speaker audio must go through this
  device.
* Volume: ``amixer -c <card> sset <control> <pct>%``.

ALSA playback is done through a PyAV ``alsa`` output container when
available; ``aplay`` (alsa-utils) is the fallback writer.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from typing import Any, Optional

import numpy as np

from .log import get_logger, kv
from .util import run_cmd

log = get_logger(__name__)


async def set_volume(card: str, control: str, pct: int) -> bool:
    pct = max(0, min(100, int(pct)))
    if not shutil.which("amixer"):
        log.warning("amixer not found; volume not applied")
        return False
    rc, _, err = await run_cmd("amixer", "-q", "-c", card, "sset", control, f"{pct}%", timeout=5)
    if rc != 0:
        log.warning("amixer failed", extra=kv(card=card, control=control, err=err.strip()))
        return False
    return True


def mix_s16(chunks: list[np.ndarray]) -> np.ndarray:
    """Sum int16 mono buffers of equal length with saturation."""
    if not chunks:
        return np.zeros(0, dtype=np.int16)
    n = min(len(c) for c in chunks)
    acc = np.zeros(n, dtype=np.int32)
    for c in chunks:
        acc += c[:n].astype(np.int32)
    return np.clip(acc, -32768, 32767).astype(np.int16)


class SpeakerMixer:
    """Mixes N incoming tracks → one ALSA playback stream (20 ms frames)."""

    FRAME_MS = 20

    def __init__(self, device: str, sample_rate: int = 48000):
        self.device = device
        self.rate = sample_rate
        self.frame = sample_rate * self.FRAME_MS // 1000
        self._buffers: dict[int, bytearray] = {}
        self._lock = threading.Lock()
        self._tasks: dict[int, asyncio.Task] = {}
        self._writer_task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next = 1
        self.max_buffer = self.frame * 2 * 10  # 200 ms per source (bytes)

    def add_track(self, track: Any) -> int:
        sid = self._next
        self._next += 1
        with self._lock:
            self._buffers[sid] = bytearray()
        self._tasks[sid] = asyncio.ensure_future(self._reader(sid, track))
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.ensure_future(self._writer())
        return sid

    def remove(self, sid: int) -> None:
        t = self._tasks.pop(sid, None)
        if t:
            t.cancel()
        with self._lock:
            self._buffers.pop(sid, None)

    async def _reader(self, sid: int, track: Any) -> None:
        import av
        from aiortc.mediastreams import MediaStreamError

        resampler = av.AudioResampler(format="s16", layout="mono", rate=self.rate)
        try:
            while True:
                frame = await track.recv()
                for rf in resampler.resample(frame):
                    data = rf.to_ndarray().astype(np.int16).tobytes()
                    with self._lock:
                        buf = self._buffers.get(sid)
                        if buf is None:
                            return
                        buf.extend(data)
                        if len(buf) > self.max_buffer:  # latency cap
                            del buf[: len(buf) - self.max_buffer]
        except (MediaStreamError, asyncio.CancelledError):
            pass
        finally:
            with self._lock:
                self._buffers.pop(sid, None)

    async def _open(self) -> None:
        if not shutil.which("aplay"):
            raise RuntimeError("aplay not found")
        self._proc = await asyncio.create_subprocess_exec(
            "aplay", "-q", "-D", self.device, "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(self.rate),
            "--buffer-time=80000", "--period-time=20000",
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

    async def _writer(self) -> None:
        nbytes = self.frame * 2
        try:
            while self._tasks:
                if self._proc is None or self._proc.returncode is not None:
                    try:
                        await self._open()
                    except Exception as e:  # noqa: BLE001
                        log.error("speaker open failed", extra=kv(device=self.device, error=str(e)))
                        await asyncio.sleep(2)
                        continue
                await asyncio.sleep(self.FRAME_MS / 1000 * 0.9)
                chunks = []
                with self._lock:
                    for buf in self._buffers.values():
                        if len(buf) >= nbytes:
                            chunks.append(np.frombuffer(bytes(buf[:nbytes]), dtype=np.int16))
                            del buf[:nbytes]
                if not chunks:
                    continue
                out = mix_s16(chunks).tobytes()
                try:
                    assert self._proc and self._proc.stdin
                    self._proc.stdin.write(out)
                    await self._proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError, AssertionError):
                    self._proc = None
        finally:
            await self._close_proc()

    async def _close_proc(self) -> None:
        p, self._proc = self._proc, None
        if p and p.returncode is None:
            try:
                if p.stdin:
                    p.stdin.close()
                p.terminate()
                await asyncio.wait_for(p.wait(), 2)
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        for sid in list(self._tasks):
            self.remove(sid)
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._close_proc()


class MicSource:
    """Shared ALSA capture → per-viewer aiortc tracks via MediaRelay."""

    def __init__(self, device: str, sample_rate: int = 48000, channels: int = 1):
        self.device = device
        self.rate = sample_rate
        self.channels = channels
        self._player = None
        self._relay = None

    def track(self) -> Optional[Any]:
        try:
            from aiortc.contrib.media import MediaPlayer, MediaRelay
        except ImportError:
            return None
        if self._player is None:
            try:
                self._player = MediaPlayer(
                    self.device, format="alsa",
                    options={"channels": str(self.channels), "sample_rate": str(self.rate)},
                )
                self._relay = MediaRelay()
            except Exception as e:  # noqa: BLE001
                log.error("mic open failed", extra=kv(device=self.device, error=str(e)))
                self._player = None
                return None
        if self._player.audio is None:
            return None
        assert self._relay is not None
        return self._relay.subscribe(self._player.audio, buffered=False)

    def release_if_idle(self, viewers: int) -> None:
        """Close the ALSA capture when nobody is listening (privacy + CPU)."""
        if viewers == 0 and self._player is not None:
            try:
                if self._player.audio:
                    self._player.audio.stop()
            except Exception:  # noqa: BLE001
                pass
            self._player = None
            self._relay = None
