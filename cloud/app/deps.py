from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import Device
from .security import Principal, decode_token


def _bearer(request: Request) -> Optional[str]:
    h = request.headers.get("authorization", "")
    scheme, _, tok = h.partition(" ")
    if scheme.lower() != "bearer" or not tok:
        return None
    return tok.strip()


def get_principal(request: Request) -> Principal:
    tok = _bearer(request)
    if not tok:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    p = decode_token(request.app.state.settings.jwt_secret, tok)
    if p is None:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return p


def require_user(p: Principal = Depends(get_principal)) -> Principal:
    if p.typ != "user":
        raise HTTPException(403, "user token required")
    return p


def require_device(p: Principal = Depends(get_principal)) -> Principal:
    if p.typ != "device":
        raise HTTPException(403, "device token required")
    return p


def owned_device(device_id: str, user: Principal, db: Session) -> Device:
    """Return the device if owned by user. 404 if it doesn't exist, 403 if owned by someone else."""
    dev = db.get(Device, device_id)
    if dev is None:
        raise HTTPException(404, "device not found")
    if dev.owner_id != user.user_id:
        raise HTTPException(403, "not your device")
    return dev


def self_device(device_id: str, p: Principal, db: Session) -> Device:
    if p.device_id != device_id:
        raise HTTPException(403, "device token does not match path")
    dev = db.get(Device, device_id)
    if dev is None:
        raise HTTPException(404, "device not found")
    return dev


def get_db_dep():
    return Depends(get_db)
