"""Greedy IoU multi-object tracker (per camera, per class).

Detections of the same class are matched to existing tracks by IoU (highest
first, greedy). Unmatched detections spawn new tracks; tracks not updated for
``max_age_s`` are dropped. Track ids are globally unique and monotonically
increasing across cameras (one ``IdSource`` shared by both trackers) so a
``track_id`` never collides between front and rear.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Iterable, Sequence


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


class IdSource:
    def __init__(self, start: int = 1):
        self._it = itertools.count(start)

    def next(self) -> int:
        return next(self._it)


@dataclass
class Track:
    track_id: int
    cls: str
    bbox: list[float]
    conf: float
    first_seen: float
    last_seen: float
    hits: int = 1
    extra: dict = field(default_factory=dict)


@dataclass
class Detection:
    cls: str
    conf: float
    bbox: list[float]
    track_id: int = -1


class IouTracker:
    def __init__(self, iou_threshold: float = 0.3, max_age_s: float = 1.5, ids: IdSource | None = None):
        self.iou_threshold = iou_threshold
        self.max_age_s = max_age_s
        self.ids = ids or IdSource()
        self.tracks: dict[int, Track] = {}

    def update(self, detections: Iterable[Detection], now: float) -> list[Detection]:
        dets = list(detections)
        # expire
        for tid in [t for t, tr in self.tracks.items() if now - tr.last_seen > self.max_age_s]:
            del self.tracks[tid]

        pairs: list[tuple[float, int, int]] = []
        for di, d in enumerate(dets):
            for tid, tr in self.tracks.items():
                if tr.cls != d.cls:
                    continue
                v = iou(d.bbox, tr.bbox)
                if v >= self.iou_threshold:
                    pairs.append((v, di, tid))
        pairs.sort(key=lambda p: -p[0])
        used_d: set[int] = set()
        used_t: set[int] = set()
        for _, di, tid in pairs:
            if di in used_d or tid in used_t:
                continue
            used_d.add(di)
            used_t.add(tid)
            tr = self.tracks[tid]
            tr.bbox = list(dets[di].bbox)
            tr.conf = dets[di].conf
            tr.last_seen = now
            tr.hits += 1
            dets[di].track_id = tid
        for di, d in enumerate(dets):
            if di in used_d:
                continue
            tid = self.ids.next()
            self.tracks[tid] = Track(tid, d.cls, list(d.bbox), d.conf, now, now)
            d.track_id = tid
        return dets

    def get(self, track_id: int) -> Track | None:
        return self.tracks.get(track_id)
