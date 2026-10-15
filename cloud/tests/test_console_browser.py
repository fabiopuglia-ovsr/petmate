"""Web test console in headless Chromium (Playwright) against a real uvicorn dev server.

Skipped when Playwright or its Chromium is missing, or with PETMATE_SKIP_BROWSER=1.
Screenshots: tests/artifacts/console-login.png, console-robots.png, console-wizard.png.
Live video is not exercised here (needs pm-core); the live page is checked up to the
offer (SDP content, non-trickle), peer_offline, bye and the 15 s answer timeout.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

if os.environ.get("PETMATE_SKIP_BROWSER") == "1":
    pytest.skip("PETMATE_SKIP_BROWSER=1", allow_module_level=True)
pw = pytest.importorskip("playwright.sync_api")
ws_sync = pytest.importorskip("websockets.sync.client")

CLOUD = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(__file__).parent / "artifacts"
DEVICE_ID = "PM-0A1B2C"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lan_ip() -> str | None:
    sys.path.insert(0, str(CLOUD))
    from app import netinfo
    hosts = netinfo.lan_hosts()
    return hosts[0] if hosts else None


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("srv")
    port = _free_port()
    env = dict(os.environ)
    env.pop("PETMATE_NO_AUTOAPP", None)
    env.update({
        "JWT_SECRET": "browser-test-secret-browser-test-secret-0123",
        "ALLOW_TOFU": "true", "PORT": str(port), "STUN_URLS": "none", "TURN_URLS": "",
        "DATABASE_URL": f"sqlite:///{tmp / 'b.db'}", "STORAGE_DIR": str(tmp / "clips"),
        "RATE_LIMIT_ENABLED": "false", "LOG_LEVEL": "WARNING",
    })
    log = open(tmp / "server.log", "wb")  # a file, not a pipe: an undrained pipe would block uvicorn
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
        cwd=CLOUD, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    base = f"http://localhost:{port}"
    deadline = time.time() + 30
    while True:
        if proc.poll() is not None or time.time() > deadline:
            proc.kill()
            pytest.fail("dev server did not start: " + (tmp / "server.log").read_text(errors="replace")[-2000:])
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    yield {"base": base, "port": port}
    proc.terminate()
    proc.wait(10)
    log.close()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            # direct connections: ignore any HTTP(S)_PROXY of the CI/sandbox (LAN-IP WebSocket)
            env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
            b = p.chromium.launch(args=["--no-proxy-server"], env=env)
        except Exception as e:  # browser not installed
            pytest.skip(f"chromium not available: {e}")
        yield b
        b.close()


class FakeDevice:
    """Device side of the signaling WS (no WebRTC): records messages, optional auto-reply."""

    def __init__(self, base: str, token: str, reply=None):
        self.ws = ws_sync.connect(base.replace("http", "ws") + "/v1/signal", additional_headers={"Authorization": f"Bearer {token}"})
        self.msgs: list[dict] = []
        self.reply = reply
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        try:
            for raw in self.ws:
                m = json.loads(raw)
                self.msgs.append(m)
                if self.reply and m.get("type") == "offer":
                    out = self.reply(m)
                    if out:
                        self.ws.send(json.dumps(out))
        except Exception:
            pass

    def wait_for(self, typ: str, timeout=10.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            for m in self.msgs:
                if m.get("type") == typ:
                    return m
            time.sleep(0.05)
        raise AssertionError(f"no {typ} received; got {self.msgs}")

    def close(self):
        self.ws.close()


def _errors(page) -> list[str]:
    return page.evaluate("window.__petmate.errors")


def test_console_flow(server, browser):
    ARTIFACTS.mkdir(exist_ok=True)
    base = server["base"]
    ctx = browser.new_context(viewport={"width": 1100, "height": 760}, locale="it-IT")
    page = ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    # the deliberate wrong login logs "Failed to load resource ... 401": not a JS error
    page.on("console", lambda m: m.type == "error" and "status of 401" not in m.text and page_errors.append(m.text))

    # ---- load: login page, no JS errors
    page.goto(f"{base}/console/")
    page.wait_for_selector("#login-email")
    assert page.evaluate("window.__petmate.view") == "login"
    page.screenshot(path=str(ARTIFACTS / "console-login.png"))

    # ---- wrong password on unknown user → Italian error
    page.fill("#login-email", "nobody@example.com")
    page.fill("#login-password", "password123")
    page.click("#login-submit")
    page.wait_for_function("document.querySelector('#login-error').textContent.length > 0")
    assert "errati" in page.text_content("#login-error")

    # ---- register → robots list (empty)
    page.fill("#login-email", "tester@example.com")
    page.fill("#login-password", "password123")
    page.click("#register-submit")
    page.wait_for_selector("#robots-list[data-loaded=true]", state="attached")
    assert page.is_visible("#robots-empty")
    page.wait_for_selector("#signal-state[data-state=open]")
    token = page.evaluate("JSON.parse(sessionStorage.getItem('petmate.console.session')).token")

    # ---- logout + login
    page.click("#logout")
    page.wait_for_selector("#login-email")
    page.fill("#login-email", "tester@example.com")
    page.fill("#login-password", "password123")
    page.click("#login-submit")
    page.wait_for_selector("#robots-list[data-loaded=true]", state="attached")
    token = page.evaluate("JSON.parse(sessionStorage.getItem('petmate.console.session')).token")

    # ---- claim a device over REST (what pm-core does after BLE provisioning)
    h = {"Authorization": f"Bearer {token}"}
    ct = httpx.post(f"{base}/v1/claim-tokens", headers=h).json()["claim_token"]
    r = httpx.post(f"{base}/v1/devices/register",
                   json={"device_id": DEVICE_ID, "device_secret": "s3cret-s3cret", "claim_token": ct, "fw": "0.1.0"})
    assert r.status_code == 201, r.text
    dtoken = r.json()["device_token"]

    # device connects → the row appears and goes online live (presence)
    dev = FakeDevice(base, dtoken)
    row = f".robot-row[data-device-id='{DEVICE_ID}']"
    page.wait_for_selector(f"{row}[data-online=true]", timeout=10000)
    page.screenshot(path=str(ARTIFACTS / "console-robots.png"))
    dev.close()
    page.wait_for_selector(f"{row}[data-online=false]", timeout=10000)

    # ---- rename via prompt
    page.once("dialog", lambda d: d.accept("Petmate cucina"))
    page.click(f"{row} .robot-rename")
    page.wait_for_function(f"document.querySelector(\"{row} .robot-name\").textContent === 'Petmate cucina'")

    # ---- live: device offline → peer_offline
    page.click(f"{row} .robot-live")
    page.wait_for_selector("#live-video", state="attached")
    page.wait_for_function("window.__petmate.live && window.__petmate.live.error === 'peer_offline'", timeout=15000)
    assert page.get_attribute("#live-status", "data-state") == "error"
    assert "offline" in page.text_content("#live-error")

    # ---- live: device online, receives a complete non-trickle offer, answers with bye
    dev = FakeDevice(base, dtoken, reply=lambda m: {"type": "bye", "to": m["from"]})
    page.wait_for_function("window.__petmate.signal.online.get('%s') === true" % DEVICE_ID)
    page.click("#live-connect")
    offer = dev.wait_for("offer")
    sdp = offer["sdp"]
    assert offer["from"].startswith("user:")
    assert "m=video" in sdp and "a=recvonly" in sdp
    assert "webrtc-datachannel" in sdp           # DataChannel "ctl" negotiated in the offer
    assert "a=candidate:" in sdp                 # ICE gathering completed before sending
    page.wait_for_function("window.__petmate.live.state === 'closed'")
    v = page.evaluate("window.__petmate.live.video()")
    assert set(v) >= {"readyState", "videoWidth", "videoHeight", "paused", "hasStream"}

    # ---- live: no answer → timeout after 15 s
    dev.reply = None
    page.click("#live-connect")
    page.wait_for_function("window.__petmate.live.error === 'timeout'", timeout=25000)
    assert "15 s" in page.text_content("#live-error")
    bye = [m for m in dev.msgs if m.get("type") == "bye"]
    assert bye, "console must send bye to the device after the timeout"
    dev.close()

    # ---- wizard on localhost: Web Bluetooth unavailable in headless Chromium → clear message
    page.goto(f"{base}/console/#/add")
    page.wait_for_selector("#wizard")
    if not page.evaluate("!!navigator.bluetooth"):
        page.wait_for_selector("#ble-unavailable")
        assert "http://localhost:8000/console/" in page.text_content("#ble-unavailable")
        assert "Chrome o Edge" in page.text_content("#ble-unavailable")
    page.screenshot(path=str(ARTIFACTS / "console-wizard.png"))

    assert not _errors(page), _errors(page)
    assert not page_errors, page_errors
    ctx.close()


def test_wizard_message_on_insecure_lan_origin(server, browser):
    """From a LAN IP the page is not a secure context: no Web Bluetooth, explain why."""
    ip = _lan_ip()
    if not ip:
        pytest.skip("no LAN address")
    ctx = browser.new_context()
    page = ctx.new_page()
    lan = f"http://{ip}:{server['port']}"
    r = httpx.post(f"{lan}/v1/auth/register", json={"email": "lan@example.com", "password": "password123"})
    assert r.status_code == 201
    page.goto(f"{lan}/console/")
    page.evaluate("t => sessionStorage.setItem('petmate.console.session', JSON.stringify({token: t, email: 'lan@example.com'}))",
                  r.json()["access_token"])
    page.goto(f"{lan}/console/?reload=1#/add")  # new document: picks up the stored session
    page.wait_for_selector("#ble-unavailable")
    assert page.evaluate("window.isSecureContext") is False
    assert "http://localhost:8000/console/" in page.text_content("#ble-unavailable")
    page.wait_for_selector("#signal-state[data-state=open]")  # WebSocket over plain ws:// on the LAN IP
    assert not _errors(page)
    ctx.close()


def test_provisioning_crypto_roundtrip(server, browser):
    """Browser-side §7 encryption (vendored noble libs) decrypts with Python `cryptography`
    exactly like pm-core; chunks ≤ 180 bytes including the flags byte."""
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.x25519")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    priv = crypto.X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    info = {"device_id": "PM-00C0DE", "model": "PM-EVT1", "fw": "0.1.0", "pubkey": base64.b64encode(pub).decode()}
    plaintext = {"ssid": "Casa Rossi è WiFi", "password": "p4ssword!", "claim_token": "x" * 43,
                 "cloud_url": "http://192.168.1.50:8000/v1", "timezone": "Europe/Rome"}

    page = browser.new_page()
    page.goto(server["base"] + "/console/")
    page.wait_for_function("window.__petmate && window.__petmate.prov")
    out = page.evaluate("""([info, pt]) => {
        const p = window.__petmate.prov;
        const msg = p.encryptPayload(info, pt);
        const chunks = p.chunkMessage(msg);
        return { msg: Array.from(msg), chunks: chunks.map(c => Array.from(c)) };
    }""", [info, plaintext])
    page.close()

    msg = bytes(out["msg"])
    chunks = [bytes(c) for c in out["chunks"]]
    assert len(chunks) >= 2 and all(len(c) <= 180 for c in chunks)
    assert all(len(c) == 180 for c in chunks[:-1])
    assert chunks[0][0] & 1 and not chunks[0][0] & 2
    assert chunks[-1][0] & 2 and not chunks[-1][0] & 1
    assert all(c[0] == 0 for c in chunks[1:-1])
    assert b"".join(c[1:] for c in chunks) == msg

    env = json.loads(msg)
    assert set(env) == {"epk", "nonce", "ct"}
    epk, nonce, ct = (base64.b64decode(env[k], validate=True) for k in ("epk", "nonce", "ct"))
    assert len(epk) == 32 and len(nonce) == 12
    shared = priv.exchange(crypto.X25519PublicKey.from_public_bytes(epk))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"PM-00C0DE", info=b"petmate-prov-v1").derive(shared)
    pt = ChaCha20Poly1305(key).decrypt(nonce, ct, b"PM-00C0DE")
    assert json.loads(pt) == plaintext


# ---------------------------------------------------------------------------------------------
# Wizard end-to-end with a mocked navigator.bluetooth; Python plays pm-core (decrypt + register).

FAKE_BLE_JS = r"""
(() => {
  const SVC = '8f1d0000-5e7a-4c3a-9d8e-7e7f1a2b3c4d';
  const enc = new TextEncoder();
  const dv = (obj) => { const b = enc.encode(JSON.stringify(obj)); return new DataView(b.buffer); };
  const statusListeners = [];
  const fake = window.__fakeBle = { info: null, networks: [], status: { state: 'idle', error: null, ip: null }, requestOptions: null };
  fake.emit = (s) => { fake.status = s; statusListeners.forEach((fn) => fn({ target: { value: dv(s) } })); };
  const char = (uuid, read, extra = {}) => ({
    uuid, readValue: async () => dv(read()),
    startNotifications: async () => {},
    addEventListener: (t, fn) => { if (uuid.startsWith('8f1d0004')) statusListeners.push(fn); },
    ...extra,
  });
  const chars = {
    '8f1d0001-5e7a-4c3a-9d8e-7e7f1a2b3c4d': char('8f1d0001', () => fake.info),
    '8f1d0002-5e7a-4c3a-9d8e-7e7f1a2b3c4d': char('8f1d0002', () => fake.networks),
    '8f1d0003-5e7a-4c3a-9d8e-7e7f1a2b3c4d': char('8f1d0003', () => ({}), {
      writeValueWithResponse: async (v) => {
        const statuses = await window.fakeBleWrite(Array.from(v));
        for (const s of statuses) { await new Promise((r) => setTimeout(r, 150)); fake.emit(s); }
      },
    }),
    '8f1d0004-5e7a-4c3a-9d8e-7e7f1a2b3c4d': char('8f1d0004', () => fake.status),
  };
  const service = { getCharacteristic: async (u) => chars[u] };
  const device = {
    name: 'Petmate-C0DE', addEventListener() {},
    gatt: { connected: false,
      async connect() { this.connected = true; return { getPrimaryService: async (u) => { if (u !== SVC) throw new Error('svc'); return service; } }; },
      disconnect() { this.connected = false; } },
  };
  Object.defineProperty(navigator, 'bluetooth', { value: { requestDevice: async (o) => { fake.requestOptions = o; return device; } } });
})();
"""


def test_wizard_provisioning_with_fake_ble(server, browser):
    x = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.x25519")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    device_id = "PM-00C0DE"
    priv = x.X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    info = {"device_id": device_id, "model": "PM-EVT1", "fw": "0.1.0", "pubkey": base64.b64encode(pub).decode()}
    networks = [{"ssid": "Casa", "rssi": -52, "secure": True}, {"ssid": "Ospiti", "rssi": -70, "secure": False}]
    buf = bytearray()
    received: list[dict] = []
    state = {"device_token": None}

    def on_write(chunk: list[int]) -> list[dict]:
        """pm-core stand-in: reassemble, decrypt (§7), 'connect' Wi-Fi, register on cloud_url."""
        c = bytes(chunk)
        assert len(c) <= 180
        if c[0] & 1:
            buf.clear()
        buf.extend(c[1:])
        if not c[0] & 2:
            return [{"state": "receiving", "error": None, "ip": None}] if c[0] & 1 else []
        env = json.loads(bytes(buf))
        epk, nonce, ct = (base64.b64decode(env[k]) for k in ("epk", "nonce", "ct"))
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=device_id.encode(),
                   info=b"petmate-prov-v1").derive(priv.exchange(x.X25519PublicKey.from_public_bytes(epk)))
        p = json.loads(ChaCha20Poly1305(key).decrypt(nonce, ct, device_id.encode()))
        received.append(p)
        steps = [{"state": "connecting", "error": None, "ip": None}]
        if p["password"] != "password-ok":
            return steps + [{"state": "error", "error": "wifi_auth_failed", "ip": None}]
        ip = "192.168.1.77"
        steps += [{"state": "wifi_ok", "error": None, "ip": ip}, {"state": "registering", "error": None, "ip": ip}]
        r = httpx.post(p["cloud_url"].rstrip("/") + "/devices/register",
                       json={"device_id": device_id, "device_secret": "pi-secret-123", "claim_token": p["claim_token"]})
        if r.status_code != 201:
            return steps + [{"state": "error", "error": "claim_invalid", "ip": ip}]
        state["device_token"] = r.json()["device_token"]
        return steps + [{"state": "done", "error": None, "ip": ip}]

    ctx = browser.new_context(viewport={"width": 1100, "height": 900})
    ctx.add_init_script(FAKE_BLE_JS)
    ctx.expose_function("fakeBleWrite", on_write)
    page = ctx.new_page()
    base = server["base"]
    tok = httpx.post(f"{base}/v1/auth/register", json={"email": "wiz@example.com", "password": "password123"}).json()["access_token"]
    page.goto(f"{base}/console/")
    page.evaluate("""([t, info, nets]) => {
        sessionStorage.setItem('petmate.console.session', JSON.stringify({token: t, email: 'wiz@example.com'}));
    }""", [tok, info, networks])
    page.goto(f"{base}/console/?r=1#/add")
    page.evaluate("([info, nets]) => { window.__fakeBle.info = info; window.__fakeBle.networks = nets; }", [info, networks])

    page.click("#ble-scan")
    req = page.evaluate("window.__fakeBle.requestOptions")
    assert req["filters"] == [{"namePrefix": "Petmate-"}]
    assert req["optionalServices"] == ["8f1d0000-5e7a-4c3a-9d8e-7e7f1a2b3c4d"]
    page.wait_for_selector("#wifi-list li[data-ssid='Casa']")
    assert device_id in page.text_content("#device-info")
    cloud_url = page.input_value("#cloud-url")
    assert cloud_url.startswith("http://") and cloud_url.endswith("/v1") and "localhost" not in cloud_url

    # 1st attempt: wrong Wi-Fi password → Italian error, retry
    page.click("#wifi-list li[data-ssid='Casa']")
    assert page.input_value("#wifi-ssid") == "Casa"
    page.fill("#wifi-password", "wrong-pass")
    page.click("#prov-send")
    page.wait_for_selector("#prov-error")
    assert "Password Wi-Fi errata" in page.text_content("#prov-error")
    page.click("#prov-retry")

    # 2nd attempt: hidden network typed by hand, good password → done; robot registers on cloud_url
    page.wait_for_selector("#wifi-ssid")
    page.fill("#wifi-ssid", "Rete nascosta")
    page.fill("#wifi-password", "password-ok")
    page.click("#prov-send")
    page.wait_for_selector("#prov-done")
    assert "192.168.1.77" in page.text_content("#prov-done")
    p = received[-1]
    assert p["ssid"] == "Rete nascosta" and p["cloud_url"] == cloud_url and p["timezone"]
    assert received[0]["claim_token"] != p["claim_token"]  # fresh claim token per attempt
    # robot comes online on the signaling WS → wizard and list show it
    dev = FakeDevice(base, state["device_token"])
    page.wait_for_function("document.querySelector('#prov-online').textContent.includes('online')")
    page.screenshot(path=str(ARTIFACTS / "console-wizard-done.png"))
    page.click("#prov-goto-robots")
    page.wait_for_selector(f".robot-row[data-device-id='{device_id}'][data-online=true]")
    dev.close()
    assert not _errors(page), _errors(page)
    ctx.close()
