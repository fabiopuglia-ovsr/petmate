from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import (APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, Query,
                     Request, Response, UploadFile)
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_principal, owned_device, require_device, require_user, self_device
from ..hub import SignalingHub
from ..models import Clip, Device, DeviceConfig, Event, PushToken, User
from ..push import PushSender, render_event, should_notify
from ..ratelimit import limit
from ..schemas import (COMMAND_TYPES, EVENT_TYPES, DeviceOut, DevicePatch, DeviceRegisterIn,
                       DeviceRegisterOut, DeviceTokenIn, DeviceTokenOut, EventIn, PushTokenIn)
from ..security import Principal, hash_secret, issue_token, verify_secret
from ..services import (ClaimError, consume_claim_token, ice_servers, iso, purge_device_data,
                        upsert_push_token, utcnow, verify_factory_secret)

log = logging.getLogger("petmate.api")
router = APIRouter()


def _iso_ts(ts: Optional[float]) -> Optional[str]:
    """Epoch seconds (as stored) -> ISO 8601 UTC for REST output (§9.5)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat().replace("+00:00", "Z")


def _hub(request: Request) -> SignalingHub:
    return request.app.state.hub


def _device_out(dev: Device, hub: SignalingHub) -> DeviceOut:
    return DeviceOut(
        device_id=dev.device_id,
        name=dev.name,
        online=hub.is_device_online(dev.device_id),
        last_seen=iso(dev.last_seen),
        fw=dev.fw,
    )


# ------------------------------------------------------------------ device auth

@router.post("/devices/register", response_model=DeviceRegisterOut, status_code=201,
             dependencies=[Depends(limit("device_auth", "rate_device_auth"))])
def register_device(body: DeviceRegisterIn, request: Request, db: Session = Depends(get_db)):
    s = request.app.state.settings
    if not verify_factory_secret(db, s, body.device_id, body.device_secret):
        db.rollback()
        raise HTTPException(401, "unknown device or bad secret")
    try:
        owner_id = consume_claim_token(db, body.claim_token, body.device_id)
    except ClaimError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    if db.get(User, owner_id) is None:
        db.rollback()
        raise HTTPException(400, "claim token owner no longer exists")

    dev = db.get(Device, body.device_id)
    previous_owner = dev.owner_id if dev else None
    if dev is None:
        dev = Device(device_id=body.device_id, secret_hash=hash_secret(body.device_secret))
        db.add(dev)
    else:
        dev.secret_hash = hash_secret(body.device_secret)
    if previous_owner is not None and previous_owner != owner_id:
        # ownership transfer: previous owner's private data must not leak to the new one
        purge_device_data(db, dev.device_id, request.app.state.storage)
        dev.name = "Petmate"
    dev.owner_id = owner_id
    dev.claimed_at = utcnow()
    dev.last_seen = utcnow()
    if body.fw:
        dev.fw = body.fw
    db.commit()

    hub = _hub(request)
    if previous_owner is not None and previous_owner != owner_id:
        hub.notify_user(previous_owner, {"type": "presence", "device_id": dev.device_id, "online": False})
        hub.kick(f"device:{dev.device_id}", "device re-claimed", 4401)

    token = issue_token(s.jwt_secret, "device", dev.device_id, s.jwt_ttl_s)
    return DeviceRegisterOut(device_token=token, owner_user_id=owner_id)


@router.post("/devices/token", response_model=DeviceTokenOut,
             dependencies=[Depends(limit("device_auth", "rate_device_auth"))])
def device_token(body: DeviceTokenIn, request: Request, db: Session = Depends(get_db)):
    s = request.app.state.settings
    dev = db.get(Device, body.device_id)
    if dev is None or not verify_secret(body.device_secret, dev.secret_hash):
        raise HTTPException(401, "unknown device or bad secret")
    if dev.owner_id is None:
        # Device was removed by its owner: pm-core should go back to BLE provisioning.
        raise HTTPException(403, "device_unclaimed")
    if body.fw:
        dev.fw = body.fw
    dev.last_seen = utcnow()
    db.commit()
    return DeviceTokenOut(device_token=issue_token(s.jwt_secret, "device", dev.device_id, s.jwt_ttl_s))


# ------------------------------------------------------------------ device management (user)

@router.get("/devices", response_model=list[DeviceOut])
def list_devices(request: Request, user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    devs = db.scalars(select(Device).where(Device.owner_id == user.user_id).order_by(Device.claimed_at))
    hub = _hub(request)
    return [_device_out(d, hub) for d in devs]


@router.patch("/devices/{device_id}", response_model=DeviceOut)
def patch_device(device_id: str, body: DevicePatch, request: Request,
                 user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    dev = owned_device(device_id, user, db)
    dev.name = body.name.strip()
    db.commit()
    return _device_out(dev, _hub(request))


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: str, request: Request,
                  user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    dev = owned_device(device_id, user, db)
    purge_device_data(db, device_id, request.app.state.storage)
    dev.owner_id = None
    dev.claimed_at = None
    dev.name = "Petmate"
    db.commit()
    hub = _hub(request)
    hub.notify_user(user.user_id, {"type": "presence", "device_id": device_id, "online": False})
    hub.kick(f"device:{device_id}", "device unclaimed", 4401)
    return Response(status_code=204)


# ------------------------------------------------------------------ ICE servers

@router.get("/ice-servers")
def get_ice_servers(request: Request, p: Principal = Depends(get_principal)):
    return ice_servers(request.app.state.settings, p.sub)


# ------------------------------------------------------------------ config

def _cfg_row(db: Session, device_id: str) -> DeviceConfig:
    row = db.get(DeviceConfig, device_id)
    if row is None:
        row = DeviceConfig(device_id=device_id)
        db.add(row)
    return row


@router.get("/devices/{device_id}/config")
def get_config(device_id: str, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    if p.typ == "user":
        owned_device(device_id, p, db)
    else:
        self_device(device_id, p, db)
    row = db.get(DeviceConfig, device_id)
    if row is None or row.desired is None:
        raise HTTPException(404, "no desired config")
    return row.desired


@router.put("/devices/{device_id}/config")
def put_config(device_id: str, request: Request, response: Response,
               config: dict[str, Any] = Body(...),
               user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    owned_device(device_id, user, db)
    row = _cfg_row(db, device_id)
    row.desired = config
    row.desired_updated_at = utcnow()
    db.commit()
    delivered = _hub(request).send_to_device(device_id, {"type": "config_set", "config": config})
    response.headers["X-Petmate-Delivered"] = "true" if delivered else "false"
    return config


@router.post("/devices/{device_id}/config/reported", status_code=204)
def post_reported(device_id: str, config: dict[str, Any] = Body(...),
                  p: Principal = Depends(require_device), db: Session = Depends(get_db)):
    self_device(device_id, p, db)
    row = _cfg_row(db, device_id)
    row.reported = config
    row.reported_updated_at = utcnow()
    db.commit()
    return Response(status_code=204)


@router.get("/devices/{device_id}/config/reported")
def get_reported(device_id: str, user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """Extension (not in §6): lets the app show the config actually applied by the robot."""
    owned_device(device_id, user, db)
    row = db.get(DeviceConfig, device_id)
    if row is None or row.reported is None:
        raise HTTPException(404, "no reported config")
    return row.reported


# ------------------------------------------------------------------ commands

@router.post("/devices/{device_id}/commands", status_code=202)
def post_command(device_id: str, request: Request, cmd: dict[str, Any] = Body(...),
                 user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    owned_device(device_id, user, db)
    t = cmd.get("t")
    if not isinstance(t, str) or t not in COMMAND_TYPES:
        raise HTTPException(422, f"unknown command type: {t!r}")
    cmd = dict(cmd)
    cmd.setdefault("id", str(uuid.uuid4()))
    hub = _hub(request)
    if not hub.is_device_online(device_id):
        raise HTTPException(409, "device offline")
    if not hub.send_to_device(device_id, {"type": "command", "cmd": cmd}):
        raise HTTPException(409, "device offline")
    return {"id": cmd["id"], "delivered": True}


# ------------------------------------------------------------------ events

async def _push_event(app, device_id: str, t: str, data: dict[str, Any]) -> None:
    db: Session = app.state.db.session()
    try:
        dev = db.get(Device, device_id)
        if dev is None or dev.owner_id is None:
            return
        row = db.get(DeviceConfig, device_id)
        cfg = (row.desired or row.reported) if row else None
        if not should_notify(t, cfg):
            return
        pet_name = None
        if cfg and isinstance(cfg.get("pet"), dict):
            pet_name = cfg["pet"].get("name")
        note = render_event(t, data, dev.name, pet_name)
        if note is None:
            return
        note.data["device_id"] = device_id
        tokens = [(pt.token, pt.platform) for pt in db.scalars(select(PushToken).where(PushToken.user_id == dev.owner_id))]
        if not tokens:
            return
        sender: PushSender = app.state.push
        invalid = await sender.send(tokens, note)
        if invalid:
            for pt in db.scalars(select(PushToken).where(PushToken.token.in_(invalid))):
                db.delete(pt)
            db.commit()
    except Exception:  # never fail the request because of push
        log.exception("push for event %s failed", t)
    finally:
        db.close()


@router.post("/devices/{device_id}/events", status_code=201)
def post_event(device_id: str, body: EventIn, request: Request, background: BackgroundTasks,
               p: Principal = Depends(require_device), db: Session = Depends(get_db)):
    dev = self_device(device_id, p, db)
    if dev.owner_id is None:
        raise HTTPException(403, "device_unclaimed")
    if body.t not in EVENT_TYPES:
        log.info("unknown event type %r from %s (stored anyway)", body.t, device_id)
    data = body.payload()
    ev = Event(device_id=device_id, t=body.t, ts=body.ts if body.ts is not None else time.time(), data=data)
    db.add(ev)
    db.commit()
    background.add_task(_push_event, request.app, device_id, body.t, data)
    return {"id": ev.id}


@router.get("/devices/{device_id}/events")
def list_events(device_id: str, limit: int = Query(50, ge=1, le=500),
                before: Optional[float] = Query(None, description="only events with ts < before"),
                user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    owned_device(device_id, user, db)
    q = select(Event).where(Event.device_id == device_id)
    if before is not None:
        q = q.where(Event.ts < before)
    q = q.order_by(Event.ts.desc(), Event.id.desc()).limit(limit)
    out = []
    for e in db.scalars(q):
        item = {k: v for k, v in (e.data or {}).items() if k not in ("id", "t", "ts")}
        item.update({"id": e.id, "t": e.t, "ts": _iso_ts(e.ts)})
        out.append(item)
    return out


# ------------------------------------------------------------------ clips

def _clip_urls(request: Request, clip: Clip) -> tuple[str, Optional[str]]:
    base = request.app.state.settings.public_base_url
    prefix = f"{base}/v1" if base else "/v1"
    url = f"{prefix}/clips/{clip.id}"
    thumb = f"{prefix}/clips/{clip.id}/thumb" if clip.thumb_key else None
    return url, thumb


@router.post("/devices/{device_id}/clips", status_code=201)
def upload_clip(device_id: str, request: Request,
                file: UploadFile = File(...),
                meta: Optional[str] = Form(None),
                thumb: Optional[UploadFile] = File(None),
                p: Principal = Depends(require_device), db: Session = Depends(get_db)):
    dev = self_device(device_id, p, db)
    if dev.owner_id is None:
        raise HTTPException(403, "device_unclaimed")
    try:
        meta_obj = json.loads(meta) if meta else {}
    except json.JSONDecodeError:
        raise HTTPException(422, "meta must be a JSON object")
    if not isinstance(meta_obj, dict):
        raise HTTPException(422, "meta must be a JSON object")

    storage = request.app.state.storage
    s = request.app.state.settings
    clip_id = str(uuid.uuid4())
    key = f"{device_id}/{clip_id}.mp4"
    try:
        size = storage.put(key, file.file, max_bytes=s.max_clip_bytes)
    except ValueError:
        raise HTTPException(413, "clip too large")
    thumb_key = None
    if thumb is not None and thumb.filename:
        thumb_key = f"{device_id}/{clip_id}.jpg"
        try:
            storage.put(thumb_key, thumb.file, max_bytes=2 * 1024 * 1024)
        except ValueError:
            storage.delete(key)
            raise HTTPException(413, "thumbnail too large")

    def _f(v: Any) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    clip = Clip(
        id=clip_id, device_id=device_id, ts=_f(meta_obj.get("ts")) or time.time(),
        duration_s=_f(meta_obj.get("duration_s")), size=size, storage_key=key,
        thumb_key=thumb_key, meta=meta_obj,
    )
    db.add(clip)
    db.commit()
    return {"clip_id": clip_id}


@router.get("/devices/{device_id}/clips")
def list_clips(device_id: str, request: Request, limit: int = Query(100, ge=1, le=500),
               user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    owned_device(device_id, user, db)
    out = []
    q = select(Clip).where(Clip.device_id == device_id).order_by(Clip.ts.desc()).limit(limit)
    for c in db.scalars(q):
        url, thumb = _clip_urls(request, c)
        out.append({"clip_id": c.id, "ts": _iso_ts(c.ts), "duration_s": c.duration_s, "thumb_url": thumb, "url": url})
    return out


def _owned_clip(clip_id: str, user: Principal, db: Session) -> Clip:
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(404, "clip not found")
    owned_device(clip.device_id, user, db)
    return clip


def _serve(request: Request, key: str, media_type: str, filename: str):
    storage = request.app.state.storage
    path = storage.local_path(key)
    if path is not None:
        return FileResponse(path, media_type=media_type, filename=filename)
    url = storage.presigned_url(key)
    if url:
        return RedirectResponse(url, status_code=302)
    raise HTTPException(404, "file missing")


@router.get("/clips/{clip_id}")
def download_clip(clip_id: str, request: Request,
                  user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    clip = _owned_clip(clip_id, user, db)
    return _serve(request, clip.storage_key, "video/mp4", f"{clip.id}.mp4")


@router.get("/clips/{clip_id}/thumb")
def download_thumb(clip_id: str, request: Request,
                   user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    clip = _owned_clip(clip_id, user, db)
    if not clip.thumb_key:
        raise HTTPException(404, "no thumbnail")
    return _serve(request, clip.thumb_key, "image/jpeg", f"{clip.id}.jpg")


@router.delete("/clips/{clip_id}", status_code=204)
def delete_clip(clip_id: str, request: Request,
                user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    clip = _owned_clip(clip_id, user, db)
    storage = request.app.state.storage
    for key in (clip.storage_key, clip.thumb_key):
        if key:
            storage.delete(key)
    db.delete(clip)
    db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ push tokens

@router.post("/push-tokens", status_code=204)
def post_push_token(body: PushTokenIn, user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    upsert_push_token(db, user.user_id, body.token, body.platform)
    return Response(status_code=204)
