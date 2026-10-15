"""End-to-end loopback: an aiortc 'app' peer ↔ pmcore WebRtcManager on localhost.

Verifies: non-trickle offer/answer, H.264 negotiated, pre-encoded packets from
the shared encoder decode on the app side, camera switch keeps the stream
alive (no renegotiation), DataChannel `ctl` → ZMQ cmd with id, ack routing,
state broadcast, viewer limit.
"""

import asyncio
import json
import threading
import time

import numpy as np
import pytest

aiortc = pytest.importorskip("aiortc")
av = pytest.importorskip("av")

from pmcore.camera import MainFrame  # noqa: E402
from pmcore.commands import CommandRouter  # noqa: E402
from pmcore.video import SharedH264Encoder  # noqa: E402
from pmcore.webrtc import WebRtcManager  # noqa: E402
from pmcore.zmq_bridge import ensure_cmd_id  # noqa: E402

W, H = 320, 240


class Local:
    def __init__(self):
        self.cam = "front"

    async def set_camera(self, cam):
        self.cam = cam

    async def set_display(self, m, g):
        pass

    async def set_volume(self, v):
        pass

    async def snapshot_jpeg(self):
        return b"\xff\xd8"

    async def record_clip(self, s):
        return {}

    def config_get(self):
        return {}

    async def config_set(self, c):
        return c


def feeder(enc, stop, local):
    t0 = time.monotonic()
    k = 0
    while not stop.is_set():
        k += 1
        y = np.full((H, W), 40 if local.cam == "front" else 200, np.uint8)
        y[:, (k * 4) % W] = 255
        uv = np.full((H // 2, W), 128, np.uint8)
        enc.push(MainFrame("front", time.monotonic(), np.vstack([y, uv]), W, H))
        time.sleep(1 / 25)


async def app_peer(label="ctl"):
    from aiortc import RTCPeerConnection

    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    pc.addTransceiver("audio", direction="recvonly")
    ch = pc.createDataChannel(label, ordered=True)
    frames = []
    msgs = asyncio.Queue()
    opened = asyncio.Event()

    @pc.on("track")
    def on_track(track):
        async def rd():
            try:
                while True:
                    frames.append(await track.recv())
            except Exception:  # noqa: BLE001
                pass
        if track.kind == "video":
            asyncio.ensure_future(rd())

    @ch.on("open")
    def _o():
        opened.set()

    @ch.on("message")
    def _m(m):
        msgs.put_nowait(json.loads(m))

    await pc.setLocalDescription(await pc.createOffer())  # waits for gathering = non-trickle
    return pc, ch, frames, msgs, opened


def test_loopback_video_and_datachannel():
    async def go():
        enc = SharedH264Encoder(W, H, 25, 600_000)
        enc.start()
        published = []
        local = Local()

        def pub(cmd):
            out = ensure_cmd_id(cmd)
            published.append(out)
            return out

        router = CommandRouter(pub, local)
        mgr = WebRtcManager(enc, router, ice_provider=lambda: asyncio.sleep(0, []), max_viewers=1)
        stop = threading.Event()
        th = threading.Thread(target=feeder, args=(enc, stop, local), daemon=True)
        th.start()
        try:
            pc, ch, frames, msgs, opened = await app_peer()
            answer = await mgr.on_offer("user:42:s1", pc.localDescription.sdp)
            assert "H264" in answer and "a=candidate" in answer
            from aiortc import RTCSessionDescription

            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
            await asyncio.wait_for(opened.wait(), 10)
            # decoded frames arrive
            for _ in range(100):
                if len(frames) >= 10:
                    break
                await asyncio.sleep(0.1)
            assert len(frames) >= 10, f"only {len(frames)} frames"
            assert (frames[-1].width, frames[-1].height) == (W, H)
            assert np.mean(frames[-1].to_ndarray(format="gray")) < 120  # dark = front

            # DC command → zmq cmd with id; ack routed back
            ch.send(json.dumps({"t": "feed", "grams": 12}))
            for _ in range(50):
                if published:
                    break
                await asyncio.sleep(0.05)
            assert published[0]["t"] == "feed" and published[0]["id"]
            await router.on_ros_ack({"id": published[0]["id"], "ok": True, "error": None})
            m = await asyncio.wait_for(msgs.get(), 5)
            assert m == {"t": "ack", "id": published[0]["id"], "ok": True, "error": None}

            # camera switch: no renegotiation, frames continue & become bright
            ch.send(json.dumps({"t": "camera", "id": "rear", "id_": 1}))
            m = await asyncio.wait_for(msgs.get(), 5)
            assert m["ok"] and m["camera"] == "rear"
            n0 = len(frames)
            for _ in range(60):
                if len(frames) > n0 + 10 and np.mean(frames[-1].to_ndarray(format="gray")) > 150:
                    break
                await asyncio.sleep(0.1)
            assert np.mean(frames[-1].to_ndarray(format="gray")) > 150

            # state broadcast
            assert mgr.broadcast({"t": "state", "ts": 1.0, "mode": "auto"}) == 1
            m = await asyncio.wait_for(msgs.get(), 5)
            assert m["t"] == "state"

            # viewer limit (max 1 here)
            pc2, *_ = await app_peer()
            assert await mgr.on_offer("user:42:s2", pc2.localDescription.sdp) is None
            await pc2.close()

            await mgr.on_bye("user:42:s1")
            assert mgr.viewers == 0 and not enc.active
            await pc.close()
        finally:
            stop.set()
            th.join(2)
            enc.stop()
            await mgr.close_all()

    asyncio.run(asyncio.wait_for(go(), 60))
