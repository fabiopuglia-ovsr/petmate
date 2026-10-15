"""Domain operations shared by REST and signaling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .config import Settings
from .models import ClaimToken, Clip, Device, DeviceConfig, Event, FactoryDevice, PushToken
from .security import hash_secret, new_claim_token, sha256_hex, verify_secret

log = logging.getLogger("petmate.services")

# Pre-computed hash used to keep timing roughly constant for unknown device ids.
_DUMMY_HASH = hash_secret("petmate-dummy-secret")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite drops tzinfo
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- claim tokens

def create_claim_token(db: Session, user_id: int, ttl_s: int) -> tuple[str, datetime]:
    tok = new_claim_token()
    exp = utcnow() + timedelta(seconds=ttl_s)
    db.add(ClaimToken(token_hash=sha256_hex(tok), user_id=user_id, expires_at=exp))
    # housekeeping: drop tokens expired for > 1 day
    db.execute(delete(ClaimToken).where(ClaimToken.expires_at < utcnow() - timedelta(days=1)))
    db.commit()
    return tok, exp


class ClaimError(Exception):
    pass


def consume_claim_token(db: Session, token: str, device_id: str) -> int:
    """Atomically mark a claim token used. Returns the owner user_id.

    Idempotent for the same device: if the device retries with a token it already
    consumed (lost HTTP response), the same owner is returned."""
    h = sha256_hex(token)
    ct = db.scalar(select(ClaimToken).where(ClaimToken.token_hash == h))
    if ct is None:
        raise ClaimError("invalid claim token")
    if ct.used_at is not None:
        if ct.used_by_device == device_id and aware(ct.expires_at) > utcnow():
            return ct.user_id
        raise ClaimError("claim token already used")
    if aware(ct.expires_at) <= utcnow():
        raise ClaimError("claim token expired")
    res = db.execute(
        update(ClaimToken)
        .where(ClaimToken.id == ct.id, ClaimToken.used_at.is_(None))
        .values(used_at=utcnow(), used_by_device=device_id)
    )
    if res.rowcount != 1:
        raise ClaimError("claim token already used")
    return ct.user_id


# ---------------------------------------------------------------- factory / devices

def verify_factory_secret(db: Session, settings: Settings, device_id: str, secret: str) -> bool:
    fd = db.get(FactoryDevice, device_id)
    if fd is None:
        if settings.allow_tofu:
            log.warning("ALLOW_TOFU: accepting unknown device %s", device_id)
            db.add(FactoryDevice(device_id=device_id, secret_hash=hash_secret(secret), model="TOFU"))
            db.flush()
            return True
        verify_secret(secret, _DUMMY_HASH)
        return False
    return verify_secret(secret, fd.secret_hash)


def purge_device_data(db: Session, device_id: str, storage) -> None:
    """Remove owner-private data (on unclaim or ownership transfer)."""
    for clip in db.scalars(select(Clip).where(Clip.device_id == device_id)):
        for key in (clip.storage_key, clip.thumb_key):
            if key:
                try:
                    storage.delete(key)
                except Exception as exc:  # pragma: no cover
                    log.warning("could not delete %s: %s", key, exc)
    db.execute(delete(Clip).where(Clip.device_id == device_id))
    db.execute(delete(Event).where(Event.device_id == device_id))
    db.execute(delete(DeviceConfig).where(DeviceConfig.device_id == device_id))


def touch_device(db: Session, device_id: str, fw: Optional[str] = None, min_interval_s: float = 0) -> None:
    dev = db.get(Device, device_id)
    if dev is None:
        return
    now = utcnow()
    if min_interval_s and dev.last_seen and (now - aware(dev.last_seen)).total_seconds() < min_interval_s and not fw:
        return
    dev.last_seen = now
    if fw:
        dev.fw = fw
    db.commit()


def device_owner(db: Session, device_id: str) -> Optional[int]:
    return db.scalar(select(Device.owner_id).where(Device.device_id == device_id))


# ---------------------------------------------------------------- push tokens

def upsert_push_token(db: Session, user_id: int, token: str, platform: str) -> None:
    pt = db.scalar(select(PushToken).where(PushToken.token == token))
    if pt is None:
        db.add(PushToken(user_id=user_id, token=token, platform=platform))
    else:  # token moved to another account (logout/login on same phone)
        pt.user_id = user_id
        pt.platform = platform
        pt.updated_at = utcnow()
    db.commit()


# ---------------------------------------------------------------- TURN credentials

def turn_credentials(secret: str, sub: str, ttl_s: int, now: Optional[float] = None) -> tuple[str, str]:
    """coturn REST API (use-auth-secret): username = "<expiry_unix>:<sub>",
    credential = base64(HMAC-SHA1(secret, username))."""
    expiry = int((now if now is not None else time.time()) + ttl_s)
    username = f"{expiry}:{sub}"
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode()


def ice_servers(settings: Settings, sub: str) -> dict[str, Any]:
    servers: list[dict[str, Any]] = []
    if settings.stun_urls:
        servers.append({"urls": list(settings.stun_urls)})
    if settings.turn_urls:
        username, credential = turn_credentials(settings.turn_secret, sub, settings.turn_ttl_s)
        servers.append({"urls": list(settings.turn_urls), "username": username, "credential": credential})
    return {"ice_servers": servers}
