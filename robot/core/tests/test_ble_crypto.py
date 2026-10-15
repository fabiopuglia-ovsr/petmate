"""Interoperability test for §7: the APP side is implemented here from the spec
text only (independently of pmcore.ble.crypto) using `cryptography`."""

import asyncio
import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from pmcore.ble.crypto import (
    ChunkReassembler,
    DeviceKeypair,
    ProvisioningCryptoError,
    ProvisioningPayload,
    chunk_message,
)
from pmcore.ble.provisioning import Provisioner

DEVICE_ID = "PM-000123"


# ---------------------------------------------------------------- APP side (spec)
def app_encrypt(device_info_json: bytes, plaintext: dict) -> bytes:
    info = json.loads(device_info_json)
    device_id = info["device_id"]
    device_pub = X25519PublicKey.from_public_bytes(base64.b64decode(info["pubkey"]))
    app_priv = X25519PrivateKey.generate()                                   # 1.
    shared = app_priv.exchange(device_pub)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=device_id.encode("utf-8"),
               info=b"petmate-prov-v1").derive(shared)                         # 2.
    pt = json.dumps(plaintext).encode("utf-8")                                 # 3.
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(key).encrypt(nonce, pt, device_id.encode("utf-8"))  # 4.
    app_pub = app_priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    msg = {"epk": base64.b64encode(app_pub).decode(), "nonce": base64.b64encode(nonce).decode(),
           "ct": base64.b64encode(ct).decode()}                                # 5.
    return json.dumps(msg).encode("utf-8")


def app_chunks(message: bytes, max_write: int = 180) -> list[bytes]:
    """6. writes of ≤180 bytes: [flags][data], bit0 first, bit1 last."""
    n = max_write - 1
    parts = [message[i:i + n] for i in range(0, len(message), n)]
    out = []
    for i, p in enumerate(parts):
        f = (1 if i == 0 else 0) | (2 if i == len(parts) - 1 else 0)
        out.append(bytes([f]) + p)
    return out


PLAINTEXT = {"ssid": "Casa Rossi", "password": "s3cr3t-pässwörd", "claim_token": "ct_abc123",
             "cloud_url": "https://api.petmate.example/v1", "timezone": "Europe/Rome"}


def test_round_trip_app_to_device():
    dev = DeviceKeypair()
    info = json.dumps({"device_id": DEVICE_ID, "model": "PM-EVT1", "fw": "0.1.0", "pubkey": dev.public_b64}).encode()
    msg = app_encrypt(info, PLAINTEXT)
    chunks = app_chunks(msg)
    assert len(chunks) > 1 and all(len(c) <= 180 for c in chunks)
    r = ChunkReassembler()
    out = None
    for c in chunks:
        out = r.feed(c)
    assert out == msg
    assert dev.decrypt_message(out, DEVICE_ID) == PLAINTEXT
    p = ProvisioningPayload.parse(PLAINTEXT)
    assert p.ssid == "Casa Rossi" and p.claim_token == "ct_abc123"


def test_device_chunker_matches_app_chunker():
    data = os.urandom(1000)
    assert chunk_message(data) == app_chunks(data)
    assert chunk_message(b"x" * 179) == [bytes([3]) + b"x" * 179]  # single write: first|last


def test_wrong_device_id_or_tamper_fails():
    dev = DeviceKeypair()
    info = json.dumps({"device_id": DEVICE_ID, "pubkey": dev.public_b64}).encode()
    msg = app_encrypt(info, PLAINTEXT)
    with pytest.raises(ProvisioningCryptoError, match="authentication"):
        dev.decrypt_message(msg, "PM-000124")  # salt + AAD mismatch
    env = json.loads(msg)
    ct = bytearray(base64.b64decode(env["ct"]))
    ct[0] ^= 1
    env["ct"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(ProvisioningCryptoError):
        dev.decrypt_message(json.dumps(env).encode(), DEVICE_ID)
    # another window's key (fresh keypair) can't decrypt
    with pytest.raises(ProvisioningCryptoError):
        DeviceKeypair().decrypt_message(msg, DEVICE_ID)


def test_reassembler_errors_and_restart():
    r = ChunkReassembler()
    with pytest.raises(ProvisioningCryptoError):
        r.feed(bytes([0]) + b"orphan")
    assert r.feed(bytes([1]) + b"abc") is None
    assert r.feed(bytes([1]) + b"new") is None  # new "first" restarts
    assert r.feed(bytes([2]) + b"!") == b"new!"
    with pytest.raises(ProvisioningCryptoError):
        r.feed(bytes([0x80]) + b"bad flags")
    r2 = ChunkReassembler(max_size=10)
    r2.feed(bytes([1]) + b"x" * 8)
    with pytest.raises(ProvisioningCryptoError, match="too large"):
        r2.feed(bytes([0]) + b"x" * 8)


def test_provisioner_full_flow_with_fakes():
    async def run():
        calls = {}

        async def wifi_connect(ssid, pw):
            calls["wifi"] = (ssid, pw)
            return "192.168.1.50"

        async def wifi_scan():
            return [{"ssid": "Casa Rossi", "rssi": -50, "secure": True}]

        async def register(claim, url):
            calls["register"] = (claim, url)
            return {"device_token": "x", "owner_user_id": "42"}

        done = []
        prov = Provisioner(DEVICE_ID, wifi_connect, wifi_scan, register, on_done=lambda p: done.append(p))
        states = []
        prov.on_status = lambda s: states.append(s["state"])
        assert json.loads(await asyncio.to_thread(lambda: b"[]")) == []
        await prov.refresh_scan()
        assert json.loads(prov.wifi_scan_bytes())[0]["ssid"] == "Casa Rossi"
        msg = app_encrypt(prov.device_info_bytes(), PLAINTEXT)
        for c in app_chunks(msg):
            prov.on_write(c)
        await prov.wait()
        assert states == ["receiving", "connecting", "wifi_ok", "registering", "done"]
        assert prov.status == {"state": "done", "error": None, "ip": "192.168.1.50"}
        assert calls["wifi"] == ("Casa Rossi", "s3cr3t-pässwörd")
        assert calls["register"] == ("ct_abc123", "https://api.petmate.example/v1")
        assert len(done) == 1

        # bad ciphertext → error state, no wifi attempt
        prov2 = Provisioner(DEVICE_ID, wifi_connect, wifi_scan, register)
        other = DeviceKeypair()
        bad = app_encrypt(json.dumps({"device_id": DEVICE_ID, "pubkey": other.public_b64}).encode(), PLAINTEXT)
        calls.clear()
        for c in app_chunks(bad):
            prov2.on_write(c)
        await prov2.wait()
        assert prov2.status["state"] == "error" and "decrypt" in prov2.status["error"]
        assert "wifi" not in calls

    asyncio.run(run())


def test_device_info_pubkey_is_32_byte_x25519_b64():
    prov = Provisioner(DEVICE_ID, None, None, None)
    info = json.loads(prov.device_info_bytes())
    assert set(info) == {"device_id", "model", "fw", "pubkey"}
    assert len(base64.b64decode(info["pubkey"])) == 32
    # fresh key per Provisioner (= per window)
    assert Provisioner(DEVICE_ID, None, None, None).device_info()["pubkey"] != info["pubkey"]
