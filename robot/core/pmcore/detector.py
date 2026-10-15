"""Object detector: Hailo-8L YOLOv8s (via picamera2.devices.Hailo) or null.

Pipeline: lores 640x640 RGB frames from both cameras (alternating, ~10 fps
each) → Hailo inference → NMS output post-processing → per-camera IoU
tracker → ZMQ ``detections`` (§3) + ``pet_detected`` events (§4.4, rate
limited) + callback for the display (look direction).

Output format of the Hailo model-zoo YOLOv8 HEF with on-chip NMS
(``yolov8s_h8l.hef``, output ``yolov8_nms_postprocess``) as returned by
``Hailo.run()``: a list of 80 per-class arrays, each ``(N, 5)`` of
``[y_min, x_min, y_max, x_max, score]`` normalised 0..1. Some HailoRT
versions return a flat "by-class" buffer instead; both are handled.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np

from .camera import LoresFrame
from .geometry import bbox_bearing
from .log import get_logger, kv
from .tracker import Detection, IdSource, IouTracker

log = get_logger(__name__)

# COCO-80 class ids (YOLOv8 order) for the classes of §3.
COCO_WANTED = {15: "cat", 16: "dog", 0: "person", 32: "sports ball", 45: "bowl"}
PET_CLASSES = ("cat", "dog")


def parse_hailo_nms(output: Any, conf_threshold: float, wanted: dict[int, str] = COCO_WANTED,
                    num_classes: int = 80) -> list[Detection]:
    """Convert Hailo NMS-by-class output to normalised [x1,y1,x2,y2] Detections."""
    dets: list[Detection] = []
    if isinstance(output, dict):
        output = next(iter(output.values()))
    if isinstance(output, (list, tuple)) and len(output) == 1 and isinstance(output[0], (list, tuple)) \
            and len(output[0]) == num_classes:
        output = output[0]  # batch dimension
    if isinstance(output, (list, tuple)) and len(output) == num_classes:
        per_class = output
    else:
        # flat buffer: for each class [count, count×(y1,x1,y2,x2,score)]
        flat = np.asarray(output, dtype=np.float32).ravel()
        per_class = []
        i = 0
        for _ in range(num_classes):
            if i >= flat.size:
                per_class.append(np.zeros((0, 5), np.float32))
                continue
            n = int(flat[i])
            i += 1
            per_class.append(flat[i:i + 5 * n].reshape(n, 5))
            i += 5 * n
    for cid, name in wanted.items():
        if cid >= len(per_class):
            continue
        arr = np.asarray(per_class[cid], dtype=np.float32).reshape(-1, 5)
        for y1, x1, y2, x2, score in arr:
            if score < conf_threshold:
                continue
            bb = [float(np.clip(v, 0.0, 1.0)) for v in (x1, y1, x2, y2)]
            if bb[2] <= bb[0] or bb[3] <= bb[1]:
                continue
            dets.append(Detection(name, round(float(score), 3), [round(v, 4) for v in bb]))
    return dets


class PetEventLimiter:
    """At most one ``pet_detected`` per class every ``min_interval_s``; requires a
    track confirmed for ``min_hits`` frames above ``min_conf``."""

    def __init__(self, min_interval_s: float = 300.0, min_conf: float = 0.55, min_hits: int = 3):
        self.min_interval_s = min_interval_s
        self.min_conf = min_conf
        self.min_hits = min_hits
        self._last: dict[str, float] = {}

    def check(self, cls: str, conf: float, hits: int, now: float) -> bool:
        if cls not in PET_CLASSES or conf < self.min_conf or hits < self.min_hits:
            return False
        last = self._last.get(cls)
        if last is not None and now - last < self.min_interval_s:
            return False
        self._last[cls] = now
        return True


def build_detections_msg(camera: str, stamp: float, dets: Iterable[Detection], hfov_rad: float,
                         yaw_offset: float = 0.0) -> dict:
    return {
        "stamp": stamp,
        "camera": camera,
        "objects": [
            {
                "cls": d.cls,
                "conf": d.conf,
                "bbox": d.bbox,
                "bearing_rad": round(bbox_bearing(d.bbox, camera, hfov_rad, yaw_offset), 4),
                "track_id": d.track_id,
            }
            for d in dets
        ],
    }


class Backend:
    name = "null"

    def infer(self, rgb: np.ndarray) -> list[Detection]:
        return []

    def close(self) -> None:
        pass


class NullBackend(Backend):
    pass


class HailoBackend(Backend):
    name = "hailo"

    def __init__(self, hef: str, conf_threshold: float):
        from picamera2.devices import Hailo  # apt: hailo-all + python3-picamera2

        self.hailo = Hailo(hef)
        self.conf = conf_threshold
        h, w, _ = self.hailo.get_input_shape()
        self.input_hw = (h, w)
        log.info("hailo model loaded", extra=kv(hef=hef, input=f"{w}x{h}"))

    def infer(self, rgb: np.ndarray) -> list[Detection]:
        h, w = self.input_hw
        if rgb.shape[0] != h or rgb.shape[1] != w:
            # lores must already be the network size; fall back to a crude resize
            ys = (np.linspace(0, rgb.shape[0] - 1, h)).astype(np.int32)
            xs = (np.linspace(0, rgb.shape[1] - 1, w)).astype(np.int32)
            rgb = rgb[ys][:, xs]
        out = self.hailo.run(np.ascontiguousarray(rgb))
        return parse_hailo_nms(out, self.conf)

    def close(self) -> None:
        try:
            self.hailo.close()
        except Exception:  # noqa: BLE001
            pass


def make_backend(cfg: dict) -> Backend:
    if not cfg.get("enabled", True):
        return NullBackend()
    try:
        return HailoBackend(cfg["hef"], float(cfg["conf_threshold"]))
    except ImportError as e:
        log.warning("Hailo not available, using null detector", extra=kv(error=str(e)))
    except Exception as e:  # noqa: BLE001
        log.error("Hailo init failed, using null detector", extra=kv(error=str(e)))
    return NullBackend()


@dataclass
class PetSighting:
    camera: str
    cls: str
    bearing_rad: float
    bbox: list[float]
    t: float


class Detector:
    """Runs inference in a worker thread; results delivered on the asyncio loop."""

    def __init__(
        self,
        cfg: dict,
        hfov_deg: float,
        loop: asyncio.AbstractEventLoop,
        publish: Callable[[dict], None],
        on_pet_event: Callable[[dict], None],
        on_sighting: Callable[[Optional[PetSighting]], None],
        pet_events_enabled: Callable[[], bool] = lambda: True,
        backend: Optional[Backend] = None,
    ):
        import math

        self.cfg = cfg
        self.hfov = math.radians(hfov_deg)
        self.loop = loop
        self.publish = publish
        self.on_pet_event = on_pet_event
        self.on_sighting = on_sighting
        self.pet_events_enabled = pet_events_enabled
        self.backend = backend
        ids = IdSource()
        self.trackers = {
            c: IouTracker(float(cfg["iou_match"]), float(cfg["track_max_age_s"]), ids) for c in ("front", "rear")
        }
        self.limiter = PetEventLimiter(float(cfg["pet_event_min_interval_s"]), float(cfg["pet_event_min_conf"]))
        self._q: "queue.Queue[LoresFrame]" = queue.Queue(maxsize=2)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_pet_t = 0.0
        self.stats = {"frames": 0, "infer_ms": 0.0}

    @property
    def backend_name(self) -> str:
        return self.backend.name if self.backend else "none"

    def start(self) -> None:
        if self.backend is None:
            self.backend = make_backend(self.cfg)
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        if self.backend:
            self.backend.close()

    def push(self, f: LoresFrame) -> None:
        """Camera-thread entry point; drops the oldest frame when busy."""
        if isinstance(self.backend, NullBackend):
            return
        try:
            self._q.put_nowait(f)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(f)
            except queue.Full:
                pass

    def _loop(self) -> None:
        assert self.backend is not None
        while self._running:
            try:
                f = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            t0 = time.perf_counter()
            try:
                dets = self.backend.infer(f.rgb)
            except Exception:  # noqa: BLE001
                log.exception("inference failed")
                time.sleep(0.5)
                continue
            self.stats["frames"] += 1
            self.stats["infer_ms"] = 0.9 * self.stats["infer_ms"] + 0.1 * (time.perf_counter() - t0) * 1000
            self.loop.call_soon_threadsafe(self.process, f.camera, dets, time.time(), time.monotonic())

    def process(self, camera: str, dets: list[Detection], stamp: float, mono: float) -> dict:
        """Track, publish, raise events. Runs on the event loop (also used by tests)."""
        dets = self.trackers[camera].update(dets, mono)
        msg = build_detections_msg(camera, stamp, dets, self.hfov, 0.0)
        self.publish(msg)
        best = None
        for d, o in zip(dets, msg["objects"]):
            if d.cls not in PET_CLASSES:
                continue
            tr = self.trackers[camera].get(d.track_id)
            hits = tr.hits if tr else 1
            if best is None or d.conf > best[0].conf:
                best = (d, o)
            if self.pet_events_enabled() and self.limiter.check(d.cls, d.conf, hits, mono):
                self.on_pet_event({"t": "pet_detected", "ts": stamp, "camera": camera, "cls": d.cls})
        if best is not None:
            d, o = best
            self._last_pet_t = mono
            self.on_sighting(PetSighting(camera, d.cls, o["bearing_rad"], d.bbox, mono))
        elif mono - self._last_pet_t > 3.0:
            self.on_sighting(None)
        return msg
