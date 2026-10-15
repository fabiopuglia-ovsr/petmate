"""Signaling client with a fake websocket transport."""

import asyncio
import json

from pmcore.cloud.signaling import SignalingClient, signal_url
from pmcore.util import Backoff


class FakeWs:
    def __init__(self, incoming):
        self.inbox: asyncio.Queue = asyncio.Queue()
        for m in incoming:
            self.inbox.put_nowait(json.dumps(m) if isinstance(m, dict) else m)
        self.sent = []
        self.closed = False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if self.closed:
            return None
        m = await self.inbox.get()
        return m

    def end(self):
        self.inbox.put_nowait(None)

    async def close(self):
        self.closed = True
        self.inbox.put_nowait(None)


class FakeHandler:
    def __init__(self, answer="v=0 answer"):
        self.answer = answer
        self.offers, self.ices, self.byes, self.cmds, self.cfgs = [], [], [], [], []

    async def on_offer(self, peer, sdp):
        self.offers.append((peer, sdp))
        await asyncio.sleep(0.01)  # emulate ICE gathering
        return self.answer

    async def on_ice(self, peer, cand):
        self.ices.append((peer, cand))

    async def on_bye(self, peer):
        self.byes.append(peer)

    async def on_command(self, cmd):
        self.cmds.append(cmd)

    async def on_config_set(self, cfg):
        self.cfgs.append(cfg)


def mk(handler, connector=None, **kw):
    async def tok(force):
        return "tok.en"

    async def default_connector(url):
        raise AssertionError("not used")

    return SignalingClient(lambda: "https://api.petmate.example/v1", tok, handler,
                           connector=connector or default_connector, **kw)


def test_signal_url():
    assert signal_url("https://api.petmate.example/v1", "a.b c") == "wss://api.petmate.example/v1/signal?token=a.b%20c"
    assert signal_url("http://127.0.0.1:8000/v1/", "t") == "ws://127.0.0.1:8000/v1/signal?token=t"


def test_message_handling():
    async def go():
        h = FakeHandler()
        c = mk(h)
        ws = FakeWs([
            {"type": "hello", "peer": "device:PM-0001"},
            {"type": "offer", "from": "user:42:s1", "to": "device:PM-0001", "sdp": "v=0 offer"},
            {"type": "ice", "from": "user:42:s1", "candidate": {"candidate": "candidate:1 1 udp 1 1.2.3.4 5 typ host",
                                                                "sdpMid": "0", "sdpMLineIndex": 0}},
            {"type": "command", "cmd": {"t": "feed", "grams": 10}},
            {"type": "config_set", "config": {"audio": {"speaker_volume": 20}}},
            {"type": "ping"},
            {"type": "presence", "device_id": "x", "online": True},
            {"type": "error", "error": "nope"},
            "not json",
            {"type": "offer", "from": "device:evil", "sdp": "x"},  # not a user → ignored
            {"type": "bye", "from": "user:42:s1"},
        ])
        task = asyncio.ensure_future(c.run_session(ws))
        await asyncio.sleep(0.05)
        await c.drain()
        assert c.peer == "device:PM-0001" and c.connected.is_set()
        ws.end()
        await task
        assert h.offers == [("user:42:s1", "v=0 offer")]
        assert h.ices[0][0] == "user:42:s1" and h.ices[0][1]["sdpMid"] == "0"
        assert h.cmds == [{"t": "feed", "grams": 10}]
        assert h.cfgs == [{"audio": {"speaker_volume": 20}}]
        assert h.byes == ["user:42:s1"]
        assert {"type": "pong"} in ws.sent
        assert {"type": "answer", "to": "user:42:s1", "sdp": "v=0 answer"} in ws.sent
        assert not c.connected.is_set()

    asyncio.run(go())


def test_rejected_offer_sends_bye():
    async def go():
        h = FakeHandler(answer=None)
        c = mk(h)
        ws = FakeWs([{"type": "offer", "from": "user:1:x", "sdp": "o"}])
        t = asyncio.ensure_future(c.run_session(ws))
        await asyncio.sleep(0.05)
        await c.drain()
        ws.end()
        await t
        assert ws.sent == [{"type": "bye", "to": "user:1:x", "reason": "rejected"}]

    asyncio.run(go())


def test_ping_and_pong_timeout_closes_socket():
    async def go():
        c = mk(FakeHandler(), ping_interval_s=0.05, pong_timeout_s=0.05)
        ws = FakeWs([{"type": "hello", "peer": "device:PM-0001"}])
        t = asyncio.ensure_future(c.run_session(ws))
        await asyncio.wait_for(t, 2.0)  # silence → closes itself
        assert ws.closed
        assert {"type": "ping"} in ws.sent

    asyncio.run(go())


def test_reconnect_with_backoff_and_token_refresh_on_401():
    async def go():
        attempts = []
        forced = []

        class Http401(Exception):
            status = 401

        sockets = []

        async def connector(url):
            attempts.append(url)
            if len(attempts) == 1:
                raise Http401("unauthorized")
            if len(attempts) == 2:
                raise ConnectionError("down")
            ws = FakeWs([{"type": "hello", "peer": "device:PM-0001"}])
            sockets.append(ws)
            return ws

        async def tok(force):
            forced.append(force)
            return f"t{len(forced)}"

        c = SignalingClient(lambda: "https://x/v1", tok, FakeHandler(), connector=connector,
                            backoff=Backoff(0.01, 0.02))
        c.start()
        for _ in range(100):
            if c.connected.is_set():
                break
            await asyncio.sleep(0.01)
        assert c.connected.is_set()
        assert forced[:2] == [False, True]  # 401 → force token refresh on next attempt
        assert len(attempts) == 3
        await c.send({"type": "ping"})
        assert sockets[0].sent[-1] == {"type": "ping"}
        await c.stop()
        assert sockets[0].closed

    asyncio.run(go())
