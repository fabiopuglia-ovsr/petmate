"""Provisioning robustness (§7, MVP): error codes, retry inside the same window,
http cloud_url, nmcli handling, and an in-process end-to-end run:
provision_cli (central) ⇄ loopback "BLE" ⇄ pm-core App(profile=mvp) ⇄ fake cloud
(aiohttp) incl. signaling over ws:// derived from the provisioned http:// URL."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from pmcore.ble import wifi  # noqa: E402
from pmcore.ble.provisioning import Provisioner, register_error_code  # noqa: E402
from pmcore.cloud.api import CloudError  # noqa: E402

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def load_cli():
    spec = importlib.util.spec_from_file_location("provision_cli", TOOLS / "provision_cli.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["provision_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


cli = load_cli()
DEVICE_ID = "PM-A1B2C3"


class LinkToProv:
    """Minimal central link straight onto a Provisioner (unit tests)."""

    def __init__(self, prov):
        from pmcore.ble.loopback import LoopbackGattServer

        self.srv = LoopbackGattServer(prov, "Petmate-B2C3")

    async def __aenter__(self):
        await self.srv.start()
        return self.srv

    async def __aexit__(self, *a):
        await self.srv.stop()


def run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ------------------------------------------------------------------ error codes
@pytest.mark.parametrize("code", ["wifi_auth_failed", "wifi_not_found", "wifi_timeout", "wifi_unavailable"])
def test_wifi_errors_reported_and_window_stays_open_for_retry(code):
    async def go():
        attempts = []

        async def wifi_connect(ssid, pw):
            attempts.append(pw)
            if len(attempts) == 1:
                raise wifi.WifiError(code, "simulated")
            return "192.168.1.77"

        async def register(claim, url):
            return {"device_token": "t", "owner_user_id": "1"}

        prov = Provisioner(DEVICE_ID, wifi_connect, lambda: asyncio.sleep(0, []), register)
        async with LinkToProv(prov) as link:
            seen = []
            st = await cli.provision(link, "Casa", "password1", "ct", None, on_status=seen.append)
            assert st == {"state": "error", "error": code, "ip": None}
            assert [s["state"] for s in seen][-2:] == ["connecting", "error"]
            # same window (same pubkey), user fixes the password and retries
            st = await cli.provision(link, "Casa", "password2", "ct", None)
            assert st == {"state": "done", "error": None, "ip": "192.168.1.77"}
            assert attempts == ["password1", "password2"]

    run(go())


@pytest.mark.parametrize("exc,code", [
    (CloudError(400, "invalid claim token"), "claim_invalid"),
    (CloudError(400, "claim token expired"), "claim_invalid"),
    (CloudError(401, "unknown device or bad secret"), "device_rejected"),
    (CloudError(503, "down"), "cloud_unreachable"),
    (aiohttp.ClientConnectionError("refused"), "cloud_unreachable"),
    (asyncio.TimeoutError(), "cloud_unreachable"),
    (OSError(113, "No route to host"), "cloud_unreachable"),
    (RuntimeError("?"), "register_failed"),
])
def test_register_error_codes(exc, code):
    assert register_error_code(exc) == code

    async def go():
        calls = []

        async def register(claim, url):
            calls.append(claim)
            raise exc

        prov = Provisioner(DEVICE_ID, lambda s, p: asyncio.sleep(0, "10.0.0.5"), lambda: asyncio.sleep(0, []),
                           register, register_attempts=3, register_retry_s=0.01)
        async with LinkToProv(prov) as link:
            st = await cli.provision(link, "Casa", "password1", "ct", None)
        assert st == {"state": "error", "error": code, "ip": "10.0.0.5"}
        # network-type errors are retried (DNS/route settle after association)
        assert len(calls) == (3 if code == "cloud_unreachable" else 1)

    run(go())


def test_transient_cloud_error_then_success():
    async def go():
        n = {"c": 0}

        async def register(claim, url):
            n["c"] += 1
            if n["c"] == 1:
                raise aiohttp.ClientConnectionError("network not ready")
            return {}

        prov = Provisioner(DEVICE_ID, lambda s, p: asyncio.sleep(0, "10.0.0.5"), lambda: asyncio.sleep(0, []),
                           register, register_retry_s=0.01)
        async with LinkToProv(prov) as link:
            seen = []
            st = await cli.provision(link, "Casa", "", "ct", None, on_status=seen.append)
        assert st["state"] == "done" and n["c"] == 2
        assert "error" not in [s["state"] for s in seen]

    run(go())


def test_http_cloud_url_only_when_allowed():
    async def go():
        got = []

        async def register(claim, url):
            got.append(url)
            return {}

        for allow, expect in ((False, "payload_invalid"), (True, "done")):
            prov = Provisioner(DEVICE_ID, lambda s, p: asyncio.sleep(0, "10.0.0.5"), lambda: asyncio.sleep(0, []),
                               register, allow_http=allow)
            async with LinkToProv(prov) as link:
                st = await cli.provision(link, "Casa", "password1", "ct", "http://192.168.1.50:8000/v1")
            assert (st["error"] or st["state"]) == expect
        assert got == ["http://192.168.1.50:8000/v1"]

    run(go())


def test_decrypt_error_code():
    async def go():
        prov = Provisioner(DEVICE_ID, None, lambda: asyncio.sleep(0, []), None)
        async with LinkToProv(prov) as link:
            info = json.loads(await link.read(cli.CHAR_DEVICE_INFO))
            info["device_id"] = "PM-OTHER1"  # wrong salt/AAD
            st = await cli.provision(link, "Casa", "password1", "ct", None, device_info=info)
        assert st["state"] == "error" and st["error"] == "decrypt"

    run(go())


# ------------------------------------------------------------------ nmcli
@pytest.mark.parametrize("rc,err,code", [
    (4, "Error: Connection activation failed: Secrets were required, but not provided.", "wifi_auth_failed"),
    (4, "Error: Connection activation failed: (7) Secrets were required, but not provided.", "wifi_auth_failed"),
    (4, "Error: Connection activation failed: The Wi-Fi network could not be found.", "wifi_not_found"),
    (10, "Error: No network with SSID 'Casa psk' found.", "wifi_not_found"),
    (3, "Error: Timeout expired (45 seconds)", "wifi_timeout"),
    (124, "timeout", "wifi_timeout"),
    (4, "Error: Connection activation failed: No suitable device found for this connection "
        "(device wlan0 not available because device is strictly unmanaged).", "wifi_unavailable"),
    (2, "Error: 802-11-wireless-security.psk: property is invalid.", "wifi_auth_failed"),
    (1, "Error: something else", "wifi_failed"),
])
def test_classify_nmcli_errors(rc, err, code):
    assert wifi.classify_nmcli_error(rc, err) == code


class FakeNmcli:
    def __init__(self, up_rc=0, up_err=""):
        self.calls = []
        self.up_rc, self.up_err = up_rc, up_err

    async def __call__(self, *args, timeout=30.0, input_data=None):
        self.calls.append(list(args))
        if args[:2] == ("nmcli", "-t") and "wifi" in args:
            return 0, "Casa:80:WPA2\nCasa3:60:WPA3\nOpen:40:\n", ""
        if "up" in args:
            return self.up_rc, "", self.up_err
        if "IP4.ADDRESS" in args:
            return 0, "IP4.ADDRESS[1]:192.168.1.77/24\n", ""
        return 0, "", ""


def test_nmcli_connect_flow(monkeypatch):
    async def go():
        fake = FakeNmcli()
        monkeypatch.setattr(wifi, "run_cmd", fake)
        nets = await wifi.scan("wlan0")
        assert [n["ssid"] for n in nets] == ["Casa", "Casa3", "Open"]
        assert await wifi.connect("Casa3", "password1", "wlan0") == "192.168.1.77"
        add = next(c for c in fake.calls if "add" in c)
        assert add[add.index("con-name") + 1] == "petmate-wifi-new"
        assert add[add.index("wifi-sec.key-mgmt") + 1] == "sae"  # WPA3-only
        assert "802-11-wireless.hidden" not in add
        assert ["nmcli", "connection", "modify", "petmate-wifi-new", "connection.id", "petmate-wifi"] in fake.calls
        # not in scan → hidden network, WPA2 default key-mgmt
        fake.calls.clear()
        await wifi.connect("Hidden", "password1", "wlan0")
        add = next(c for c in fake.calls if "add" in c)
        assert add[add.index("802-11-wireless.hidden") + 1] == "yes"
        assert add[add.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
        # wrong password: error code, temporary profile removed, old profile untouched
        bad = FakeNmcli(4, "Error: Connection activation failed: Secrets were required, but not provided.")
        monkeypatch.setattr(wifi, "run_cmd", bad)
        with pytest.raises(wifi.WifiError) as ei:
            await wifi.connect("Casa", "badpassword", "wlan0")
        assert ei.value.code == "wifi_auth_failed"
        assert ["nmcli", "connection", "delete", "petmate-wifi-new"] in bad.calls[2:]
        assert ["nmcli", "connection", "delete", "petmate-wifi"] not in bad.calls

    run(go())


# ------------------------------------------------------------------ E2E in-process
class FakeCloud:
    def __init__(self):
        self.registered = []
        self.signal_tokens = []
        self.app = web.Application()
        r = self.app.router
        r.add_post("/v1/devices/register", self.register)
        r.add_post("/v1/devices/token", self.token)
        r.add_get("/v1/ice-servers", self.ice)
        r.add_get("/v1/devices/{id}/config", self.config)
        r.add_post("/v1/devices/{id}/config/reported", self.reported)
        r.add_get("/v1/signal", self.signal)
        self.connected = asyncio.Event()

    async def ice(self, req):
        return web.json_response({"ice_servers": []})

    async def config(self, req):
        return web.json_response({"detail": "no config"}, status=404)

    async def reported(self, req):
        return web.Response(status=204)

    async def register(self, req):
        b = await req.json()
        if b.get("claim_token") != "good":
            return web.json_response({"detail": "invalid claim token"}, status=400)
        self.registered.append(b)
        return web.json_response({"device_token": "a.eyJleHAiOjk5OTk5OTk5OTl9.c", "owner_user_id": 7}, status=201)

    async def token(self, req):
        return web.json_response({"device_token": "a.eyJleHAiOjk5OTk5OTk5OTl9.c"})

    async def signal(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.signal_tokens.append(req.query.get("token"))
        await ws.send_json({"type": "hello", "peer": f"device:{DEVICE_ID}"})
        self.connected.set()
        async for _ in ws:
            pass
        return ws


def test_e2e_provisioning_loopback_ble_fake_cloud(tmp_path):
    from pmcore.app import App
    from pmcore.ble import loopback
    from pmcore.paths import Paths
    from pmcore.settings import Settings

    async def go():
        cloud = FakeCloud()
        runner = web.AppRunner(cloud.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/v1"

        (tmp_path / "etc/petmate").mkdir(parents=True)
        (tmp_path / "etc/petmate/identity.json").write_text(json.dumps(
            {"device_id": DEVICE_ID, "device_secret": "x" * 43}))
        st = Settings.load(None, env={}, overrides={
            "profile": "mvp",
            "camera": {"backend": "lavfi", "main_size": [320, 240]},
            "ble": {"transport": "loopback", "wifi": "fake"},
        })
        app = App(Paths(tmp_path), st, enable_hw=False)
        await app.start()
        try:
            assert app.ble.active and app.ble.ble_name == "Petmate-B2C3"
            link = loopback.get("Petmate-B2C3")
            assert link is app.ble.server
            nets = await cli.list_networks(link)
            assert nets[0]["ssid"] == "PetmateTest"

            # wrong Wi-Fi password, then wrong claim token: both errors, window stays open
            st1 = await cli.provision(link, "PetmateTest", "wrongpassword", "good", url)
            assert st1["error"] == "wifi_auth_failed"
            st2 = await cli.provision(link, "PetmateTest", "password1", "expired", url)
            assert st2 == {"state": "error", "error": "claim_invalid", "ip": "127.0.0.1"}
            assert app.ble.active and not app.cloud_state.provisioned

            seen = []
            st3 = await cli.provision(link, "PetmateTest", "password1", "good", url, on_status=seen.append)
            assert st3 == {"state": "done", "error": None, "ip": "127.0.0.1"}
            assert [s["state"] for s in seen] == ["receiving", "connecting", "wifi_ok", "registering", "done"]
            assert cloud.registered[0]["device_id"] == DEVICE_ID

            # cloud_url persisted (http allowed in mvp) and used by signaling (ws://)
            saved = json.loads((tmp_path / "var/lib/petmate/cloud.json").read_text())
            assert saved["cloud_url"] == url and saved["owner_user_id"] == "7"
            assert app.config.get("timezone") == "Europe/Rome"
            await asyncio.wait_for(cloud.connected.wait(), 10)
            assert cloud.signal_tokens[0].startswith("a.")
            # BLE advertising stops after success
            for _ in range(50):
                if not app.ble.active:
                    break
                await asyncio.sleep(0.1)
            assert not app.ble.active and loopback.get("Petmate-B2C3") is None

            # the provisioned http:// URL survives a restart
            from pmcore.identity import CloudState

            assert CloudState.load(tmp_path / "var/lib/petmate/cloud.json").cloud_url == url
        finally:
            await app.shutdown()
            await runner.cleanup()

    run(go(), 90)


def test_cloud_url_override_env(tmp_path):
    from pmcore.app import App
    from pmcore.paths import Paths
    from pmcore.settings import Settings

    st = Settings.load(None, env={"PMCORE_PROFILE": "mvp", "PMCORE_CLOUD_URL": "http://10.1.2.3:8000/v1/"})
    app = App(Paths(tmp_path), st, enable_hw=False)
    assert app.cloud_state.cloud_url == "http://10.1.2.3:8000/v1"
    from pmcore.cloud.signaling import signal_url

    assert signal_url(app.cloud_state.cloud_url, "t").startswith("ws://10.1.2.3:8000/v1/signal?token=")
