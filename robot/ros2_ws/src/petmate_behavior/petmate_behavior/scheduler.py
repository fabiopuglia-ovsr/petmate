"""Pure, timezone-aware scheduler for meals and play sessions (INTERFACES.md §5).

No ROS dependency. The clock is injected (``now_fn`` returns an aware datetime
or a POSIX timestamp) so the logic is fully unit-testable.

Policy (documented for other teams):
* A scheduled slot fires once per local calendar day, if ``now`` falls within
  ``[slot, slot + grace)`` and the ISO weekday is in ``days``. Slots missed by
  more than the grace window (robot off, busy, sleeping) are reported once as
  ``missed`` and not executed late.
* Meals: portion is clamped to ``portion_max_g`` and to what remains of
  ``daily_max_g`` today (local day). If nothing remains the slot is reported
  as ``daily_max_reached``. Meals are NOT blocked by quiet hours (the owner
  scheduled them explicitly); quiet hours only gate autonomous play/patrol.
* Play sessions require ``play.auto_play`` and are skipped during quiet hours.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Tuple, Union

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

DEFAULT_TZ = 'Europe/Rome'


def get_tz(name: Optional[str]):
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:  # noqa: BLE001
        return ZoneInfo('UTC')


def parse_hhmm(s: str) -> dt.time:
    h, m = s.strip().split(':')
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f'bad time {s!r}')
    return dt.time(h, m)


def in_quiet_hours(local: dt.datetime, quiet: Optional[dict]) -> bool:
    """True if ``local`` (aware, local tz) is inside [start, end). Handles
    windows crossing midnight. start == end means no quiet hours."""
    if not quiet:
        return False
    try:
        start = parse_hhmm(quiet['start'])
        end = parse_hhmm(quiet['end'])
    except (KeyError, ValueError, AttributeError):
        return False
    t = local.time().replace(second=0, microsecond=0, tzinfo=None)
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


@dataclass
class Due:
    kind: str                 # 'meal' | 'play'
    key: str                  # unique per slot per day, e.g. 'meal|2026-10-14|08:00'
    slot_time: str
    grams: float = 0.0        # meals: effective grams after clamping
    requested_g: float = 0.0
    duration_s: float = 0.0   # play
    skip_reason: Optional[str] = None   # set => do not execute; emit failure/skip

    @property
    def runnable(self) -> bool:
        return self.skip_reason is None


@dataclass
class SchedulerState:
    """Persistable state (JSON-friendly)."""
    day: str = ''                       # local date ISO of today_g
    today_g: float = 0.0
    last_meal: Optional[dict] = None    # {"ts": float, "grams": float}
    handled: List[str] = field(default_factory=list)   # slot keys done/skipped
    play_index: int = 0                 # rotates through play.games

    def to_dict(self) -> dict:
        return {'day': self.day, 'today_g': self.today_g, 'last_meal': self.last_meal,
                'handled': list(self.handled), 'play_index': self.play_index}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> 'SchedulerState':
        d = d or {}
        return cls(day=str(d.get('day', '')), today_g=float(d.get('today_g', 0.0)),
                   last_meal=d.get('last_meal'), handled=list(d.get('handled', [])),
                   play_index=int(d.get('play_index', 0)))


NowFn = Callable[[], Union[dt.datetime, float]]


class Scheduler:
    def __init__(self, config: Optional[dict] = None, now_fn: Optional[NowFn] = None,
                 state: Optional[SchedulerState] = None, grace_s: float = 900.0):
        self._now_fn = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
        self.state = state or SchedulerState()
        self.grace = dt.timedelta(seconds=grace_s)
        self.config: dict = {}
        self.tz = get_tz(DEFAULT_TZ)
        self.set_config(config or {})

    # ---------------------------------------------------------------- config / clock
    def set_config(self, config: dict):
        self.config = config or {}
        self.tz = get_tz(self.config.get('timezone'))

    def now_local(self) -> dt.datetime:
        n = self._now_fn()
        if isinstance(n, (int, float)):
            n = dt.datetime.fromtimestamp(float(n), dt.timezone.utc)
        if n.tzinfo is None:
            n = n.replace(tzinfo=dt.timezone.utc)
        return n.astimezone(self.tz)

    @property
    def feeding(self) -> dict:
        return self.config.get('feeding') or {}

    @property
    def play(self) -> dict:
        return self.config.get('play') or {}

    def quiet_now(self) -> bool:
        return in_quiet_hours(self.now_local(), self.play.get('quiet_hours'))

    # ---------------------------------------------------------------- daily accounting
    def _rollover(self, local: dt.datetime):
        today = local.date().isoformat()
        if self.state.day != today:
            self.state.day = today
            self.state.today_g = 0.0
            # keep only keys for today (bounded list)
            self.state.handled = [k for k in self.state.handled if f'|{today}|' in k]

    @property
    def today_g(self) -> float:
        self._rollover(self.now_local())
        return self.state.today_g

    def remaining_g(self) -> float:
        daily_max = float(self.feeding.get('daily_max_g', 0) or 0)
        if daily_max <= 0:
            return float('inf')
        return max(0.0, daily_max - self.today_g)

    def clamp_portion(self, grams: float) -> Tuple[float, Optional[str]]:
        """Return (effective_grams, reason_if_zero)."""
        if grams <= 0:
            return 0.0, 'invalid_grams'
        g = float(grams)
        pmax = float(self.feeding.get('portion_max_g', 60) or 60)
        g = min(g, pmax, 60.0)
        rem = self.remaining_g()
        if rem <= 0.0:
            return 0.0, 'daily_max_reached'
        g = min(g, rem)
        if g < 1.0:
            return 0.0, 'daily_max_reached'
        return round(g, 1), None

    def record_meal(self, grams: float, ts: Optional[float] = None):
        local = self.now_local()
        self._rollover(local)
        self.state.today_g = round(self.state.today_g + max(0.0, float(grams)), 2)
        self.state.last_meal = {'ts': float(ts if ts is not None else local.timestamp()),
                                'grams': round(float(grams), 1)}

    def mark_handled(self, key: str):
        if key not in self.state.handled:
            self.state.handled.append(key)

    # ---------------------------------------------------------------- slots
    @staticmethod
    def _days_ok(entry: dict, local: dt.datetime) -> bool:
        days = entry.get('days') or [1, 2, 3, 4, 5, 6, 7]
        return local.isoweekday() in [int(d) for d in days]

    def _slot_dt(self, local: dt.datetime, hhmm: str) -> dt.datetime:
        t = parse_hhmm(hhmm)
        naive = dt.datetime.combine(local.date(), t)
        # zoneinfo handles DST; nonexistent local times shift forward by fold rules
        return naive.replace(tzinfo=self.tz)

    def _iter_slots(self, kind: str, entries: Iterable[dict], local: dt.datetime):
        for e in entries or []:
            try:
                slot = self._slot_dt(local, e['time'])
            except (KeyError, ValueError, AttributeError):
                continue
            if not self._days_ok(e, local):
                continue
            key = f"{kind}|{local.date().isoformat()}|{slot.strftime('%H:%M')}"
            yield e, slot, key

    def poll(self) -> List[Due]:
        """Return slots that became due (or definitively missed) since last poll.

        Callers MUST call ``mark_handled(due.key)`` once they have acted on a
        Due (executed it or reported the skip); un-handled runnable slots are
        returned again on the next poll while inside the grace window, which
        lets the caller defer when busy.
        """
        local = self.now_local()
        self._rollover(local)
        out: List[Due] = []

        for e, slot, key in self._iter_slots('meal', self.feeding.get('schedule'), local):
            if key in self.state.handled or local < slot:
                continue
            req = float(e.get('grams', 0) or 0)
            if local >= slot + self.grace:
                out.append(Due('meal', key, e['time'], requested_g=req, skip_reason='missed'))
                continue
            g, reason = self.clamp_portion(req)
            out.append(Due('meal', key, e['time'], grams=g, requested_g=req, skip_reason=reason))

        play = self.play
        for e, slot, key in self._iter_slots('play', play.get('sessions'), local):
            if key in self.state.handled or local < slot:
                continue
            dur = float(e.get('duration_min', 10) or 10) * 60.0
            reason = None
            if not play.get('auto_play', False):
                reason = 'auto_play_disabled'
            elif local >= slot + self.grace:
                reason = 'missed'
            elif in_quiet_hours(local, play.get('quiet_hours')):
                reason = 'quiet_hours'
            out.append(Due('play', key, e['time'], duration_s=dur, skip_reason=reason))
        return out

    def next_game(self) -> Optional[str]:
        games = [g for g in (self.play.get('games') or []) if g]
        if not games:
            return None
        g = games[self.state.play_index % len(games)]
        self.state.play_index = (self.state.play_index + 1) % len(games)
        return g

    def patrol_due(self, last_patrol_ts: float) -> bool:
        p = self.config.get('patrol') or {}
        if not p.get('enabled'):
            return False
        if self.quiet_now():
            return False
        interval = float(p.get('interval_min', 120) or 120) * 60.0
        return self.now_local().timestamp() - last_patrol_ts >= interval
