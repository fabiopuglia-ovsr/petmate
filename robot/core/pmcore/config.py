"""Device configuration store (INTERFACES.md §5).

* ``default_config()`` returns the §5 example verbatim (it doubles as factory
  defaults).
* ``validate_config(cfg)`` fills missing fields from defaults, type-checks and
  range-checks every field and returns a normalised deep copy, or raises
  :class:`ConfigError` with a dotted path to the offending field. Unknown keys
  are dropped (and reported via ``ConfigError.warnings``-style list on the
  store) so that a newer app cannot corrupt the stored document.
* ``ConfigStore`` persists atomically to ``/var/lib/petmate/config.json`` and
  notifies listeners on change. ``apply()`` accepts a full or partial config
  (deep-merged onto the current one) as sent in ``config_set``.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from .log import get_logger, kv
from .util import atomic_write_json, read_json

log = get_logger(__name__)

CONFIG_VERSION = 1

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_POI_RE = re.compile(r"^[^\x00-\x1f]{1,64}$")

SPECIES = ("cat", "dog")
GAMES = ("chase_ball", "teaser", "screen_birds", "laser_dots")  # §4.2 "play"


def default_config() -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "pet": {"name": "Micio", "species": "cat", "weight_kg": 4.5},
        "feeding": {
            "schedule": [{"time": "08:00", "grams": 25, "days": [1, 2, 3, 4, 5, 6, 7]}],
            "portion_max_g": 60,
            "daily_max_g": 120,
            "feeding_poi": "cucina",
        },
        "play": {
            "auto_play": True,
            "sessions": [{"time": "14:00", "duration_min": 10, "days": [1, 2, 3, 4, 5, 6, 7]}],
            "launch_power": 40,
            "games": ["chase_ball", "teaser", "screen_birds"],
            "quiet_hours": {"start": "22:00", "end": "07:00"},
        },
        "patrol": {"enabled": False, "interval_min": 120},
        "notifications": {"meals": True, "activity": True, "battery": True, "pet_detected": False},
        "privacy": {"stream_led": True, "rear_camera_streaming": True},
        "audio": {"speaker_volume": 70},
        "timezone": "Europe/Rome",
    }


class ConfigError(ValueError):
    def __init__(self, path: str, msg: str):
        super().__init__(f"{path}: {msg}")
        self.path = path
        self.msg = msg


# --------------------------------------------------------------------------
# primitive validators
# --------------------------------------------------------------------------

def _bool(v: Any, p: str) -> bool:
    if not isinstance(v, bool):
        raise ConfigError(p, "must be a boolean")
    return v


def _num(v: Any, p: str, lo: float, hi: float, integer: bool = False) -> float | int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(p, "must be a number")
    if v != v:  # NaN
        raise ConfigError(p, "must not be NaN")
    if integer:
        if isinstance(v, float):
            if not v.is_integer():
                raise ConfigError(p, "must be an integer")
            v = int(v)
    if not (lo <= v <= hi):
        raise ConfigError(p, f"must be in [{lo}, {hi}]")
    return v


def _str(v: Any, p: str, max_len: int = 64, min_len: int = 0) -> str:
    if not isinstance(v, str):
        raise ConfigError(p, "must be a string")
    if not (min_len <= len(v) <= max_len):
        raise ConfigError(p, f"length must be in [{min_len}, {max_len}]")
    return v


def _hhmm(v: Any, p: str) -> str:
    v = _str(v, p, 5, 5)
    if not _TIME_RE.match(v):
        raise ConfigError(p, "must be HH:MM (24h)")
    return v


def _enum(v: Any, p: str, allowed: Iterable[str]) -> str:
    allowed = tuple(allowed)
    if v not in allowed:
        raise ConfigError(p, f"must be one of {list(allowed)}")
    return v


def _days(v: Any, p: str) -> list[int]:
    if not isinstance(v, list) or not v:
        raise ConfigError(p, "must be a non-empty list of ISO weekdays 1..7")
    out = sorted({int(_num(d, f"{p}[{i}]", 1, 7, integer=True)) for i, d in enumerate(v)})
    return out


def _obj(v: Any, p: str) -> dict:
    if not isinstance(v, dict):
        raise ConfigError(p, "must be an object")
    return v


def _timezone(v: Any, p: str) -> str:
    v = _str(v, p, 64, 1)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(p, "unknown IANA timezone")
    except ImportError:  # pragma: no cover
        pass
    return v


# --------------------------------------------------------------------------
# merge + validate
# --------------------------------------------------------------------------

def deep_merge(base: dict, patch: dict) -> dict:
    """Return base updated recursively by patch (lists are replaced)."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _filled(section: str, cfg: dict) -> dict:
    """Section value with missing keys filled from defaults."""
    d = default_config()[section]
    v = _obj(cfg.get(section, d), section)
    return deep_merge(d, v) if isinstance(d, dict) else v


def validate_config(cfg: Any, dropped: list[str] | None = None) -> dict[str, Any]:
    """Validate and normalise a full config (missing fields → defaults)."""
    cfg = _obj(cfg, "$")
    defaults = default_config()
    if dropped is not None:
        for k in cfg:
            if k not in defaults:
                dropped.append(k)

    version = cfg.get("version", CONFIG_VERSION)
    version = int(_num(version, "version", 1, CONFIG_VERSION, integer=True))

    pet = _filled("pet", cfg)
    out_pet = {
        "name": _str(pet["name"], "pet.name", 32, 1),
        "species": _enum(pet["species"], "pet.species", SPECIES),
        "weight_kg": float(_num(pet["weight_kg"], "pet.weight_kg", 0.5, 80.0)),
    }

    fd = _filled("feeding", cfg)
    schedule = fd["schedule"]
    if not isinstance(schedule, list) or len(schedule) > 12:
        raise ConfigError("feeding.schedule", "must be a list with at most 12 entries")
    portion_max = int(_num(fd["portion_max_g"], "feeding.portion_max_g", 1, 60, integer=True))
    daily_max = int(_num(fd["daily_max_g"], "feeding.daily_max_g", 1, 1000, integer=True))
    out_sched = []
    for i, e in enumerate(schedule):
        p = f"feeding.schedule[{i}]"
        e = _obj(e, p)
        for req in ("time", "grams"):
            if req not in e:
                raise ConfigError(f"{p}.{req}", "is required")
        grams = _num(e["grams"], f"{p}.grams", 1, 60)  # §4.2 feed 1..60
        if grams > portion_max:
            raise ConfigError(f"{p}.grams", f"exceeds portion_max_g ({portion_max})")
        out_sched.append({
            "time": _hhmm(e["time"], f"{p}.time"),
            "grams": int(grams) if float(grams).is_integer() else float(grams),
            "days": _days(e.get("days", [1, 2, 3, 4, 5, 6, 7]), f"{p}.days"),
        })
    # Daily sum must not exceed daily_max_g for any weekday.
    for day in range(1, 8):
        tot = sum(e["grams"] for e in out_sched if day in e["days"])
        if tot > daily_max:
            raise ConfigError("feeding.schedule", f"total for weekday {day} ({tot} g) exceeds daily_max_g ({daily_max})")
    # The app serialises a cleared feeding POI as null: normalise to "".
    poi = _str(fd["feeding_poi"] if fd["feeding_poi"] is not None else "", "feeding.feeding_poi", 64, 0)
    if poi and not _POI_RE.match(poi):
        raise ConfigError("feeding.feeding_poi", "invalid POI name")
    out_feeding = {
        "schedule": out_sched,
        "portion_max_g": portion_max,
        "daily_max_g": daily_max,
        "feeding_poi": poi,
    }

    pl = _filled("play", cfg)
    sessions = pl["sessions"]
    if not isinstance(sessions, list) or len(sessions) > 12:
        raise ConfigError("play.sessions", "must be a list with at most 12 entries")
    out_sessions = []
    for i, e in enumerate(sessions):
        p = f"play.sessions[{i}]"
        e = _obj(e, p)
        for req in ("time", "duration_min"):
            if req not in e:
                raise ConfigError(f"{p}.{req}", "is required")
        out_sessions.append({
            "time": _hhmm(e["time"], f"{p}.time"),
            "duration_min": int(_num(e["duration_min"], f"{p}.duration_min", 1, 60, integer=True)),
            "days": _days(e.get("days", [1, 2, 3, 4, 5, 6, 7]), f"{p}.days"),
        })
    games = pl["games"]
    if not isinstance(games, list):
        raise ConfigError("play.games", "must be a list")
    out_games: list[str] = []
    for i, g in enumerate(games):
        g = _enum(g, f"play.games[{i}]", GAMES)
        if g not in out_games:
            out_games.append(g)
    qh = _obj(pl["quiet_hours"], "play.quiet_hours")
    qh = deep_merge(default_config()["play"]["quiet_hours"], qh)
    out_play = {
        "auto_play": _bool(pl["auto_play"], "play.auto_play"),
        "sessions": out_sessions,
        "launch_power": int(_num(pl["launch_power"], "play.launch_power", 20, 70, integer=True)),  # §2 clamp 20–70
        "games": out_games,
        "quiet_hours": {
            "start": _hhmm(qh["start"], "play.quiet_hours.start"),
            "end": _hhmm(qh["end"], "play.quiet_hours.end"),
        },
    }

    pa = _filled("patrol", cfg)
    out_patrol = {
        "enabled": _bool(pa["enabled"], "patrol.enabled"),
        "interval_min": int(_num(pa["interval_min"], "patrol.interval_min", 10, 1440, integer=True)),
    }

    no = _filled("notifications", cfg)
    out_notif = {k: _bool(no[k], f"notifications.{k}") for k in ("meals", "activity", "battery", "pet_detected")}

    pr = _filled("privacy", cfg)
    out_priv = {k: _bool(pr[k], f"privacy.{k}") for k in ("stream_led", "rear_camera_streaming")}

    au = _filled("audio", cfg)
    out_audio = {"speaker_volume": int(_num(au["speaker_volume"], "audio.speaker_volume", 0, 100, integer=True))}

    tz = _timezone(cfg.get("timezone", defaults["timezone"]), "timezone")

    for section, d in defaults.items():
        if isinstance(d, dict) and isinstance(cfg.get(section), dict) and dropped is not None:
            for k in cfg[section]:
                if k not in d:
                    dropped.append(f"{section}.{k}")

    return {
        "version": version,
        "pet": out_pet,
        "feeding": out_feeding,
        "play": out_play,
        "patrol": out_patrol,
        "notifications": out_notif,
        "privacy": out_priv,
        "audio": out_audio,
        "timezone": tz,
    }


Listener = Callable[[dict, dict], Any]  # (new, old)


class ConfigStore:
    """Persistent, validated configuration with change listeners."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._cfg: dict[str, Any] = default_config()
        self._listeners: list[Listener] = []

    @property
    def data(self) -> dict[str, Any]:
        return copy.deepcopy(self._cfg)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._cfg
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return copy.deepcopy(cur)

    def on_change(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def load(self) -> dict[str, Any]:
        """Load from disk; on missing/corrupt/invalid file fall back to defaults and persist."""
        try:
            raw = read_json(self.path)
            dropped: list[str] = []
            self._cfg = validate_config(raw, dropped)
            if dropped:
                log.warning("config: dropped unknown keys", extra=kv(keys=dropped))
            if raw != self._cfg:
                self.save()
        except FileNotFoundError:
            log.info("config: not found, writing defaults", extra=kv(path=str(self.path)))
            self._cfg = default_config()
            self.save()
        except (ValueError, OSError) as e:
            bad = self.path.with_suffix(".json.bad")
            log.error("config: invalid, resetting to defaults", extra=kv(error=str(e), backup=str(bad)))
            try:
                self.path.replace(bad)
            except OSError:
                pass
            self._cfg = default_config()
            self.save()
        return self.data

    def save(self) -> None:
        atomic_write_json(self.path, self._cfg, mode=0o644)

    def apply(self, patch: dict[str, Any], replace: bool = False) -> tuple[dict, bool]:
        """Merge (or replace) and validate. Returns (new_config, changed).

        Raises ConfigError without modifying state if the result is invalid.
        """
        if not isinstance(patch, dict):
            raise ConfigError("$", "must be an object")
        candidate = patch if replace else deep_merge(self._cfg, patch)
        dropped: list[str] = []
        new = validate_config(candidate, dropped)
        if dropped:
            log.warning("config_set: dropped unknown keys", extra=kv(keys=dropped))
        old = self._cfg
        if new == old:
            return self.data, False
        self._cfg = new
        self.save()
        for fn in list(self._listeners):
            try:
                fn(copy.deepcopy(new), copy.deepcopy(old))
            except Exception:  # noqa: BLE001
                log.exception("config listener failed")
        return self.data, True
