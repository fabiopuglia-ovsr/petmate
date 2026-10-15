"""Password/secret hashing and JWT (HS256) handling per INTERFACES.md §6."""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Literal

import jwt
from passlib.context import CryptContext

_pwd = CryptContext(schemes=["argon2"], deprecated="auto")

TokenType = Literal["user", "device"]


def hash_secret(secret: str) -> str:
    return _pwd.hash(secret)


def verify_secret(secret: str, hashed: str) -> bool:
    try:
        return _pwd.verify(secret, hashed)
    except Exception:  # malformed hash
        return False


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_claim_token() -> str:
    # 128 bit, URL-safe; short enough for the BLE provisioning payload.
    return secrets.token_urlsafe(16)


@dataclass(frozen=True)
class Principal:
    typ: TokenType
    sub: str  # user_id (as string) or device_id

    @property
    def user_id(self) -> int:
        assert self.typ == "user"
        return int(self.sub)

    @property
    def device_id(self) -> str:
        assert self.typ == "device"
        return self.sub


def issue_token(secret: str, typ: TokenType, sub: str | int, ttl_s: int) -> str:
    now = int(time.time())
    payload = {"sub": str(sub), "typ": typ, "iat": now, "exp": now + ttl_s}
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(secret: str, token: str) -> Principal | None:
    try:
        p = jwt.decode(
            token, secret, algorithms=["HS256"], options={"require": ["sub", "typ", "exp"]}
        )
    except jwt.PyJWTError:
        return None
    if p.get("typ") not in ("user", "device"):
        return None
    return Principal(typ=p["typ"], sub=str(p["sub"]))
