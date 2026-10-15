#!/usr/bin/env python3
"""Reference test vector for Petmate BLE provisioning (INTERFACES.md §7).

Uses the Python `cryptography` package (the same primitives the robot side,
pm-core, is expected to use) to produce a deterministic vector with fixed
keys and nonce. The Dart unit test `test/prov_crypto_test.dart` asserts the
Flutter implementation produces exactly these bytes, and that the decrypt
path here accepts the Dart output.

Usage:  python3 tools/prov_vector.py [--write tools/prov_vector.json]
"""
import base64
import json
import sys

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

DEVICE_ID = "PM-000123"
APP_PRIV = bytes(range(0x01, 0x21))  # 01 02 .. 20
DEVICE_PRIV = bytes(range(0x41, 0x61))  # 41 42 .. 60
NONCE = bytes(range(0xA0, 0xAC))  # 12 bytes
PLAINTEXT_OBJ = {
    "ssid": "Casa Rossi",
    "password": "p@ssw0rd-è-lunga!",
    "claim_token": "ct_9f8e7d6c5b4a",
    "cloud_url": "https://api.petmate.example/v1",
    "timezone": "Europe/Rome",
}
MAX_WRITE = 180  # total bytes per BLE write, flags byte included


def raw_pub(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def compact_json(obj) -> bytes:
    # Same byte layout as Dart's jsonEncode: no spaces, non-ASCII kept as UTF-8.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def derive_key(shared: bytes, device_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=device_id.encode("utf-8"),
        info=b"petmate-prov-v1",
    ).derive(shared)


def chunk(message: bytes, max_write: int = MAX_WRITE):
    size = max_write - 1
    parts = [message[i : i + size] for i in range(0, len(message), size)] or [b""]
    out = []
    for i, p in enumerate(parts):
        flags = (1 if i == 0 else 0) | (2 if i == len(parts) - 1 else 0)
        out.append(bytes([flags]) + p)
    return out


def device_side_decrypt(device_priv: X25519PrivateKey, device_id: str, writes):
    """What pm-core does: reassemble chunks, decrypt, return plaintext dict."""
    assert writes[0][0] & 1 and writes[-1][0] & 2
    msg = json.loads(b"".join(w[1:] for w in writes).decode("utf-8"))
    epk = X25519PublicKey.from_public_bytes(base64.b64decode(msg["epk"]))
    key = derive_key(device_priv.exchange(epk), device_id)
    pt = ChaCha20Poly1305(key).decrypt(
        base64.b64decode(msg["nonce"]), base64.b64decode(msg["ct"]), device_id.encode()
    )
    return json.loads(pt)


def build():
    app = X25519PrivateKey.from_private_bytes(APP_PRIV)
    dev = X25519PrivateKey.from_private_bytes(DEVICE_PRIV)
    shared = app.exchange(dev.public_key())
    key = derive_key(shared, DEVICE_ID)
    plaintext = compact_json(PLAINTEXT_OBJ)
    ct = ChaCha20Poly1305(key).encrypt(NONCE, plaintext, DEVICE_ID.encode("utf-8"))
    message = compact_json(
        {
            "epk": base64.b64encode(raw_pub(app)).decode(),
            "nonce": base64.b64encode(NONCE).decode(),
            "ct": base64.b64encode(ct).decode(),
        }
    )
    writes = chunk(message)
    assert device_side_decrypt(dev, DEVICE_ID, writes) == PLAINTEXT_OBJ
    return {
        "device_id": DEVICE_ID,
        "app_priv_hex": APP_PRIV.hex(),
        "app_pub_hex": raw_pub(app).hex(),
        "device_priv_hex": DEVICE_PRIV.hex(),
        "device_pub_hex": raw_pub(dev).hex(),
        "device_pub_b64": base64.b64encode(raw_pub(dev)).decode(),
        "shared_hex": shared.hex(),
        "key_hex": key.hex(),
        "nonce_hex": NONCE.hex(),
        "plaintext_utf8": plaintext.decode("utf-8"),
        "ct_hex": ct.hex(),
        "message_utf8": message.decode("utf-8"),
        "max_write": MAX_WRITE,
        "writes_hex": [w.hex() for w in writes],
    }


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-writes":
        # Decrypt writes produced elsewhere (e.g. by the Dart implementation).
        writes = [bytes.fromhex(h) for h in json.load(open(sys.argv[2]))]
        dev = X25519PrivateKey.from_private_bytes(DEVICE_PRIV)
        print(json.dumps(device_side_decrypt(dev, DEVICE_ID, writes), ensure_ascii=False))
        sys.exit(0)
    vec = build()
    text = json.dumps(vec, indent=2, ensure_ascii=False)
    if len(sys.argv) == 3 and sys.argv[1] == "--write":
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
