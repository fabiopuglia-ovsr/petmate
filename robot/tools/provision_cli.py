#!/usr/bin/env python3
"""Petmate BLE provisioning from a laptop — acts as the BLE central exactly like
the app (docs/INTERFACES.md §7), so the Pi side can be tested without a phone.

    pip install bleak cryptography
    python3 provision_cli.py --cloud http://192.168.1.50:8000/v1 \\
        --email me@example.com --password secret --signup \\
        --ssid "Casa" --wifi-password "..."            # omit --ssid to choose interactively

Steps: login (or sign up) on the cloud → ``POST /claim-tokens`` → scan for
``Petmate-XXXX`` → read device info (X25519 pubkey) and the Wi-Fi list →
encrypt {ssid, password, claim_token, cloud_url, timezone} with
X25519 + HKDF-SHA256 + ChaCha20-Poly1305 → write chunks of ≤ 180 B (flags
byte included) → follow the status notifications until ``done``/``error``.

``cloud_url`` sent to the robot defaults to ``--cloud``; when you log in via
``localhost`` pass ``--robot-cloud-url http://<PC LAN IP>:8000/v1`` (the robot
cannot reach the PC's localhost).

Standalone on purpose (no pmcore import): it is an independent implementation
of the §7 protocol. Tests drive :func:`provision` over pmcore's in-process
loopback transport (``pmcore.ble.loopback``); any object with async
``read(uuid)``, ``write(uuid, data)``, ``subscribe(uuid, cb)`` works as link.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

SERVICE_UUID = "8f1d0000-5e7a-4c3a-9d8e-7e7f1a2b3c4d"
CHAR_DEVICE_INFO = "8f1d0001-5e7a-4c3a-9d8e-7e7f1a2b3c4d"
CHAR_WIFI_SCAN = "8f1d0002-5e7a-4c3a-9d8e-7e7f1a2b3c4d"
CHAR_PAYLOAD = "8f1d0003-5e7a-4c3a-9d8e-7e7f1a2b3c4d"
CHAR_STATUS = "8f1d0004-5e7a-4c3a-9d8e-7e7f1a2b3c4d"
CHUNK_MAX = 180  # §9.4: whole write incl. the flags byte
TERMINAL = ("done", "error")

ERROR_TEXT = {  # what the robot reports in {"state":"error","error":<code>}
    "wifi_auth_failed": "password Wi-Fi errata",
    "wifi_not_found": "rete Wi-Fi non trovata dal robot",
    "wifi_timeout": "timeout connessione Wi-Fi / DHCP",
    "wifi_unavailable": "Wi-Fi del robot spento/bloccato (paese Wi-Fi non impostato?)",
    "wifi_failed": "connessione Wi-Fi non riuscita",
    "cloud_unreachable": "cloud non raggiungibile dal robot (cloud_url corretto? firewall?)",
    "claim_invalid": "claim token non valido, usato o scaduto",
    "device_rejected": "device_id/secret rifiutati dal cloud (ALLOW_TOFU?)",
    "register_failed": "registrazione fallita",
    "decrypt": "decifratura fallita (chiave pubblica cambiata? riprova)",
    "payload_invalid": "payload non valido (cloud_url http non ammesso? password < 8 caratteri?)",
}


# ------------------------------------------------------------------ crypto (§7)
def encrypt_payload(device_info: dict, plaintext: dict) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    device_id = device_info["device_id"]
    device_pub = X25519PublicKey.from_public_bytes(base64.b64decode(device_info["pubkey"]))
    app_priv = X25519PrivateKey.generate()
    shared = app_priv.exchange(device_pub)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=device_id.encode("utf-8"),
               info=b"petmate-prov-v1").derive(shared)
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(key).encrypt(nonce, json.dumps(plaintext).encode("utf-8"), device_id.encode("utf-8"))
    app_pub = app_priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    env = {"epk": base64.b64encode(app_pub).decode(), "nonce": base64.b64encode(nonce).decode(),
           "ct": base64.b64encode(ct).decode()}
    return json.dumps(env, separators=(",", ":")).encode("utf-8")


def chunk(data: bytes, max_write: int = CHUNK_MAX) -> list[bytes]:
    n = max_write - 1
    parts = [data[i:i + n] for i in range(0, len(data), n)] or [b""]
    return [bytes([(1 if i == 0 else 0) | (2 if i == len(parts) - 1 else 0)]) + p for i, p in enumerate(parts)]


# ------------------------------------------------------------------ cloud
class CloudClient:
    def __init__(self, base: str, timeout: float = 15.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Optional[dict] = None, token: Optional[str] = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                txt = r.read().decode()
                return r.status, (json.loads(txt) if txt else None)
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(txt)
            except ValueError:
                return e.code, {"detail": txt}

    def login(self, email: str, password: str, signup: bool = False) -> str:
        st, j = self._req("POST", "/auth/login", {"email": email, "password": password})
        if st == 401 and signup:
            st, j = self._req("POST", "/auth/register", {"email": email, "password": password})
        if st not in (200, 201) or not isinstance(j, dict) or "access_token" not in j:
            raise RuntimeError(f"login failed: HTTP {st} {j}")
        return j["access_token"]

    def claim_token(self, access_token: str) -> str:
        st, j = self._req("POST", "/claim-tokens", {}, token=access_token)
        if st not in (200, 201) or not isinstance(j, dict) or "claim_token" not in j:
            raise RuntimeError(f"claim token failed: HTTP {st} {j}")
        return j["claim_token"]

    def devices(self, access_token: str) -> list:
        st, j = self._req("GET", "/devices", None, token=access_token)
        return j if st == 200 and isinstance(j, list) else []


# ------------------------------------------------------------------ BLE link (bleak)
class BleakLink:
    def __init__(self, client: Any):
        self.client = client

    @classmethod
    async def find_and_connect(cls, name_prefix: str = "Petmate-", address: Optional[str] = None,
                               timeout: float = 15.0, log: Callable[[str], None] = print) -> "BleakLink":
        from bleak import BleakClient, BleakScanner

        if address is None:
            log(f"Scansione BLE ({timeout:.0f} s) per '{name_prefix}*'…")
            found = await BleakScanner.discover(timeout=timeout, return_adv=True)
            cands = []
            for dev, adv in found.values():
                name = adv.local_name or dev.name or ""
                svc = [u.lower() for u in (adv.service_uuids or [])]
                if name.startswith(name_prefix) or SERVICE_UUID in svc:
                    cands.append((adv.rssi, dev, name))
            if not cands:
                raise RuntimeError("nessun robot Petmate trovato (BLE spento? finestra di provisioning chiusa?)")
            cands.sort(key=lambda c: -c[0])
            for rssi, dev, name in cands:
                log(f"  trovato {name or '?'} {dev.address} RSSI {rssi}")
            target: Any = cands[0][1]
        else:
            target = address
        client = BleakClient(target, timeout=20.0)
        await client.connect()
        link = cls(client)
        try:  # BlueZ: learn the negotiated MTU (bleak private API, best effort)
            acq = getattr(client._backend, "_acquire_mtu", None)
            if acq:
                await acq()
        except Exception:  # noqa: BLE001
            pass
        log(f"Connesso a {getattr(target, 'name', None) or target} (MTU {client.mtu_size})")
        return link

    @property
    def max_write(self) -> int:
        # §7: ≤ 180 B per write. A write-with-response larger than MTU-3 is sent as a
        # long write by BlueZ/CoreBluetooth/WinRT, and the robot still sees one write.
        return CHUNK_MAX

    async def read(self, uuid: str) -> bytes:
        return bytes(await self.client.read_gatt_char(uuid))

    async def write(self, uuid: str, data: bytes) -> None:
        await self.client.write_gatt_char(uuid, data, response=True)

    async def subscribe(self, uuid: str, callback: Callable[[bytes], Any]) -> None:
        await self.client.start_notify(uuid, lambda _c, d: callback(bytes(d)))

    async def close(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ protocol
async def read_json(link: Any, uuid: str) -> Any:
    return json.loads((await link.read(uuid)).decode("utf-8"))


async def list_networks(link: Any, wait_s: float = 6.0) -> list[dict]:
    """The device refreshes its scan in the background on read: retry a few times."""
    nets: list[dict] = []
    t0 = time.monotonic()
    while True:
        nets = await read_json(link, CHAR_WIFI_SCAN)
        if nets or time.monotonic() - t0 > wait_s:
            return nets
        await asyncio.sleep(1.5)


async def provision(link: Any, ssid: str, password: str, claim_token: str, cloud_url: Optional[str],
                    timezone: Optional[str] = "Europe/Rome", timeout: float = 120.0,
                    on_status: Callable[[dict], None] = lambda s: None,
                    device_info: Optional[dict] = None) -> dict:
    """Send the encrypted payload and wait for a terminal status. Returns it."""
    info = device_info or await read_json(link, CHAR_DEVICE_INFO)
    plain: dict[str, Any] = {"ssid": ssid, "password": password, "claim_token": claim_token}
    if cloud_url:
        plain["cloud_url"] = cloud_url
    if timezone:
        plain["timezone"] = timezone
    msg = encrypt_payload(info, plain)

    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()
    armed = False
    last: dict = {}

    def handle(st: dict) -> None:
        nonlocal last
        if not armed or st == last:
            return
        last = st
        on_status(st)
        if st.get("state") in TERMINAL and not done.done():
            done.set_result(st)

    def on_notify(data: bytes) -> None:
        try:
            handle(json.loads(data.decode("utf-8")))
        except ValueError:
            pass

    await link.subscribe(CHAR_STATUS, on_notify)
    armed = True
    for c in chunk(msg, getattr(link, "max_write", CHUNK_MAX)):
        await link.write(CHAR_PAYLOAD, c)
    deadline = loop.time() + timeout
    while not done.done():
        try:
            await asyncio.wait_for(asyncio.shield(done), 2.0)
        except asyncio.TimeoutError:
            pass
        if done.done():
            break
        if loop.time() > deadline:
            raise TimeoutError(f"no final status within {timeout:.0f} s (last {last})")
        try:  # notifications may be lost while Wi-Fi associates: poll as fallback
            handle(await read_json(link, CHAR_STATUS))
        except Exception:  # noqa: BLE001
            pass
    return done.result()


def describe(st: dict) -> str:
    s = st.get("state")
    if s == "error":
        code = st.get("error")
        return f"ERRORE: {code} — {ERROR_TEXT.get(code, '')}".rstrip(" —")
    return f"{s}" + (f" (ip {st['ip']})" if st.get("ip") else "")


# ------------------------------------------------------------------ CLI
def _is_local(url: str) -> bool:
    h = urllib.parse.urlparse(url).hostname or ""
    return h in ("localhost", "::1") or h.startswith("127.")


async def amain(a: argparse.Namespace) -> int:
    claim = a.claim_token
    robot_url = a.robot_cloud_url or a.cloud
    if robot_url and _is_local(robot_url):
        print(f"ATTENZIONE: cloud_url per il robot = {robot_url}: il robot non può raggiungere il localhost del PC. "
              "Usa --robot-cloud-url http://<IP-LAN-del-PC>:8000/v1", file=sys.stderr)
    cloud = CloudClient(a.cloud) if a.cloud else None
    access = None
    if not claim and not a.scan_only:
        if cloud is None or not a.email:
            print("serve --claim-token oppure --cloud + --email/--password", file=sys.stderr)
            return 2
        pw = a.password or getpass.getpass("Password account: ")
        access = cloud.login(a.email, pw, signup=a.signup)
        claim = cloud.claim_token(access)
        print(f"Claim token ottenuto (valido 10 min): {claim[:6]}…")

    link = await BleakLink.find_and_connect(a.name_prefix, a.address, a.scan_timeout)
    try:
        info = await read_json(link, CHAR_DEVICE_INFO)
        print(f"Robot: {info.get('device_id')} model={info.get('model')} fw={info.get('fw')}")
        st = await read_json(link, CHAR_STATUS)
        print(f"Stato attuale: {describe(st)}")
        nets = await list_networks(link)
        print("Reti Wi-Fi viste dal robot:")
        for i, n in enumerate(nets):
            print(f"  [{i}] {n['ssid']:<32} {n['rssi']:>4} dBm {'protetta' if n.get('secure') else 'aperta'}")
        if a.scan_only:
            return 0
        ssid = a.ssid
        if not ssid:
            sel = input("Numero rete o SSID (anche nascosta): ").strip()
            ssid = nets[int(sel)]["ssid"] if sel.isdigit() and int(sel) < len(nets) else sel
        wpw = a.wifi_password if a.wifi_password is not None else os.environ.get("PETMATE_WIFI_PASSWORD")
        if wpw is None:
            wpw = getpass.getpass(f"Password Wi-Fi per '{ssid}' (vuota se aperta): ")
        t0 = time.monotonic()
        final = await provision(
            link, ssid, wpw, claim, robot_url, a.timezone, a.timeout,
            on_status=lambda s: print(f"  [{time.monotonic() - t0:5.1f}s] {describe(s)}"),
            device_info=info,
        )
    finally:
        await link.close()
    if final.get("state") != "done":
        print("Provisioning NON riuscito. La finestra BLE resta aperta: correggi e riprova.")
        return 1
    print("Provisioning completato: il robot è registrato e il BLE si spegne.")
    if cloud and access:
        for d in cloud.devices(access):
            if d.get("device_id") == info.get("device_id"):
                print(f"Cloud: {d.get('device_id')} online={d.get('online')}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Petmate BLE provisioning (laptop central, §7)")
    p.add_argument("--cloud", help="cloud base URL incl. /v1 used for login, e.g. http://192.168.1.50:8000/v1")
    p.add_argument("--robot-cloud-url", help="cloud_url sent to the robot (default: --cloud)")
    p.add_argument("--email")
    p.add_argument("--password", help="account password (prompted if omitted)")
    p.add_argument("--signup", action="store_true", help="create the account if login fails")
    p.add_argument("--claim-token", help="use this claim token instead of logging in")
    p.add_argument("--ssid")
    p.add_argument("--wifi-password", help="Wi-Fi password (or env PETMATE_WIFI_PASSWORD; prompted if omitted)")
    p.add_argument("--timezone", default="Europe/Rome")
    p.add_argument("--name-prefix", default="Petmate-")
    p.add_argument("--address", help="BLE address/UUID of the robot (skip scan)")
    p.add_argument("--scan-timeout", type=float, default=10.0)
    p.add_argument("--timeout", type=float, default=120.0, help="max wait for done/error")
    p.add_argument("--scan-only", action="store_true", help="only show device info and Wi-Fi list")
    a = p.parse_args(argv)
    try:
        return asyncio.run(amain(a))
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"Errore: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
