"""Runtime configuration, read from environment variables (see deploy/.env.example)."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field

log = logging.getLogger("petmate.config")

# Used when neither STUN_URLS nor TURN_HOST is set (MVP dev: cloud on a laptop, no coturn).
# STUN_URLS=none disables STUN entirely (offline LAN: host candidates only).
DEFAULT_STUN = "stun:stun.l.google.com:19302"


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _rate(name: str, default: str) -> tuple[int, float]:
    """Parse "<calls>/<seconds>" (e.g. "10/60")."""
    raw = os.environ.get(name, default)
    calls, _, period = raw.partition("/")
    return int(calls), float(period or 60)


def _stun_urls(default_from_turn_host: str) -> list[str]:
    urls = _list("STUN_URLS", default_from_turn_host or DEFAULT_STUN)
    if [u.lower() for u in urls] == ["none"]:
        return []
    return urls or [DEFAULT_STUN]


@dataclass
class Settings:
    database_url: str = "sqlite:///./petmate.db"
    jwt_secret: str = ""
    jwt_ttl_s: int = 24 * 3600
    claim_ttl_s: int = 600
    allow_tofu: bool = False

    # TURN / STUN (coturn REST API, use-auth-secret)
    turn_secret: str = ""
    turn_ttl_s: int = 3600
    stun_urls: list[str] = field(default_factory=list)
    turn_urls: list[str] = field(default_factory=list)

    # Clip storage
    storage_dir: str = "./data/clips"
    max_clip_bytes: int = 100 * 1024 * 1024
    public_base_url: str = ""  # e.g. https://api.petmate.example ; empty → relative URLs

    # Push (FCM HTTP v1)
    fcm_project_id: str = ""
    fcm_credentials_file: str = ""

    # HTTP
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    rate_auth: tuple[int, float] = (10, 60.0)  # /auth/register, /auth/login
    rate_device_auth: tuple[int, float] = (20, 60.0)  # /devices/register, /devices/token
    rate_limit_enabled: bool = True
    port: int = 8000  # only used to build the LAN URLs of /v1/server-info and the startup log

    @classmethod
    def from_env(cls) -> "Settings":
        turn_host = os.environ.get("TURN_HOST", "")
        turn_port = os.environ.get("TURN_PORT", "3478")
        turns_port = os.environ.get("TURNS_PORT", "5349")
        default_stun = f"stun:{turn_host}:{turn_port}" if turn_host else ""
        default_turn = (
            f"turn:{turn_host}:{turn_port}?transport=udp,"
            f"turn:{turn_host}:{turn_port}?transport=tcp,"
            f"turns:{turn_host}:{turns_port}?transport=tcp"
            if turn_host
            else ""
        )
        s = cls(
            database_url=os.environ.get("DATABASE_URL", cls.database_url),
            jwt_secret=os.environ.get("JWT_SECRET", ""),
            jwt_ttl_s=int(os.environ.get("JWT_TTL_S", 24 * 3600)),
            claim_ttl_s=int(os.environ.get("CLAIM_TTL_S", 600)),
            allow_tofu=_bool("ALLOW_TOFU", False),
            turn_secret=os.environ.get("TURN_SECRET", ""),
            turn_ttl_s=int(os.environ.get("TURN_TTL_S", 3600)),
            stun_urls=_stun_urls(default_stun),
            turn_urls=_list("TURN_URLS", default_turn),
            storage_dir=os.environ.get("STORAGE_DIR", cls.storage_dir),
            max_clip_bytes=int(os.environ.get("MAX_CLIP_MB", 100)) * 1024 * 1024,
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
            fcm_project_id=os.environ.get("FCM_PROJECT_ID", ""),
            fcm_credentials_file=os.environ.get(
                "FCM_CREDENTIALS_FILE", os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            ),
            cors_origins=_list("CORS_ORIGINS", "*"),
            rate_auth=_rate("RATE_LIMIT_AUTH", "10/60"),
            rate_device_auth=_rate("RATE_LIMIT_DEVICE_AUTH", "20/60"),
            rate_limit_enabled=_bool("RATE_LIMIT_ENABLED", True),
            port=int(os.environ.get("PORT", 8000)),
        )
        return s

    def finalize(self) -> "Settings":
        if not self.jwt_secret:
            log.warning("JWT_SECRET not set: using a random secret (tokens die on restart)")
            self.jwt_secret = secrets.token_urlsafe(48)
        if not self.turn_secret:
            log.warning("TURN_SECRET not set: TURN credentials will not work with coturn")
        return self
