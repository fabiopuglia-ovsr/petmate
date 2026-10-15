import asyncio
import base64
import json
import time

from aiohttp import web

from pmcore.cloud.api import AuthError, CloudApi
from pmcore.identity import CloudState, Identity, jwt_exp


def fake_jwt(sub, exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'HS256'})}.{enc({'sub': sub, 'typ': 'device', 'exp': exp})}.sig"


def test_cloud_api_flow(tmp_path):
    async def go():
        log = []
        state = {"valid": set(), "n": 0}

        def new_token():
            state["n"] += 1
            t = fake_jwt("PM-0001", time.time() + 86400) + str(state["n"])
            state["valid"] = {t}
            return t

        def auth(req):
            h = req.headers.get("Authorization", "")
            if h[7:] not in state["valid"]:
                raise web.HTTPUnauthorized()

        async def register(req):
            b = await req.json()
            log.append(("register", b))
            if b["claim_token"] != "good":
                return web.json_response({"detail": "invalid claim"}, status=400)
            return web.json_response({"device_token": new_token(), "owner_user_id": 42})

        async def token(req):
            b = await req.json()
            log.append(("token", b["device_id"]))
            return web.json_response({"device_token": new_token()})

        async def events(req):
            auth(req)
            log.append(("event", req.match_info["id"], await req.json()))
            return web.json_response({"ok": True})

        async def ice(req):
            auth(req)
            return web.json_response({"ice_servers": [{"urls": ["stun:x:3478"]},
                                                      {"urls": ["turn:x:3478"], "username": "u", "credential": "c"}]})

        async def clips(req):
            auth(req)
            data = await req.post()
            log.append(("clip", data["file"].filename, json.loads(data["meta"])))
            return web.json_response({"clip_id": "c9"})

        app = web.Application()
        app.router.add_post("/v1/devices/register", register)
        app.router.add_post("/v1/devices/token", token)
        app.router.add_post("/v1/devices/{id}/events", events)
        app.router.add_post("/v1/devices/{id}/clips", clips)
        app.router.add_get("/v1/ice-servers", ice)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        st = CloudState(cloud_url=f"http://127.0.0.1:{port}/v1")
        api = CloudApi(Identity("PM-0001", "s" * 32), st, tmp_path / "cloud.json")
        try:
            try:
                await api.register("bad")
                raise AssertionError("expected error")
            except Exception as e:  # noqa: BLE001
                assert getattr(e, "status", None) == 400
            await api.register("good")
            assert st.provisioned and st.owner_user_id == "42"
            assert json.loads((tmp_path / "cloud.json").read_text())["owner_user_id"] == "42"
            assert jwt_exp(st.device_token) > time.time()

            servers = await api.ice_servers()
            assert servers[1]["username"] == "u"

            # server-side revocation → 401 → transparent refresh via /devices/token
            state["valid"] = set()
            await api.post_event({"t": "docked", "ts": 1.0})
            assert ("token", "PM-0001") in log
            assert ("event", "PM-0001", {"t": "docked", "ts": 1.0}) in log

            # queued events
            await api.start()
            api.enqueue_event({"t": "cliff"})
            for _ in range(50):
                if any(e[0] == "event" and e[2].get("t") == "cliff" for e in log):
                    break
                await asyncio.sleep(0.02)
            ev = [e for e in log if e[0] == "event" and e[2]["t"] == "cliff"][0]
            assert "ts" in ev[2]

            f = tmp_path / "x.mp4"
            f.write_bytes(b"\x00\x00\x00\x18ftypmp42")
            assert await api.upload_clip(f, {"duration_s": 1}) == "c9"
            assert ("clip", "x.mp4", {"duration_s": 1}) in log
        finally:
            await api.close()
            await runner.cleanup()

    asyncio.run(go())
