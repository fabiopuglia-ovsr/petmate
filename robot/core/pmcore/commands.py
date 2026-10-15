"""Command validation and routing (INTERFACES.md §4.2).

Sources of commands:
* DataChannel ``ctl`` (per WebRTC session),
* signaling ``{"type":"command","cmd":{...}}`` (cloud REST /commands).

ROS commands are validated, given an ``id`` if missing and published on ZMQ
``cmd``. DataChannel-only commands (camera, display, volume, snapshot,
record_clip, config_get, config_set) are handled by pm-core via the
:class:`LocalHandlers` interface.

Replies sent back to the originating channel (pm-core conventions, see
report — §4 does not define them except ``snapshot``):
* ``{"t":"ack","id":str,"ok":bool,"error":str|null}`` — for every command;
  ROS commands get the ack forwarded from ZMQ ``ack`` (routed back to the
  originator by ``id``); validation errors are acked immediately with
  ``ok:false``.
* ``{"t":"snapshot","jpeg_b64":...}`` (§4.2).
* ``{"t":"config","config":{...}}`` for ``config_get`` and after a successful
  ``config_set`` (also broadcast to all viewers).
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Optional, Protocol

from .config import ConfigError
from .log import get_logger, kv

log = get_logger(__name__)

# Commands forwarded to ROS (§4.2 first block).
ROS_CMDS = {
    "teleop", "feed", "feed_abort", "launch", "intake", "tease", "led", "goto",
    "mode", "estop", "play", "save_poi", "map",
}
# DataChannel-only commands (§4.2 second block).
LOCAL_CMDS = {"camera", "display", "volume", "snapshot", "record_clip", "config_get", "config_set"}

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_POI = re.compile(r"^[^\x00-\x1f]{1,64}$")


class CommandError(ValueError):
    pass


def _num(cmd: dict, key: str, lo: float, hi: float, required: bool = True, integer: bool = False, default: Any = None):
    if key not in cmd:
        if required:
            raise CommandError(f"{key} is required")
        return default
    v = cmd[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise CommandError(f"{key} must be a number")
    if integer and isinstance(v, float) and not v.is_integer():
        raise CommandError(f"{key} must be an integer")
    if not (lo <= v <= hi):
        raise CommandError(f"{key} must be in [{lo}, {hi}]")
    return int(v) if integer else v


def _enum(cmd: dict, key: str, allowed: tuple, required: bool = True, default: Any = None):
    if key not in cmd:
        if required:
            raise CommandError(f"{key} is required")
        return default
    if cmd[key] not in allowed:
        raise CommandError(f"{key} must be one of {list(allowed)}")
    return cmd[key]


def validate_ros_cmd(cmd: dict, portion_max_g: Optional[float] = None) -> dict:
    """Validate a ROS-bound command; returns a normalised copy (incl. ``id`` if given)."""
    t = cmd.get("t")
    out: dict[str, Any] = {"t": t}
    if t == "teleop":
        out["v"] = float(_num(cmd, "v", -0.5, 0.5))   # §1 teleop v_max 0.50
        out["w"] = float(_num(cmd, "w", -2.0, 2.0))   # §1 ω_max 2.0
    elif t == "feed":
        g = _num(cmd, "grams", 1, 60)
        if portion_max_g is not None and g > portion_max_g:
            raise CommandError(f"grams exceeds portion_max_g ({portion_max_g})")
        out["grams"] = g
    elif t in ("feed_abort",):
        pass
    elif t == "launch":
        out["power"] = _num(cmd, "power", 20, 70, integer=True)
        out["count"] = _num(cmd, "count", 1, 4, integer=True, required=False, default=1)
    elif t == "intake":
        out["mode"] = _enum(cmd, "mode", ("in", "out", "off"))
    elif t == "tease":
        out["pattern"] = _enum(cmd, "pattern", ("wiggle", "sweep", "random", "stop"))
        if out["pattern"] != "stop" or "duration_s" in cmd:
            out["duration_s"] = _num(cmd, "duration_s", 0, 65.535, required=out["pattern"] != "stop", default=0)
    elif t == "led":
        out["mode"] = _enum(cmd, "mode", ("solid", "breathe", "rainbow", "chase", "off"))
        if "color" in cmd:
            c = cmd["color"]
            if not isinstance(c, str) or not _HEX_COLOR.match(c):
                raise CommandError("color must be #RRGGBB")
            out["color"] = c.upper()
        elif out["mode"] in ("solid", "breathe", "chase"):
            raise CommandError("color is required")
    elif t == "goto":
        out["target"] = _enum(cmd, "target", ("dock", "pet", "poi"))
        if out["target"] == "poi":
            p = cmd.get("poi")
            if not isinstance(p, str) or not _POI.match(p):
                raise CommandError("poi is required for target=poi")
            out["poi"] = p
    elif t == "mode":
        out["mode"] = _enum(cmd, "mode", ("auto", "manual", "sleep"))
    elif t == "estop":
        e = cmd.get("engage")
        if not isinstance(e, bool):
            raise CommandError("engage must be a boolean")
        out["engage"] = e
    elif t == "play":
        out["game"] = _enum(cmd, "game", ("chase_ball", "teaser", "screen_birds", "laser_dots"))
        out["duration_s"] = _num(cmd, "duration_s", 1, 3600, required=False, default=300)
    elif t == "save_poi":
        n = cmd.get("name")
        if not isinstance(n, str) or not _POI.match(n):
            raise CommandError("name must be a non-empty string (≤64 chars)")
        out["name"] = n
    elif t == "map":
        out["action"] = _enum(cmd, "action", ("start_mapping", "stop_mapping", "save", "clear"))
    else:
        raise CommandError(f"unknown command {t!r}")
    if isinstance(cmd.get("id"), str) and cmd["id"]:
        out["id"] = cmd["id"]
    return out


class LocalHandlers(Protocol):
    async def set_camera(self, cam: str) -> None: ...
    async def set_display(self, mode: str, game: Optional[str]) -> None: ...
    async def set_volume(self, speaker: int) -> None: ...
    async def snapshot_jpeg(self) -> bytes: ...
    async def record_clip(self, seconds: float) -> dict: ...
    def config_get(self) -> dict: ...
    async def config_set(self, cfg: dict) -> dict: ...


Reply = Callable[[dict], Any]  # send JSON object back on the originating channel


class CommandRouter:
    """Routes commands; tracks pending ROS acks by ``id`` back to the originator."""

    ACK_TTL_S = 30.0

    def __init__(
        self,
        publish_cmd: Callable[[dict], dict],
        local: LocalHandlers,
        portion_max_g: Callable[[], Optional[float]] = lambda: None,
        rear_streaming_allowed: Callable[[], bool] = lambda: True,
    ):
        self._publish_cmd = publish_cmd
        self.local = local
        self._portion_max_g = portion_max_g
        self._rear_allowed = rear_streaming_allowed
        self._pending: dict[str, tuple[float, Reply]] = {}

    # ---------------------------------------------------------------- acks
    @staticmethod
    def ack(cid: Optional[str], ok: bool, error: Optional[str] = None, **extra: Any) -> dict:
        m = {"t": "ack", "id": cid, "ok": ok, "error": error}
        m.update(extra)
        return m

    async def _send(self, reply: Optional[Reply], msg: dict) -> None:
        if reply is None:
            return
        r = reply(msg)
        if asyncio.iscoroutine(r):
            await r

    def _gc(self) -> None:
        now = time.monotonic()
        for k in [k for k, (ts, _) in self._pending.items() if now - ts > self.ACK_TTL_S]:
            del self._pending[k]

    async def on_ros_ack(self, ack: dict) -> bool:
        """Route ZMQ ``ack`` to the originator. Returns True if routed."""
        cid = ack.get("id")
        entry = self._pending.pop(cid, None) if isinstance(cid, str) else None
        if entry is None:
            return False
        await self._send(entry[1], self.ack(cid, bool(ack.get("ok")), ack.get("error")))
        return True

    def drop_origin(self, reply: Reply) -> None:
        """Forget pending acks for a closed channel."""
        # bound methods are re-created on each attribute access: compare with ==
        for k in [k for k, (_, r) in self._pending.items() if r == reply]:
            del self._pending[k]

    # ---------------------------------------------------------------- handle
    async def handle(self, msg: Any, reply: Optional[Reply] = None, source: str = "dc") -> Optional[dict]:
        """Handle one command object. Returns the ZMQ-published cmd (ROS) or None."""
        if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
            await self._send(reply, self.ack(None, False, "invalid message"))
            return None
        t = msg["t"]
        cid = msg.get("id") if isinstance(msg.get("id"), str) and msg.get("id") else None

        if t in ROS_CMDS:
            try:
                cmd = validate_ros_cmd(msg, self._portion_max_g())
            except CommandError as e:
                log.info("command rejected", extra=kv(t=t, error=str(e), source=source))
                await self._send(reply, self.ack(cid, False, str(e)))
                return None
            try:
                sent = self._publish_cmd(cmd)
            except CommandError as e:  # e.g. MVP profile: no ROS stack → "not_supported"
                log.info("command not routed", extra=kv(t=t, error=str(e), source=source))
                await self._send(reply, self.ack(cid, False, str(e)))
                return None
            if t != "teleop" and reply is not None:  # don't track 10 Hz teleop acks
                self._gc()
                self._pending[sent["id"]] = (time.monotonic(), reply)
            if t != "teleop":
                log.info("cmd → ros", extra=kv(t=t, id=sent["id"], source=source))
            return sent

        if t in LOCAL_CMDS:
            cid = cid or str(uuid.uuid4())
            try:
                await self._handle_local(t, msg, cid, reply)
            except (CommandError, ConfigError) as e:
                await self._send(reply, self.ack(cid, False, str(e)))
            except Exception as e:  # noqa: BLE001
                log.exception("local command failed", extra=kv(t=t))
                await self._send(reply, self.ack(cid, False, f"internal error: {e.__class__.__name__}"))
            return None

        await self._send(reply, self.ack(cid, False, f"unknown command {t!r}"))
        return None

    async def _handle_local(self, t: str, msg: dict, cid: str, reply: Optional[Reply]) -> None:
        L = self.local
        if t == "camera":
            cam = _enum(msg, "id", ("front", "rear"))
            if cam == "rear" and not self._rear_allowed():
                raise CommandError("rear camera streaming disabled by privacy settings")
            try:
                await L.set_camera(cam)
            except CommandError as e:  # e.g. "camera_not_available" (MVP: only "front")
                await self._send(reply, self.ack(cid, False, str(e), camera=cam))
                return
            await self._send(reply, self.ack(cid, True, camera=cam))
        elif t == "display":
            mode = _enum(msg, "mode", ("owner", "face", "game"))
            game = None
            if mode == "game":
                game = _enum(msg, "game", ("birds", "fish", "mouse"), required=False, default="birds")
            await L.set_display(mode, game)
            await self._send(reply, self.ack(cid, True))
        elif t == "volume":
            vol = _num(msg, "speaker", 0, 100, integer=True)
            await L.set_volume(vol)
            await self._send(reply, self.ack(cid, True))
        elif t == "snapshot":
            jpeg = await L.snapshot_jpeg()
            await self._send(reply, {"t": "snapshot", "jpeg_b64": base64.b64encode(jpeg).decode("ascii")})
        elif t == "record_clip":
            secs = _num(msg, "seconds", 1, 60, required=False, default=15)
            info = await L.record_clip(secs)
            await self._send(reply, self.ack(cid, True, **info))
        elif t == "config_get":
            await self._send(reply, {"t": "config", "config": L.config_get()})
        elif t == "config_set":
            cfg = msg.get("config")
            if not isinstance(cfg, dict):
                raise CommandError("config must be an object")
            new = await L.config_set(cfg)
            await self._send(reply, self.ack(cid, True))
            await self._send(reply, {"t": "config", "config": new})
