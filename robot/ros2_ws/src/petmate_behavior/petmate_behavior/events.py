"""Pure mapping of MCU events / flag edges to §4.4 events."""
from __future__ import annotations

from typing import Dict, List, Optional

# MCU EVENT codes (§2)
CLIFF, BUMP, WHEEL_DROP, MOTOR_FAULT = 1, 2, 3, 4
BUTTON_SHORT, BUTTON_LONG, BUTTON_PROVISION = 5, 6, 7
DOCK_CONTACT, UNDOCK, BATTERY_LOW, BATTERY_CRITICAL = 8, 9, 10, 11
FEEDER_JAM, HOPPER_LOW, WATCHDOG = 12, 13, 14

FLAG_ESTOP = 1 << 0
FLAG_DOCKED = 1 << 4


def ev(t: str, ts: float, **kw) -> dict:
    d = {'t': t, 'ts': round(float(ts), 3)}
    d.update(kw)
    return d


def mcu_event_to_events(code: int, data: int, ts: float) -> List[dict]:
    """Translate one MCU EVENT into zero or more §4.4 events.

    docked/undocked are NOT emitted here (derived from flags bit4 edges to
    avoid duplicates); estop likewise from flags bit0.
    """
    if code == CLIFF:
        return [ev('cliff', ts)]
    if code == WHEEL_DROP:
        return [ev('stuck', ts, reason='wheel_drop')]
    if code == FEEDER_JAM:
        return [ev('feeder_jam', ts)]
    if code == HOPPER_LOW:
        return [ev('hopper_low', ts, grams=int(data))]
    # BATTERY_LOW / BATTERY_CRITICAL are handled by BatteryMonitor (latched,
    # one battery_low event per discharge cycle).
    if code == MOTOR_FAULT:
        # ADDITION (not in §4.4): generic hardware fault notification
        return [ev('fault', ts, what='motor_fault')]
    if code == BUTTON_PROVISION:
        # ADDITION (not in §4.4): pm-core opens the BLE provisioning window (§7).
        # pm-core consumes it locally (not forwarded to app/cloud).
        return [ev('button_provision', ts)]
    # BUTTON_SHORT / BUTTON_LONG are handled inside petmate_behavior (play / sleep).
    return []


class FlagEdges:
    """Detect rising/falling edges of MCU flags -> docked/undocked/estop."""

    def __init__(self):
        self._prev: Optional[int] = None

    def update(self, flags: int, ts: float) -> List[dict]:
        out: List[dict] = []
        prev = self._prev
        self._prev = flags
        if prev is None:
            return out
        if (flags & FLAG_DOCKED) and not (prev & FLAG_DOCKED):
            out.append(ev('docked', ts))
        if not (flags & FLAG_DOCKED) and (prev & FLAG_DOCKED):
            out.append(ev('undocked', ts))
        if (flags & FLAG_ESTOP) and not (prev & FLAG_ESTOP):
            out.append(ev('estop', ts))
        return out


class PetSightings:
    """Rate-limited pet_detected events (one per cooldown)."""

    def __init__(self, classes=('cat', 'dog'), min_conf: float = 0.5,
                 cooldown_s: float = 300.0):
        self.classes = set(classes)
        self.min_conf = min_conf
        self.cooldown = cooldown_s
        self.last_seen_ts: Optional[float] = None
        self.last_event_ts: Optional[float] = None
        self.camera: Optional[str] = None
        self.bearing: Optional[float] = None
        self.cls: Optional[str] = None

    def update(self, det: dict, now: float) -> List[dict]:
        best = None
        for o in det.get('objects') or []:
            if o.get('cls') in self.classes and float(o.get('conf', 0)) >= self.min_conf:
                if best is None or float(o['conf']) > float(best['conf']):
                    best = o
        if best is None:
            return []
        ts = float(det.get('stamp', now) or now)
        self.last_seen_ts = ts
        self.camera = det.get('camera')
        self.cls = best.get('cls')
        try:
            self.bearing = float(best.get('bearing_rad'))
        except (TypeError, ValueError):
            self.bearing = None
        if self.last_event_ts is None or now - self.last_event_ts >= self.cooldown:
            self.last_event_ts = now
            return [ev('pet_detected', now, camera=self.camera, cls=self.cls)]
        return []

    def seen_within(self, now: float, s: float) -> bool:
        return self.last_seen_ts is not None and now - self.last_seen_ts <= s


class BatteryMonitor:
    """Latch: one ``battery_low`` per discharge cycle, from MCU event or SoC
    threshold, whichever comes first. Re-arms when charging/docked and SoC is
    above threshold + hysteresis."""

    def __init__(self, low_soc: int = 20, hysteresis: int = 10):
        self.low_soc = low_soc
        self.hyst = hysteresis
        self.latched = False

    def update(self, soc: int, charging: bool, docked: bool, ts: float,
               mcu_low: bool = False) -> List[dict]:
        if self.latched:
            if (charging or docked) and soc >= self.low_soc + self.hyst:
                self.latched = False
            return []
        if charging or docked:
            return []
        if mcu_low or (0 < soc <= self.low_soc):
            self.latched = True
            return [ev('battery_low', ts, soc=int(soc))]
        return []


class RateLimiter:
    def __init__(self, period_s: float = 5.0):
        self.period = period_s
        self._last: Dict[str, float] = {}

    def allow(self, key: str, now: float) -> bool:
        t = self._last.get(key)
        if t is not None and now - t < self.period:
            return False
        self._last[key] = now
        return True
