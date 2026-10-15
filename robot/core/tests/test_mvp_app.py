"""MVP profile end-to-end inside pm-core: App(profile=mvp, camera=lavfi) ↔ aiortc
peers using the two offer shapes seen in the field:

* Flutter app: audio sendrecv (mic track) + video recvonly + DataChannel ``ctl``;
* browser console: video recvonly + DataChannel only (Chrome-style ``.local``
  mDNS host candidates are tested separately).
"""

import asyncio
import json
import re
import uuid

import numpy as np
import pytest

aiortc = pytest.importorskip("aiortc")
av = pytest.importorskip("av")

from pmcore.app import App, placeholder_state  # noqa: E402
from pmcore.paths import Paths  # noqa: E402
from pmcore.settings import DEFAULTS, Settings  # noqa: E402


def mvp_settings(**extra):
    over = {"profile": "mvp",
            "camera": {"backend": "lavfi", "main_size": [320, 240], "fps": 30, "idle_close_s": 0.5},
            "state": {"local_rate_hz": 10.0}}
    for k, v in extra.items():
        over.setdefault(k, {}).update(v)
    return Settings.load(None, env={}, overrides=over)


# ------------------------------------------------------------------ settings
def test_profile_resolution(tmp_path):
    p = tmp_path / "core.json"
    p.write_text(json.dumps({"profile": "mvp", "camera": {"main_size": [640, 480]}}))
    s = Settings.load(p, env={})
    assert s.profile == "mvp"
    assert s["camera"]["backend"] == "uvc" and s["camera"]["device"] == "auto"
    assert s["camera"]["main_size"] == [640, 480]  # explicit core.json beats the profile
    assert s["camera"]["fps"] == 30
    for sec in ("detector", "display", "audio", "clips", "zmq"):
        assert s[sec]["enabled"] is False, sec
    assert s["cloud"]["allow_http"] is True
    # env still wins, and PMCORE_PROFILE selects the profile
    s2 = Settings.load(p, env={"PMCORE_CAMERA_BACKEND": "lavfi", "PMCORE_CLOUD_URL": "http://10.0.0.2:8000/v1"})
    assert s2["camera"]["backend"] == "lavfi" and s2["cloud"]["url"] == "http://10.0.0.2:8000/v1"
    s3 = Settings.load(None, env={"PMCORE_PROFILE": "mvp"})
    assert s3.profile == "mvp" and s3["zmq"]["enabled"] is False
    # product defaults untouched
    s4 = Settings.load(None, env={})
    assert s4.profile == "product" and s4["camera"]["backend"] == "picamera2" and s4["zmq"]["enabled"]
    assert DEFAULTS["zmq"]["enabled"] is True


def test_placeholder_state_shape_matches_interfaces_4_3():
    st = placeholder_state()
    assert st["t"] == "state" and isinstance(st["ts"], float)
    assert set(st) == {"t", "ts", "battery", "pose", "mode", "activity", "feeder", "launcher", "safety",
                       "nav", "pet", "pois", "fw"}
    assert set(st["battery"]) == {"soc", "voltage", "current", "charging", "docked"}
    assert st["battery"]["soc"] is None
    assert st["mode"] in ("auto", "manual", "sleep") and st["activity"] == "idle"
    assert st["nav"] == {"status": "idle", "goal": None, "mapping": False}
    assert set(st["safety"]) == {"estop", "cliff_mask", "bump_mask", "wheel_drop", "motor_fault"}
    json.dumps(st)


# ------------------------------------------------------------------ peers
async def make_peer(shape: str):
    from aiortc import RTCPeerConnection
    from aiortc.mediastreams import AudioStreamTrack

    pc = RTCPeerConnection()
    if shape == "flutter":
        pc.addTransceiver(AudioStreamTrack(), direction="sendrecv")  # mic → robot
        pc.addTransceiver("video", direction="recvonly")
    else:
        pc.addTransceiver("video", direction="recvonly")
    ch = pc.createDataChannel("ctl", ordered=True)
    frames, msgs, opened = [], asyncio.Queue(), asyncio.Event()

    @pc.on("track")
    def on_track(track):
        if track.kind != "video":
            return

        async def rd():
            try:
                while True:
                    frames.append(await track.recv())
            except Exception:  # noqa: BLE001
                pass
        asyncio.ensure_future(rd())

    ch.on("open", opened.set)
    ch.on("message", lambda m: msgs.put_nowait(json.loads(m)))
    await pc.setLocalDescription(await pc.createOffer())
    return pc, ch, frames, msgs, opened


def m_sections(sdp: str) -> dict:
    out, cur = {}, None
    for line in sdp.splitlines():
        if line.startswith("m="):
            cur = line[2:].split(" ", 1)[0]
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return out


async def next_msg(msgs, pred, timeout=5.0):
    async def go():
        while True:
            m = await msgs.get()
            if pred(m):
                return m
    return await asyncio.wait_for(go(), timeout)


@pytest.mark.parametrize("shape", ["flutter", "browser"])
def test_mvp_app_streams_lavfi_to_both_offer_shapes(tmp_path, shape):
    async def go():
        app = App(Paths(tmp_path), mvp_settings(), enable_hw=True)
        await app.start()
        try:
            # MVP profile: no ROS/ZMQ, detector, display, audio, clips
            assert app.zmq is None and app.detector is None and app.renderer is None
            assert app.mic is None and app.speaker is None and app.clips is None
            assert app.cameras.backend == "lavfi" and app.api is None and app.ble is None

            pc, ch, frames, msgs, opened = await make_peer(shape)
            answer = await app.webrtc.on_offer("user:1:" + shape, pc.localDescription.sdp)
            sec = m_sections(answer)
            assert "a=sendonly" in sec["video"] and any("H264" in x for x in sec["video"])
            if shape == "flutter":
                assert "a=inactive" in sec["audio"]  # no mic/speaker in the MVP
            else:
                assert "audio" not in sec
            assert "application" in sec
            from aiortc import RTCSessionDescription

            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
            await asyncio.wait_for(opened.wait(), 10)
            for _ in range(150):
                if len(frames) >= 10:
                    break
                await asyncio.sleep(0.1)
            assert len(frames) >= 10, f"only {len(frames)} frames decoded"
            f = frames[-1]
            assert (f.width, f.height) == (320, 240)
            assert float(np.std(f.to_ndarray(format="gray"))) > 5  # testsrc2 pattern, not a flat frame

            # placeholder state at local_rate_hz, §4.3 shape
            st = await next_msg(msgs, lambda m: m.get("t") == "state")
            assert st["battery"]["soc"] is None and st["pet"]["camera"] == "front"

            # rear camera does not exist in the MVP
            ch.send(json.dumps({"t": "camera", "id": "rear"}))
            ack = await next_msg(msgs, lambda m: m.get("t") == "ack")
            assert ack == {"t": "ack", "id": "rear", "ok": False, "error": "camera_not_available", "camera": "rear"}
            ch.send(json.dumps({"t": "camera", "id": "front"}))
            ack = await next_msg(msgs, lambda m: m.get("t") == "ack")
            assert ack["ok"] is True and ack["camera"] == "front"

            # ROS commands are answered immediately, not left pending
            ch.send(json.dumps({"t": "feed", "grams": 10, "id": "c1"}))
            ack = await next_msg(msgs, lambda m: m.get("t") == "ack")
            assert ack == {"t": "ack", "id": "c1", "ok": False, "error": "not_supported"}
            ch.send(json.dumps({"t": "record_clip", "seconds": 5, "id": "c2"}))
            ack = await next_msg(msgs, lambda m: m.get("t") == "ack")
            assert ack["id"] == "c2" and ack["error"] == "not_supported"

            # snapshot works from the UVC/lavfi path while streaming
            ch.send(json.dumps({"t": "snapshot"}))
            snap = await next_msg(msgs, lambda m: m.get("t") == "snapshot", timeout=10)
            import base64

            assert base64.b64decode(snap["jpeg_b64"])[:2] == b"\xff\xd8"

            # volume still persists config (no amixer call in MVP)
            ch.send(json.dumps({"t": "volume", "speaker": 20, "id": "v"}))
            ack = await next_msg(msgs, lambda m: m.get("t") == "ack")
            assert ack["ok"] and app.config.get("audio.speaker_volume") == 20

            await app.webrtc.on_bye("user:1:" + shape)
            assert app.webrtc.viewers == 0 and not app.encoder.active
            await pc.close()
        finally:
            await app.shutdown()

    asyncio.run(asyncio.wait_for(go(), 90))


def test_mdns_host_candidates_are_resolved(tmp_path):
    """Chrome hides host IPs behind ``<uuid>.local`` candidates; aioice (aiortc's
    ICE) resolves them with its own mDNS client (no avahi needed)."""
    from aioice import mdns

    async def go():
        app = App(Paths(tmp_path), mvp_settings(), enable_hw=True)
        await app.start()
        responder = await mdns.create_mdns_protocol()
        try:
            pc, ch, frames, msgs, opened = await make_peer("browser")
            sdp = pc.localDescription.sdp
            ips = sorted(set(re.findall(r"a=candidate:\S+ \d+ udp \d+ (\d+\.\d+\.\d+\.\d+) \d+ typ host", sdp)))
            assert ips
            names = {}
            for ip in ips:
                names[ip] = f"{uuid.uuid4()}.local"
                await responder.publish(names[ip], ip)
                sdp = re.sub(rf"(a=candidate:\S+ \d+ udp \d+ ){re.escape(ip)}( \d+ typ host)",
                             rf"\g<1>{names[ip]}\g<2>", sdp)
            # Only host candidates are obfuscated (as Chrome does). With internet access
            # aiortc also gathers srflx candidates whose "raddr" still carries the
            # private IP (e.g. on CI runners) — Chrome sends raddr 0.0.0.0 there.
            host_lines = [l for l in sdp.splitlines() if l.startswith("a=candidate:") and " typ host" in l]
            assert ".local" in sdp and not any(f" {ip} " in l for l in host_lines for ip in ips)
            answer = await app.webrtc.on_offer("user:1:mdns", sdp)
            sess = app.webrtc.sessions["user:1:mdns"]
            ice = sess.pc.getTransceivers()[0].receiver.transport.transport
            resolved = {c.ip for c in ice.getRemoteCandidates()}
            assert set(ips) <= resolved, (ips, resolved)
            from aiortc import RTCSessionDescription

            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
            await asyncio.wait_for(opened.wait(), 10)
            for _ in range(100):
                if frames:
                    break
                await asyncio.sleep(0.1)
            assert frames
            await pc.close()
        finally:
            await responder.close()
            await app.shutdown()

    asyncio.run(asyncio.wait_for(go(), 60))


def test_doctor_with_lavfi_camera(tmp_path):
    from pmcore.doctor import FAIL, PASS, Doctor

    (tmp_path / "etc/petmate").mkdir(parents=True)
    (tmp_path / "etc/petmate/core.json").write_text(json.dumps(
        {"profile": "mvp", "camera": {"backend": "lavfi", "main_size": [320, 240]}}))
    (tmp_path / "etc/petmate/identity.json").write_text(json.dumps({"device_id": "PM-ABC123", "device_secret": "s" * 43}))
    os_ = __import__("os")
    os_.chmod(tmp_path / "etc/petmate/identity.json", 0o600)
    lines = []
    d = Doctor(str(tmp_path), seconds=1.0, skip_system=True, out=lines.append)
    d.run()
    by = {r.name: r for r in d.results}
    assert by["Identità dispositivo"].status == PASS and "PM-ABC123" in by["Identità dispositivo"].detail
    assert by["Configurazione core.json"].status == PASS
    assert by["Camera rilevata"].status == PASS
    assert by["Cattura 1 s"].status == PASS and "320x240" in by["Cattura 1 s"].detail
    assert by["Encoder x264"].status in (PASS, "WARN") and "fps" in by["Encoder x264"].detail
    assert by["Cloud raggiungibile"].status == "SKIP"
    assert not [r for r in d.results if r.status == FAIL and r.name != "Dipendenze Python"]
    assert lines[-1].strip().startswith("Risultato:")
