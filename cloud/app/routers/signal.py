"""Signaling WebSocket `/v1/signal?token=<JWT>` (INTERFACES.md §6)."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ..hub import Connection, SignalingHub, device_peer, parse_peer
from ..models import Device, User
from ..security import decode_token
from ..services import touch_device

log = logging.getLogger("petmate.signal")
router = APIRouter()

RELAY_TYPES = {"offer", "answer", "ice", "bye"}
MAX_MSG_BYTES = 64 * 1024
LAST_SEEN_MIN_INTERVAL_S = 60


def _db_call(app, fn, *args):
    def run():
        s = app.state.db.session()
        try:
            return fn(s, *args)
        finally:
            s.close()

    return asyncio.to_thread(run)


def _device_owner(s, device_id: str) -> Optional[int]:
    return s.scalar(select(Device.owner_id).where(Device.device_id == device_id))


def _user_exists(s, user_id: int) -> bool:
    return s.get(User, user_id) is not None


def _owned_device_ids(s, user_id: int) -> list[str]:
    return list(s.scalars(select(Device.device_id).where(Device.owner_id == user_id)))


async def _authorized(app, src_kind: str, src_id: str, dst: str) -> bool:
    parsed = parse_peer(dst)
    if parsed is None:
        return False
    dst_kind, dst_id, _ = parsed
    if src_kind == "user" and dst_kind == "device":
        owner = await _db_call(app, _device_owner, dst_id)
        return owner is not None and str(owner) == src_id
    if src_kind == "device" and dst_kind == "user":
        owner = await _db_call(app, _device_owner, src_id)
        return owner is not None and str(owner) == dst_id
    return False


@router.websocket("/signal")
async def signal(ws: WebSocket):
    app = ws.app
    settings = app.state.settings
    hub: SignalingHub = app.state.hub

    token = ws.query_params.get("token")
    if not token:
        # also accept Authorization header (non-browser clients such as pm-core)
        h = ws.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            token = h[7:].strip()
    principal = decode_token(settings.jwt_secret, token) if token else None

    await ws.accept()
    if principal is None:
        await ws.send_json({"type": "error", "error": "unauthorized"})
        await ws.close(code=4401)
        return

    user_id: Optional[int] = None
    device_id: Optional[str] = None
    if principal.typ == "device":
        device_id = principal.device_id
        owner = await _db_call(app, _device_owner, device_id)
        if owner is None:
            await ws.send_json({"type": "error", "error": "device_unclaimed"})
            await ws.close(code=4403)
            return
        peer = device_peer(device_id)
        src_kind, src_id = "device", device_id
    else:
        user_id = principal.user_id
        if not await _db_call(app, _user_exists, user_id):
            await ws.send_json({"type": "error", "error": "unauthorized"})
            await ws.close(code=4401)
            return
        peer = f"user:{user_id}:{uuid.uuid4().hex}"
        src_kind, src_id = "user", str(user_id)

    async def _close(code: int) -> None:
        await ws.close(code=code)

    conn = Connection(peer, ws.send_json, closer=_close)
    conn.send({"type": "hello", "peer": peer})
    old = await hub.register(conn, user_id=user_id, device_id=device_id)
    if old is not None:
        old.kick("replaced by a new connection", 4409)

    if device_id is not None:
        await _db_call(app, touch_device, device_id)
        hub.notify_user(owner, {"type": "presence", "device_id": device_id, "online": True})
    else:
        for did in await _db_call(app, _owned_device_ids, user_id):
            conn.send({"type": "presence", "device_id": did, "online": hub.is_device_online(did)})

    log.info("connected %s", peer)
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MSG_BYTES:
                conn.send({"type": "error", "error": "message too large"})
                continue
            try:
                msg: Any = json.loads(raw)
            except json.JSONDecodeError:
                conn.send({"type": "error", "error": "invalid json"})
                continue
            if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                conn.send({"type": "error", "error": "missing type"})
                continue
            mtype = msg["type"]

            if mtype == "ping":
                conn.send({"type": "pong"})
                if device_id is not None:
                    await _db_call(app, touch_device, device_id, None, LAST_SEEN_MIN_INTERVAL_S)
                continue
            if mtype == "pong":
                continue
            if mtype not in RELAY_TYPES:
                conn.send({"type": "error", "error": f"unsupported type: {mtype}"})
                continue

            to = msg.get("to")
            if not isinstance(to, str) or not await _authorized(app, src_kind, src_id, to):
                conn.send({"type": "error", "error": "forbidden", "to": to, "ref": mtype})
                continue
            out = dict(msg)
            out["from"] = peer
            if not hub.deliver(to, out):
                conn.send({"type": "error", "error": "peer_offline", "to": to, "ref": mtype})
                continue
            if mtype == "bye":
                conn.contacts.discard(to)
            else:
                conn.contacts.add(to)
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:  # socket closed by kick()
        log.debug("ws %s runtime error: %s", peer, exc)
    finally:
        # Notifications first and without awaiting I/O: the task may be cancelled
        # right after the disconnect.
        was_active = await hub.unregister(conn)
        for other in list(conn.contacts):
            hub.deliver(other, {"type": "bye", "to": other, "from": peer})
        if device_id is not None and was_active:
            hub.notify_user(owner, {"type": "presence", "device_id": device_id, "online": False})
        log.info("disconnected %s", peer)
        try:
            await conn.aclose()
            if device_id is not None and was_active:
                await _db_call(app, touch_device, device_id)
        except BaseException:  # cancelled during shutdown: best effort only
            pass
