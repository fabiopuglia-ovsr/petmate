"""DataChannel → ZMQ cmd routing, local commands and ROS ack round-trip."""

import asyncio
import json
import uuid

import pytest

from pmcore.commands import CommandRouter, validate_ros_cmd, CommandError
from pmcore.zmq_bridge import decode, encode, ensure_cmd_id


class FakeLocal:
    def __init__(self):
        self.calls = []
        self.cfg = {"version": 1, "audio": {"speaker_volume": 70}}

    async def set_camera(self, cam):
        self.calls.append(("camera", cam))

    async def set_display(self, mode, game):
        self.calls.append(("display", mode, game))

    async def set_volume(self, v):
        self.calls.append(("volume", v))

    async def snapshot_jpeg(self):
        return b"\xff\xd8jpeg"

    async def record_clip(self, s):
        return {"clip": "c1", "duration_s": s}

    def config_get(self):
        return self.cfg

    async def config_set(self, cfg):
        self.cfg = {**self.cfg, **cfg}
        return self.cfg


def make_router(rear_ok=True):
    published = []

    def pub(cmd):
        out = ensure_cmd_id(cmd)
        published.append(out)
        return out

    local = FakeLocal()
    r = CommandRouter(pub, local, portion_max_g=lambda: 40, rear_streaming_allowed=lambda: rear_ok)
    return r, published, local


def run(c):
    return asyncio.run(c)


def test_ros_commands_get_uuid_and_are_published():
    r, pub, _ = make_router()
    replies = []

    async def go():
        sent = await r.handle({"t": "feed", "grams": 20}, replies.append)
        sent2 = await r.handle({"t": "led", "mode": "solid", "color": "#f0a830", "id": "abc"}, replies.append)
        return sent, sent2

    sent, sent2 = run(go())
    assert pub[0]["t"] == "feed" and pub[0]["grams"] == 20
    uuid.UUID(pub[0]["id"])  # valid uuid
    assert sent2["id"] == "abc" and pub[1]["color"] == "#F0A830"
    assert replies == []  # ack comes from ROS
    # ZMQ wire format
    frames = encode("cmd", pub[0])
    assert frames[0] == b"cmd"
    assert decode(frames) == ("cmd", pub[0])


@pytest.mark.parametrize("cmd", [
    {"t": "teleop", "v": 0.2, "w": 0.5},
    {"t": "feed_abort"},
    {"t": "launch", "power": 40, "count": 1},
    {"t": "intake", "mode": "in"},
    {"t": "tease", "pattern": "wiggle", "duration_s": 20},
    {"t": "tease", "pattern": "stop"},
    {"t": "led", "mode": "off"},
    {"t": "goto", "target": "poi", "poi": "cucina"},
    {"t": "goto", "target": "dock"},
    {"t": "mode", "mode": "sleep"},
    {"t": "estop", "engage": True},
    {"t": "play", "game": "screen_birds", "duration_s": 300},
    {"t": "save_poi", "name": "salotto"},
    {"t": "map", "action": "save"},
])
def test_all_spec_ros_commands_valid(cmd):
    out = validate_ros_cmd(cmd, 60)
    for k, v in cmd.items():
        assert out[k] == v


@pytest.mark.parametrize("cmd,err", [
    ({"t": "feed", "grams": 50}, "portion_max_g"),   # > portion_max 40
    ({"t": "feed", "grams": 0}, "grams"),
    ({"t": "teleop", "v": 1.0, "w": 0}, "v must"),
    ({"t": "launch", "power": 80}, "power"),
    ({"t": "estop", "engage": 1}, "boolean"),
    ({"t": "goto", "target": "poi"}, "poi"),
    ({"t": "led", "mode": "solid", "color": "red"}, "color"),
    ({"t": "nope"}, "unknown"),
])
def test_invalid_commands_nacked_not_published(cmd, err):
    r, pub, _ = make_router()
    replies = []
    run(r.handle({**cmd, "id": "x1"}, replies.append))
    assert pub == []
    assert replies[0]["t"] == "ack" and replies[0]["ok"] is False and replies[0]["id"] == "x1"
    assert err in replies[0]["error"]


def test_ros_ack_routed_back_to_originating_channel():
    r, pub, _ = make_router()
    a, b = [], []

    async def go():
        await r.handle({"t": "feed", "grams": 10}, a.append)
        await r.handle({"t": "mode", "mode": "auto"}, b.append)
        assert await r.on_ros_ack({"id": pub[1]["id"], "ok": False, "error": "busy"}) is True
        assert await r.on_ros_ack({"id": pub[0]["id"], "ok": True, "error": None}) is True
        assert await r.on_ros_ack({"id": pub[0]["id"], "ok": True, "error": None}) is False  # once
        assert await r.on_ros_ack({"id": "unknown", "ok": True}) is False

    run(go())
    assert a == [{"t": "ack", "id": pub[0]["id"], "ok": True, "error": None}]
    assert b == [{"t": "ack", "id": pub[1]["id"], "ok": False, "error": "busy"}]


def test_teleop_not_tracked_and_closed_channel_dropped():
    r, pub, _ = make_router()
    a = []

    async def go():
        await r.handle({"t": "teleop", "v": 0.1, "w": 0.0}, a.append)
        await r.handle({"t": "feed", "grams": 10}, a.append)
        r.drop_origin(a.append)  # bound methods compare equal
        return await r.on_ros_ack({"id": pub[1]["id"], "ok": True})

    assert run(go()) is False
    assert a == []


def test_local_commands_not_forwarded():
    r, pub, local = make_router(rear_ok=False)
    rep = []

    async def go():
        await r.handle({"t": "camera", "id": "front"}, rep.append)
        await r.handle({"t": "camera", "id": "rear"}, rep.append)
        await r.handle({"t": "display", "mode": "game", "game": "fish"}, rep.append)
        await r.handle({"t": "volume", "speaker": 55}, rep.append)
        await r.handle({"t": "snapshot"}, rep.append)
        await r.handle({"t": "record_clip", "seconds": 10}, rep.append)
        await r.handle({"t": "config_get"}, rep.append)
        await r.handle({"t": "config_set", "config": {"audio": {"speaker_volume": 10}}}, rep.append)

    run(go())
    assert pub == []
    assert local.calls == [("camera", "front"), ("display", "game", "fish"), ("volume", 55)]
    assert rep[0]["ok"] is True and rep[0]["camera"] == "front"
    assert rep[1]["ok"] is False and "privacy" in rep[1]["error"]
    assert rep[4] == {"t": "snapshot", "jpeg_b64": "/9hqcGVn"}
    assert rep[5]["ok"] and rep[5]["clip"] == "c1"
    assert rep[6]["t"] == "config"
    assert rep[7]["t"] == "ack" and rep[7]["ok"] and rep[8]["config"]["audio"]["speaker_volume"] == 10


def test_zmq_decode_rejects_garbage():
    assert decode([b"cmd"]) is None
    assert decode([b"cmd", b"{bad"]) is None
    assert decode([b"cmd", b"[1,2]"]) is None


def test_zmq_bridge_real_sockets_roundtrip():
    """Real pyzmq PUB/SUB on ephemeral localhost ports: core PUB bind / SUB connect."""
    import zmq
    import zmq.asyncio

    from pmcore.zmq_bridge import ZmqBridge

    async def go():
        ctx = zmq.asyncio.Context.instance()
        ros_pub = ctx.socket(zmq.PUB)
        port_in = ros_pub.bind_to_random_port("tcp://127.0.0.1")
        import socket

        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            port_out = so.getsockname()[1]
        br = ZmqBridge(f"tcp://127.0.0.1:{port_out}", f"tcp://127.0.0.1:{port_in}",
                       config_provider=lambda: {"version": 1})
        got = asyncio.Queue()
        br.on("event", lambda t, p: got.put_nowait((t, p)))
        br.on("ack", lambda t, p: got.put_nowait((t, p)))
        await br.start()
        ros_sub = ctx.socket(zmq.SUB)
        ros_sub.setsockopt(zmq.SUBSCRIBE, b"")
        ros_sub.connect(f"tcp://127.0.0.1:{port_out}")
        await asyncio.sleep(0.3)
        for _ in range(20):
            await ros_pub.send_multipart([b"event", json.dumps({"t": "docked", "ts": 1.0}).encode()])
            await ros_pub.send_multipart([b"bogus", b"{}"])
            try:
                item = await asyncio.wait_for(got.get(), 0.1)
                break
            except asyncio.TimeoutError:
                continue
        assert item == ("event", {"t": "docked", "ts": 1.0})
        sent = br.publish_cmd({"t": "feed_abort"})
        seen = []
        for _ in range(30):
            try:
                fr = await asyncio.wait_for(ros_sub.recv_multipart(), 0.2)
                seen.append(decode(fr))
                if seen[-1][0] == "cmd":
                    break
            except asyncio.TimeoutError:
                br.publish_cmd({"t": "feed_abort", "id": sent["id"]})
        assert ("cmd", {"t": "feed_abort", "id": sent["id"]}) in seen
        await br.stop()
        ros_pub.close(0)
        ros_sub.close(0)

    run(go())
