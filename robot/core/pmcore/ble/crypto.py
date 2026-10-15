"""Provisioning crypto and chunking — device side (INTERFACES.md §7).

1. device publishes an X25519 public key (fresh per provisioning window);
2. app: ``shared = X25519(app_priv, device_pub)``;
3. ``key = HKDF-SHA256(shared, salt=utf8(device_id), info=b"petmate-prov-v1", L=32)``;
4. ``ct = ChaCha20-Poly1305(key).encrypt(nonce12, plaintext, aad=utf8(device_id))``
   (``ct`` includes the 16-byte Poly1305 tag appended, as produced by
   ``cryptography``/libsodium IETF construction);
5. message ``{"epk":b64(app_pub),"nonce":b64(nonce),"ct":b64(ct)}`` UTF-8;
6. chunks of ≤ 180 bytes: each write = ``[flags:u8][data]``, bit0 first,
   bit1 last. Interpretation used here: 180 bytes is the size of the whole
   write (1 flag byte + ≤ 179 data bytes) — the safest reading; a receiver
   also accepts writes of 1+180 bytes.

base64 = standard alphabet with padding (RFC 4648 §4); decoder also accepts
missing padding and URL-safe alphabet for robustness.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.exceptions import InvalidTag

HKDF_INFO = b"petmate-prov-v1"
NONCE_LEN = 12
KEY_LEN = 32
CHUNK_MAX = 180          # bytes per GATT write, including the flags byte
CHUNK_DATA_MAX = CHUNK_MAX - 1
FLAG_FIRST = 0x01
FLAG_LAST = 0x02
MAX_MESSAGE = 4096       # sanity bound on the reassembled JSON


class ProvisioningCryptoError(ValueError):
    pass


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: Any) -> bytes:
    if not isinstance(s, str):
        raise ProvisioningCryptoError("base64 field must be a string")
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ProvisioningCryptoError(f"invalid base64: {e}")


def derive_key(shared: bytes, device_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=device_id.encode("utf-8"),
        info=HKDF_INFO,
    ).derive(shared)


def raw_public(pub: X25519PublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class DeviceKeypair:
    """Ephemeral device X25519 keypair for one provisioning window."""

    def __init__(self, priv: Optional[X25519PrivateKey] = None):
        self._priv = priv or X25519PrivateKey.generate()
        self.public_raw = raw_public(self._priv.public_key())

    @property
    def public_b64(self) -> str:
        return b64e(self.public_raw)

    def decrypt_message(self, message: bytes | str, device_id: str) -> dict:
        """Decrypt the §7 envelope and return the plaintext JSON object."""
        try:
            env = json.loads(message if isinstance(message, str) else message.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProvisioningCryptoError("envelope is not valid JSON")
        if not isinstance(env, dict):
            raise ProvisioningCryptoError("envelope must be an object")
        epk = b64d(env.get("epk"))
        nonce = b64d(env.get("nonce"))
        ct = b64d(env.get("ct"))
        if len(epk) != 32:
            raise ProvisioningCryptoError("epk must be 32 bytes")
        if len(nonce) != NONCE_LEN:
            raise ProvisioningCryptoError("nonce must be 12 bytes")
        if len(ct) < 16:
            raise ProvisioningCryptoError("ciphertext too short")
        try:
            shared = self._priv.exchange(X25519PublicKey.from_public_bytes(epk))
        except ValueError as e:  # all-zero shared secret (low-order point)
            raise ProvisioningCryptoError(f"key exchange failed: {e}")
        key = derive_key(shared, device_id)
        try:
            pt = ChaCha20Poly1305(key).decrypt(nonce, ct, device_id.encode("utf-8"))
        except InvalidTag:
            raise ProvisioningCryptoError("authentication failed")
        try:
            obj = json.loads(pt.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProvisioningCryptoError("plaintext is not valid JSON")
        if not isinstance(obj, dict):
            raise ProvisioningCryptoError("plaintext must be an object")
        return obj


@dataclass
class ProvisioningPayload:
    ssid: str
    password: str
    claim_token: str
    cloud_url: Optional[str]
    timezone: Optional[str]

    @classmethod
    def parse(cls, obj: dict, allow_http: bool = False) -> "ProvisioningPayload":
        """Validate the decrypted §7 plaintext. ``allow_http``: accept an
        ``http://`` cloud_url (MVP developer cloud on the LAN, settings
        ``cloud.allow_http``); otherwise only ``https://``."""
        ssid = obj.get("ssid")
        if not isinstance(ssid, str) or not (1 <= len(ssid.encode("utf-8")) <= 32):
            raise ProvisioningCryptoError("ssid must be 1..32 bytes")
        pw = obj.get("password", "")
        if not isinstance(pw, str) or len(pw) > 63 or (pw and len(pw) < 8):
            raise ProvisioningCryptoError("password must be empty (open) or 8..63 chars")
        ct = obj.get("claim_token")
        if not isinstance(ct, str) or not ct:
            raise ProvisioningCryptoError("claim_token is required")
        url = obj.get("cloud_url")
        schemes = ("https://", "http://") if allow_http else ("https://",)
        if url == "":
            url = None
        if url is not None and (not isinstance(url, str) or not url.startswith(schemes) or len(url) > 512):
            raise ProvisioningCryptoError("cloud_url must be " + " or ".join(schemes))
        tz = obj.get("timezone")
        if tz is not None and not isinstance(tz, str):
            raise ProvisioningCryptoError("timezone must be a string")
        return cls(ssid, pw, ct, url, tz)


# ------------------------------------------------------------------ chunking

def chunk_message(data: bytes, chunk_max: int = CHUNK_MAX) -> list[bytes]:
    """Split ``data`` into ``[flags][≤chunk_max-1 bytes]`` writes (§7.6)."""
    n = chunk_max - 1
    if n < 1:
        raise ValueError("chunk_max too small")
    parts = [data[i:i + n] for i in range(0, len(data), n)] or [b""]
    out = []
    for i, p in enumerate(parts):
        flags = (FLAG_FIRST if i == 0 else 0) | (FLAG_LAST if i == len(parts) - 1 else 0)
        out.append(bytes([flags]) + p)
    return out


class ChunkReassembler:
    """Reassembles §7 chunked writes. ``feed`` returns the full message when done."""

    def __init__(self, max_size: int = MAX_MESSAGE):
        self.max_size = max_size
        self._buf: Optional[bytearray] = None

    @property
    def receiving(self) -> bool:
        return self._buf is not None

    def reset(self) -> None:
        self._buf = None

    def feed(self, write: bytes) -> Optional[bytes]:
        if len(write) < 1:
            raise ProvisioningCryptoError("empty write")
        if len(write) > CHUNK_MAX + 1:
            raise ProvisioningCryptoError("chunk too large")
        flags, data = write[0], bytes(write[1:])
        if flags & ~(FLAG_FIRST | FLAG_LAST):
            raise ProvisioningCryptoError("invalid flags")
        if flags & FLAG_FIRST:
            self._buf = bytearray()  # a new "first" restarts the transfer
        elif self._buf is None:
            raise ProvisioningCryptoError("continuation chunk without first")
        assert self._buf is not None
        self._buf.extend(data)
        if len(self._buf) > self.max_size:
            self._buf = None
            raise ProvisioningCryptoError("message too large")
        if flags & FLAG_LAST:
            out, self._buf = bytes(self._buf), None
            return out
        return None
