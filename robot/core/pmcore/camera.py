"""Camera sources and fan-out.

Backends (settings ``camera.backend``):

* ``picamera2`` (product) — dual Camera Module 3 Wide. Each camera runs a
  Picamera2 instance configured with ``main`` 1280x720 ``YUV420`` (I420, ready
  for libx264) and ``lores`` 640x640 ``BGR888`` (RGB bytes for the Hailo
  YOLOv8 HEF). Falls back to ``synthetic`` when picamera2 is not importable.
* ``uvc`` (MVP, docs/MVP.md) — one USB UVC webcam (Logitech) through PyAV's
  ``v4l2`` demuxer. MJPEG 1280x720@30 is requested first, then MJPEG 640x480,
  YUYV 640x480, then the driver default. Only camera ``front`` exists.
  The device is opened **on demand** (while a viewer/snapshot/detector needs
  frames) and closed ``idle_close_s`` after the last consumer left, so the
  webcam's LED doubles as a "someone is watching" indicator. Unplug/replug is
  handled by retrying every ``reconnect_s`` (2 s) with clear logs.
* ``lavfi`` (tests) — FFmpeg ``testsrc2`` through exactly the same code path as
  ``uvc`` (paced to real time). Default pixel format yuvj422p = what the MJPEG
  decoder produces for a Logitech webcam.
* ``synthetic`` — numpy moving bar (no FFmpeg needed).

Main frames are delivered as :class:`MainFrame`; PyAV backends hand over the
decoded ``av.VideoFrame`` itself (no ndarray round-trip): the encoder thread
does the single required swscale conversion to yuv420p.

A capture thread per camera; consumers are plain callables invoked from that
thread (they must be cheap and thread-safe).
"""

from __future__ import annotations

import fcntl
import glob
import os
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from .log import get_logger, kv

log = get_logger(__name__)

CAMERAS = ("front", "rear")
AV_BACKENDS = ("uvc", "lavfi")
BACKENDS = ("picamera2", "uvc", "lavfi", "synthetic")


class CameraNotAvailable(ValueError):
    """Requested camera does not exist with the configured backend."""


@dataclass
class MainFrame:
    camera: str
    t: float                       # time.monotonic() at capture
    yuv420: Optional[np.ndarray]   # shape (h*3/2, w) uint8 (I420), or None when ``vframe`` is set
    width: int
    height: int
    vframe: Any = field(default=None, repr=False)  # av.VideoFrame (any pix_fmt) from PyAV backends

    def i420(self) -> np.ndarray:
        """I420 ndarray (h*3/2, w); converts the PyAV frame only when needed."""
        if self.yuv420 is not None:
            return self.yuv420
        vf = self.vframe
        if vf.format.name != "yuv420p":
            vf = vf.reformat(format="yuv420p")
        return vf.to_ndarray()

    def copy(self) -> "MainFrame":
        """Detach from capture buffers (picamera2 buffers are recycled; av frames are refcounted)."""
        if self.yuv420 is not None:
            return MainFrame(self.camera, self.t, self.yuv420.copy(), self.width, self.height)
        return MainFrame(self.camera, self.t, None, self.width, self.height, self.vframe)


@dataclass
class LoresFrame:
    camera: str
    t: float
    rgb: np.ndarray        # shape (h, w, 3) uint8, RGB order


MainSink = Callable[[MainFrame], None]
LoresSink = Callable[[LoresFrame], None]


# ---------------------------------------------------------------------------- V4L2 discovery
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000
VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability) (104 bytes)
LOGITECH_VID = "046d"


def v4l2_querycap(dev: str) -> Optional[dict]:
    """VIDIOC_QUERYCAP via ioctl (no v4l-utils needed). None if it can't be queried."""
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf, True)
    except OSError:
        return None
    finally:
        os.close(fd)
    caps, dcaps = struct.unpack_from("<II", buf, 84)
    eff = dcaps if caps & V4L2_CAP_DEVICE_CAPS else caps
    return {
        "driver": bytes(buf[0:16]).split(b"\0", 1)[0].decode(errors="replace"),
        "card": bytes(buf[16:48]).split(b"\0", 1)[0].decode(errors="replace"),
        "bus_info": bytes(buf[48:80]).split(b"\0", 1)[0].decode(errors="replace"),
        "capture": bool(eff & V4L2_CAP_VIDEO_CAPTURE),
    }


def _sysfs_driver(node_dir: str) -> Optional[str]:
    for rel in ("device/driver", "device/../driver"):
        p = os.path.join(node_dir, rel)
        if os.path.exists(p):
            return os.path.basename(os.path.realpath(p))
    return None


def find_uvc_device(
    by_id_dir: str = "/dev/v4l/by-id",
    sysfs_dir: str = "/sys/class/video4linux",
    dev_dir: str = "/dev",
    querycap: Callable[[str], Optional[dict]] = v4l2_querycap,
) -> Optional[str]:
    """``device: "auto"`` resolution (docs/MVP.md).

    1. first ``/dev/v4l/by-id/*Logitech*-video-index0`` (also ``usb-046d_*``: many
       Logitech models expose the vendor id instead of the name there);
    2. else the first ``/dev/videoN`` whose sysfs driver is ``uvcvideo`` and whose
       V4L2 capabilities include VIDEO_CAPTURE (UVC also creates a metadata node
       per camera, which is skipped). When the ioctl isn't possible, the sysfs
       ``index`` attribute (0 = capture node) is used instead.
    """
    links = sorted(glob.glob(os.path.join(by_id_dir, "*-video-index0")))
    for p in links:
        name = os.path.basename(p)
        if "logitech" in name.lower() or name.lower().startswith(f"usb-{LOGITECH_VID}_"):
            if os.path.exists(p):
                return p

    def num(p: str) -> int:
        m = re.search(r"(\d+)$", p)
        return int(m.group(1)) if m else 1 << 30

    for node in sorted(glob.glob(os.path.join(sysfs_dir, "video*")), key=num):
        if _sysfs_driver(node) != "uvcvideo":
            continue
        dev = os.path.join(dev_dir, os.path.basename(node))
        if not os.path.exists(dev):
            continue
        cap = querycap(dev)
        if cap is not None:
            if cap.get("capture"):
                return dev
            continue
        try:
            with open(os.path.join(node, "index")) as f:
                if f.read().strip() == "0":
                    return dev
        except OSError:
            return dev
    return None


def uvc_modes(main_size: tuple[int, int], fps: int, input_format: str = "mjpeg") -> list[dict]:
    """Ordered list of V4L2 option sets to try (docs/MVP.md fallbacks)."""
    w, h = int(main_size[0]), int(main_size[1])
    cands: list[dict] = [
        {"input_format": input_format, "video_size": f"{w}x{h}", "framerate": str(fps)},
        {"input_format": "mjpeg", "video_size": "640x480", "framerate": str(fps)},
        {"input_format": "yuyv422", "video_size": "640x480", "framerate": str(min(fps, 30))},
        {},  # driver default
    ]
    out: list[dict] = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


# ---------------------------------------------------------------------------- camera threads
class _CamBase:
    def __init__(self, name: str, mgr: "CameraManager"):
        self.name = name
        self.mgr = mgr
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_lores = 0.0
        self.frames = 0
        self.ok = False
        self.info: dict = {}

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop_guard, name=f"cam-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _loop_guard(self) -> None:
        try:
            self._loop()
        except Exception:  # noqa: BLE001
            log.exception("camera thread crashed", extra=kv(camera=self.name))
            self.ok = False

    def _loop(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _lores_due(self, t: float) -> bool:
        period = 1.0 / self.mgr.lores_rate_hz if self.mgr.lores_rate_hz > 0 else None
        return period is not None and bool(self.mgr.lores_sinks) and t - self._last_lores >= period * 0.95

    def _dispatch(self, t: float, get_main: Callable[[], Any], get_lores: Callable[[], np.ndarray]) -> None:
        self.frames += 1
        if self.mgr.wants_main(self.name):
            m = get_main()
            if isinstance(m, np.ndarray):
                w, h = self.mgr.main_size
                self.mgr._emit_main(MainFrame(self.name, t, m, w, h))
            else:  # av.VideoFrame
                self.mgr._emit_main(MainFrame(self.name, t, None, m.width, m.height, m))
        if self._lores_due(t):
            self._last_lores = t
            self.mgr._emit_lores(LoresFrame(self.name, t, get_lores()))


class PiCam(_CamBase):
    def __init__(self, name: str, index: int, mgr: "CameraManager", hflip: bool, vflip: bool, lores_format: str):
        super().__init__(name, mgr)
        self.index = index
        self.hflip = hflip
        self.vflip = vflip
        self.lores_format = lores_format
        self.picam2 = None

    def open(self) -> None:
        from picamera2 import Picamera2  # apt: python3-picamera2
        from libcamera import Transform

        self.picam2 = Picamera2(self.index)
        w, h = self.mgr.main_size
        lw, lh = self.mgr.lores_size
        cfg = self.picam2.create_video_configuration(
            main={"size": (w, h), "format": "YUV420"},
            lores={"size": (lw, lh), "format": self.lores_format},
            transform=Transform(hflip=self.hflip, vflip=self.vflip),
            controls={"FrameRate": float(self.mgr.fps)},
            buffer_count=6,
            # full-FOV binned sensor mode for the widest view
            sensor={"output_size": (2304, 1296)},
        )
        self.picam2.configure(cfg)
        try:
            from libcamera import controls

            self.picam2.set_controls({"AfMode": controls.AfModeEnum.Continuous})
        except Exception:  # noqa: BLE001
            pass
        self.picam2.start()
        self.ok = True
        log.info("camera started", extra=kv(camera=self.name, index=self.index))

    def _loop(self) -> None:
        assert self.picam2 is not None
        swap = self.lores_format == "RGB888"  # picamera2 RGB888 is B,G,R in memory
        while self._running:
            req = self.picam2.capture_request()
            try:
                t = time.monotonic()
                self._dispatch(
                    t,
                    lambda: req.make_array("main"),
                    (lambda: req.make_array("lores")[:, :, ::-1].copy()) if swap else (lambda: req.make_array("lores")),
                )
            finally:
                req.release()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        if self.picam2 is not None:
            try:
                self.picam2.stop()
                self.picam2.close()
            except Exception:  # noqa: BLE001
                pass


class SyntheticCam(_CamBase):
    """Moving test pattern; used when picamera2 is unavailable (dev machine)."""

    def _loop(self) -> None:
        w, h = self.mgr.main_size
        lw, lh = self.mgr.lores_size
        period = 1.0 / self.mgr.fps
        ybase = np.tile(np.linspace(16, 235, w, dtype=np.uint8), (h, 1))
        uv = np.full((h // 2, w), 128 if self.name == "front" else 90, dtype=np.uint8)
        self.ok = True
        nxt = time.monotonic()
        k = 0
        while self._running:
            k += 1
            t = time.monotonic()
            x = (k * 8) % w

            def main() -> np.ndarray:
                y = ybase.copy()
                y[:, max(0, x - 20):x + 20] = 235
                return np.vstack([y, uv])

            def lores() -> np.ndarray:
                return np.zeros((lh, lw, 3), dtype=np.uint8)

            self._dispatch(t, main, lores)
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))


class AvCaptureCam(_CamBase):
    """PyAV capture (``uvc`` = V4L2 webcam, ``lavfi`` = FFmpeg test source).

    Opened on demand, closed when idle, re-opened every ``reconnect_s`` after a
    failure (camera missing, unplugged, busy...).
    """

    def __init__(self, name: str, mgr: "CameraManager", backend: str, device: str = "auto",
                 input_format: str = "mjpeg", reconnect_s: float = 2.0, idle_close_s: float = 5.0,
                 lavfi_source: str = "testsrc2=size={w}x{h}:rate={fps}",
                 finder: Callable[[], Optional[str]] = find_uvc_device):
        super().__init__(name, mgr)
        assert backend in AV_BACKENDS
        self.backend = backend
        self.device = device
        self.input_format = input_format
        self.reconnect_s = float(reconnect_s)
        self.idle_close_s = float(idle_close_s)
        self.lavfi_source = lavfi_source
        self._finder = finder
        self._wake = threading.Event()
        self.opens = 0
        self.failures = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------ helpers
    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)

    def poke(self) -> None:
        """Wake the thread (a consumer appeared)."""
        self._wake.set()

    def _sleep(self, s: float) -> None:
        self._wake.wait(s)
        self._wake.clear()

    def resolve_source(self) -> Optional[str]:
        if self.backend == "lavfi":
            w, h = self.mgr.main_size
            return self.lavfi_source.format(w=w, h=h, fps=self.mgr.fps)
        if self.device and self.device != "auto":
            return self.device if os.path.exists(self.device) else None
        return self._finder()

    def open_source(self, src: str) -> tuple[Any, Any, dict]:
        """Open ``src`` trying the fallback modes; returns (container, stream, mode)."""
        import av

        if self.backend == "lavfi":
            c = av.open(src, format="lavfi", timeout=(5.0, 5.0))
            return c, c.streams.video[0], {"source": "lavfi"}
        errors = []
        for mode in uvc_modes(self.mgr.main_size, self.mgr.fps, self.input_format):
            try:
                c = av.open(src, format="v4l2", options=dict(mode), timeout=(5.0, 5.0))
            except Exception as e:  # noqa: BLE001  (av.error.* subclasses OSError/ValueError)
                errors.append(f"{mode or 'default'}: {e}")
                continue
            return c, c.streams.video[0], mode
        raise OSError("no usable V4L2 mode: " + " | ".join(errors))

    # ------------------------------------------------------------ thread
    def _loop(self) -> None:
        missing_logged = False
        while self._running:
            if not self.mgr.demand(self.name):
                self._sleep(0.2)
                continue
            src = self.resolve_source()
            if src is None:
                if not missing_logged:
                    log.warning("camera not found; retrying", extra=kv(
                        camera=self.name, device=self.device, every_s=self.reconnect_s))
                    missing_logged = True
                self.last_error = "not_found"
                self._sleep(self.reconnect_s)
                continue
            try:
                container, stream, mode = self.open_source(src)
            except Exception as e:  # noqa: BLE001
                self.failures += 1
                self.last_error = str(e)
                log.warning("camera open failed; retrying", extra=kv(
                    camera=self.name, device=src, error=str(e), every_s=self.reconnect_s))
                self._sleep(self.reconnect_s)
                continue
            missing_logged = False
            self.opens += 1
            cc = stream.codec_context
            self.info = {
                "backend": self.backend, "device": src if self.backend == "uvc" else "lavfi",
                "input_format": mode.get("input_format") or cc.name, "codec": cc.name,
                "size": [cc.width, cc.height],
                "fps": float(stream.average_rate) if stream.average_rate else None,
            }
            self.ok = True
            log.info("camera opened", extra=kv(camera=self.name, **self.info))
            reason: Optional[str]
            try:
                reason = self._pump(container, stream)
            except Exception as e:  # noqa: BLE001  (ENODEV on unplug, EIO, timeouts...)
                reason = f"{e.__class__.__name__}: {e}"
            finally:
                self.ok = False
                try:
                    container.close()
                except Exception:  # noqa: BLE001
                    pass
            if reason is None:
                log.info("camera closed (no consumers)", extra=kv(camera=self.name))
                continue
            self.failures += 1
            self.last_error = reason
            if self._running:
                log.warning("camera lost (unplugged?); reconnecting", extra=kv(
                    camera=self.name, device=src, reason=reason, every_s=self.reconnect_s))
                self._sleep(self.reconnect_s)

    def _pump(self, container: Any, stream: Any) -> Optional[str]:
        """Read until idle (returns None) or error/EOF (returns reason)."""
        pace = self.backend == "lavfi"  # lavfi produces as fast as we read
        period = 1.0 / max(1, self.mgr.fps)
        nxt = time.monotonic()
        last_demand = time.monotonic()
        for packet in container.demux(stream):
            if not self._running:
                return None
            now = time.monotonic()
            want_main = self.mgr.wants_main(self.name)
            want_lores = self._lores_due(now)
            if want_main or want_lores:
                last_demand = now
            elif not self.mgr.demand(self.name) and now - last_demand > self.idle_close_s:
                return None
            if packet.size == 0:
                continue
            self.frames += 1
            if want_main or want_lores:
                # Decoding is skipped while nobody needs pixels (MJPEG/raw are intra-only).
                for vf in packet.decode():
                    if want_main:
                        self.mgr._emit_main(MainFrame(self.name, now, None, vf.width, vf.height, vf))
                    if want_lores:
                        self._last_lores = now
                        lw, lh = self.mgr.lores_size
                        rgb = vf.reformat(width=lw, height=lh, format="rgb24").to_ndarray()
                        self.mgr._emit_lores(LoresFrame(self.name, now, rgb))
            if pace:
                nxt += period
                d = nxt - time.monotonic()
                if d > 0:
                    time.sleep(d)
                elif d < -1.0:
                    nxt = time.monotonic()
        return "end of stream"


# ---------------------------------------------------------------------------- manager
class CameraManager:
    def __init__(self, settings: dict, lores_rate_hz: float):
        self.s = settings
        self.backend = str(settings.get("backend", "picamera2"))
        if self.backend not in BACKENDS:
            raise ValueError(f"unknown camera backend {self.backend!r} (expected one of {BACKENDS})")
        self.main_size = tuple(settings["main_size"])
        self.lores_size = tuple(settings["lores_size"])
        self.fps = int(settings["fps"])
        self.lores_rate_hz = lores_rate_hz
        self.cams: dict[str, _CamBase] = {}
        # MVP (uvc/lavfi): a single USB webcam = "front".
        self.available: tuple[str, ...] = ("front",) if self.backend in AV_BACKENDS else CAMERAS
        self.selected = "front"
        self.main_sinks: list[MainSink] = []
        self.lores_sinks: list[LoresSink] = []
        self._encoder_wants = False
        self._snap_lock = threading.Lock()
        self._snap_waiters: list[tuple[str, Callable[[MainFrame], None]]] = []
        self.synthetic = False

    # ------------------------------------------------------------ lifecycle
    def start(self, allow_synthetic: bool = True) -> None:
        if self.backend in AV_BACKENDS:
            s = self.s
            cam = AvCaptureCam(
                "front", self, self.backend, device=str(s.get("device", "auto")),
                input_format=str(s.get("input_format", "mjpeg")),
                reconnect_s=float(s.get("reconnect_s", 2.0)), idle_close_s=float(s.get("idle_close_s", 5.0)),
                lavfi_source=str(s.get("lavfi_source", "testsrc2=size={w}x{h}:rate={fps}")),
            )
            self.cams["front"] = cam
            cam.start()
            log.info("camera backend", extra=kv(backend=self.backend, device=s.get("device", "auto")))
            return
        idx = {"front": int(self.s["front_index"]), "rear": int(self.s["rear_index"])}
        have_pc = False
        if self.backend == "picamera2":
            try:
                import picamera2  # noqa: F401
                have_pc = True
            except ImportError:
                have_pc = False
        for name in CAMERAS:
            cam_b: _CamBase
            if have_pc:
                pc = PiCam(name, idx[name], self, bool(self.s.get("hflip")), bool(self.s.get("vflip")),
                           self.s.get("lores_format", "BGR888"))
                try:
                    pc.open()
                    cam_b = pc
                except Exception as e:  # noqa: BLE001
                    log.error("camera open failed", extra=kv(camera=name, error=str(e)))
                    if not allow_synthetic:
                        continue
                    cam_b = SyntheticCam(name, self)
                    self.synthetic = True
            elif allow_synthetic or self.backend == "synthetic":
                cam_b = SyntheticCam(name, self)
                self.synthetic = True
            else:
                continue
            self.cams[name] = cam_b
            cam_b.start()
        if self.synthetic:
            log.warning("using synthetic camera source(s)")

    def stop(self) -> None:
        for c in self.cams.values():
            c.stop()
        self.cams.clear()

    def _poke(self, cam: Optional[str] = None) -> None:
        for n, c in self.cams.items():
            if (cam is None or n == cam) and isinstance(c, AvCaptureCam):
                c.poke()

    # ------------------------------------------------------------ selection
    def select(self, cam: str) -> None:
        if cam not in self.available:
            raise CameraNotAvailable(cam)
        if cam != self.selected:
            log.info("camera selected", extra=kv(camera=cam))
        self.selected = cam

    def set_encoder_active(self, active: bool) -> None:
        self._encoder_wants = active
        if active:
            self._poke(self.selected)

    def wants_main(self, cam: str) -> bool:
        if cam == self.selected and self._encoder_wants:
            return True
        if self._snap_waiters:
            with self._snap_lock:
                return any(c == cam for c, _ in self._snap_waiters)
        return False

    def demand(self, cam: str) -> bool:
        """Someone needs frames from ``cam`` (main or lores)."""
        return self.wants_main(cam) or (bool(self.lores_sinks) and self.lores_rate_hz > 0)

    def grab_main(self, cam: str, cb: Callable[[MainFrame], None]) -> None:
        """Deliver the next main frame of ``cam`` to ``cb`` once (from cam thread)."""
        with self._snap_lock:
            self._snap_waiters.append((cam, cb))
        self._poke(cam)

    # ------------------------------------------------------------ fan-out
    def _emit_main(self, f: MainFrame) -> None:
        if self._snap_waiters:
            with self._snap_lock:
                mine = [cb for c, cb in self._snap_waiters if c == f.camera]
                self._snap_waiters = [(c, cb) for c, cb in self._snap_waiters if c != f.camera]
            for cb in mine:
                try:
                    cb(f)
                except Exception:  # noqa: BLE001
                    log.exception("snapshot callback failed")
        if f.camera == self.selected and self._encoder_wants:
            for s in self.main_sinks:
                s(f)

    def _emit_lores(self, f: LoresFrame) -> None:
        for s in self.lores_sinks:
            s(f)

    def status(self) -> dict:
        out = {}
        for n, c in self.cams.items():
            d: dict[str, Any] = {"ok": c.ok, "frames": c.frames}
            if isinstance(c, AvCaptureCam):
                d.update(c.info, opens=c.opens, failures=c.failures, last_error=c.last_error)
            out[n] = d
        return out
