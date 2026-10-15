"""Factory identity (/etc/petmate/identity.json) and cloud state
(/var/lib/petmate/cloud.json), INTERFACES.md §8."""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .log import get_logger, kv
from .util import atomic_write_json, read_json

log = get_logger(__name__)

# Factory ids are decimal (PM-000123); MVP ids are the last 6 hex digits of the
# Pi serial (PM-A1B2C3, docs/MVP.md). Cloud accepts ^[A-Za-z0-9_-]+$.
DEVICE_ID_RE = re.compile(r"^PM-[0-9A-Za-z]{4,32}$")
DEFAULT_CLOUD_URL = "https://api.petmate.example/v1"


class IdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    device_id: str
    device_secret: str

    @property
    def short_id(self) -> str:
        """Last 4 characters of device_id (BLE name Petmate-XXXX, §7)."""
        tail = re.sub(r"[^0-9A-Za-z]", "", self.device_id[3:] if self.device_id.startswith("PM-") else self.device_id)
        return tail[-4:].upper().rjust(4, "0")

    @classmethod
    def load(cls, path: Path) -> "Identity":
        try:
            raw = read_json(path)
        except FileNotFoundError:
            raise IdentityError(f"identity file not found: {path}")
        except (OSError, json.JSONDecodeError) as e:
            raise IdentityError(f"identity file unreadable: {e}")
        did = raw.get("device_id")
        sec = raw.get("device_secret")
        if not isinstance(did, str) or not DEVICE_ID_RE.match(did):
            raise IdentityError(f"invalid device_id in {path}: {did!r}")
        if not isinstance(sec, str) or len(sec) < 16:
            raise IdentityError(f"invalid device_secret in {path}")
        return cls(did, sec)


def jwt_exp(token: str) -> Optional[float]:
    """Read the ``exp`` claim of a JWT without verifying it (device side)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001
        return None


@dataclass
class CloudState:
    """Persisted cloud association. ``provisioned`` == registered with an owner."""

    cloud_url: str = DEFAULT_CLOUD_URL
    device_token: Optional[str] = None
    token_exp: Optional[float] = None
    owner_user_id: Optional[str] = None
    registered_at: Optional[float] = None
    timezone: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def provisioned(self) -> bool:
        return self.owner_user_id is not None

    def token_valid(self, margin_s: float = 300.0, now: Optional[float] = None) -> bool:
        if not self.device_token:
            return False
        exp = self.token_exp or jwt_exp(self.device_token)
        if exp is None:
            return True
        return (now or time.time()) < exp - margin_s

    def set_token(self, token: str) -> None:
        self.device_token = token
        self.token_exp = jwt_exp(token)

    @classmethod
    def load(cls, path: Path) -> "CloudState":
        try:
            raw = read_json(path)
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError) as e:
            log.error("cloud.json unreadable, treating as unprovisioned", extra=kv(error=str(e)))
            return cls()
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        st = cls(**{k: v for k, v in raw.items() if k in known})
        # http:// is only ever stored when the BLE payload was accepted with
        # cloud.allow_http (MVP: developer cloud on the LAN), so keep it.
        if not isinstance(st.cloud_url, str) or not st.cloud_url.startswith(("https://", "http://")):
            st.cloud_url = DEFAULT_CLOUD_URL
        return st

    def save(self, path: Path) -> None:
        atomic_write_json(path, asdict(self), mode=0o600)
