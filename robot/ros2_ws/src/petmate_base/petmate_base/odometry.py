"""Pure diff-drive odometry from encoder ticks (fallback when odom_source=encoders)."""
from __future__ import annotations

import math
from typing import Optional, Tuple


class DiffDriveOdometry:
    def __init__(self, ticks_per_rev: float = 1320.0, wheel_radius: float = 0.036,
                 track: float = 0.260):
        self.ticks_per_rev = float(ticks_per_rev)
        self.wheel_radius = float(wheel_radius)
        self.track = float(track)
        self.reset()

    def reset(self):
        self.x = 0.0
        self.y = 0.0
        self.th = 0.0
        self.v = 0.0
        self.w = 0.0
        self._last: Optional[Tuple[int, int, float]] = None

    @property
    def m_per_tick(self) -> float:
        return 2.0 * math.pi * self.wheel_radius / self.ticks_per_rev

    def update(self, enc_l: int, enc_r: int, t_s: float) -> Tuple[float, float, float, float, float]:
        if self._last is None:
            self._last = (enc_l, enc_r, t_s)
            return self.x, self.y, self.th, 0.0, 0.0
        l0, r0, t0 = self._last
        self._last = (enc_l, enc_r, t_s)
        dl = _wrap_i32(enc_l - l0) * self.m_per_tick
        dr = _wrap_i32(enc_r - r0) * self.m_per_tick
        ds = 0.5 * (dl + dr)
        dth = (dr - dl) / self.track
        # midpoint integration
        self.x += ds * math.cos(self.th + 0.5 * dth)
        self.y += ds * math.sin(self.th + 0.5 * dth)
        self.th = math.atan2(math.sin(self.th + dth), math.cos(self.th + dth))
        dt = t_s - t0
        if dt > 1e-4:
            self.v = ds / dt
            self.w = dth / dt
        return self.x, self.y, self.th, self.v, self.w


def _wrap_i32(d: int) -> int:
    d &= 0xFFFFFFFF
    return d - 0x100000000 if d & 0x80000000 else d
