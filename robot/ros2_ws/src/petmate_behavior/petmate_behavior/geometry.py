"""Pure geometry helpers and controllers (seek pet, dock approach)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple


def wrap(a: float) -> float:
    return math.atan2(math.sin(a), math.cos(a))


def yaw_from_quat(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def quat_from_yaw(th: float) -> Tuple[float, float, float, float]:
    return 0.0, 0.0, math.sin(th / 2.0), math.cos(th / 2.0)


def predock_pose(dock: dict, dist: float) -> dict:
    """Dock POI = robot pose when docked (rear contacts touching the dock, robot
    heading away from it). Pre-dock pose lies ``dist`` m in front of it with
    the same heading; final approach is a straight reverse."""
    th = float(dock['th'])
    return {'x': float(dock['x']) + dist * math.cos(th),
            'y': float(dock['y']) + dist * math.sin(th),
            'th': th}


def dock_reverse_cmd(yaw: float, dock_th: float, lateral_err: float = 0.0,
                     v: float = 0.05, k_th: float = 1.5, k_y: float = 0.0,
                     w_max: float = 0.3) -> Tuple[float, float]:
    """Open-loop reverse at ``-v`` while holding heading (odom/map yaw).
    ``lateral_err`` (+ = robot left of dock axis) is optional (k_y=0 => off)."""
    e = wrap(dock_th - yaw)
    w = k_th * e - k_y * lateral_err
    w = max(-w_max, min(w_max, w))
    return -abs(v), w


@dataclass
class SeekParams:
    target_dist: float = 0.80     # keep ~0.8 m from the pet
    dist_tol: float = 0.10
    v_max: float = 0.25           # never chase faster than 0.25 m/s
    w_max: float = 1.0
    k_w: float = 1.5
    k_v: float = 0.6
    align_tol: float = 0.25       # rad: rotate in place until |bearing| < this
    reverse_ok: bool = False      # back off if too close (default: just stop)


def seek_cmd(bearing: Optional[float], dist: Optional[float],
             p: SeekParams = SeekParams()) -> Tuple[float, float, bool]:
    """Return (v, w, arrived). ``bearing`` in base_link (+ left), ``dist`` range
    to the pet along that bearing (None if unknown)."""
    if bearing is None:
        return 0.0, 0.0, False
    b = wrap(bearing)
    w = max(-p.w_max, min(p.w_max, p.k_w * b))
    if abs(b) > p.align_tol:
        return 0.0, w, False                     # rotate in place first
    if dist is None or not math.isfinite(dist):
        # no range: creep forward slowly while centred
        return min(0.10, p.v_max), w, False
    err = dist - p.target_dist
    if abs(err) <= p.dist_tol:
        return 0.0, w * 0.5, True
    if err < 0:
        return (max(-0.10, p.k_v * err) if p.reverse_ok else 0.0), w, not p.reverse_ok
    v = min(p.v_max, p.k_v * err)
    # slow down when not well aligned
    v *= max(0.0, math.cos(b))
    return v, w, False


def range_at_bearing(ranges, angle_min: float, angle_increment: float,
                     bearing: float, half_width: float = 0.10,
                     range_min: float = 0.05, range_max: float = 30.0) -> Optional[float]:
    """Min valid range of a LaserScan within +-half_width around ``bearing``.
    Works for full (360 deg) and partial scans; angles outside the scan are skipped."""
    n = len(ranges) if ranges is not None else 0
    if n == 0 or angle_increment <= 0.0:
        return None
    best = None
    steps = max(1, int(math.ceil(2.0 * half_width / angle_increment)))
    for k in range(steps + 1):
        a = bearing - half_width + k * (2.0 * half_width / steps)
        rel = (a - angle_min) % (2.0 * math.pi)
        i = int(round(rel / angle_increment))
        if i == n and abs(n * angle_increment - 2.0 * math.pi) < angle_increment:
            i = 0  # full-circle wrap
        if 0 <= i < n:
            r = ranges[i]
            if r is not None and math.isfinite(r) and range_min <= r <= range_max:
                best = r if best is None else min(best, r)
    return best
