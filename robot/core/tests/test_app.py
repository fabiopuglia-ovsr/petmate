"""App wiring smoke test (no hardware, no cloud identity) + ROS event handling."""

import asyncio
import json

from pmcore.app import App, dc_event
from pmcore.paths import Paths
from pmcore.settings import Settings


def free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_app_starts_routes_events_and_stops(tmp_path):
    async def go():
        st = Settings.load(None, env={})
        st["zmq"]["pub"] = f"tcp://127.0.0.1:{free_port()}"
        st["zmq"]["sub"] = f"tcp://127.0.0.1:{free_port()}"
        st["audio"]["mixer_card"] = "none"
        app = App(Paths(tmp_path), st, enable_hw=False)
        await app.start()
        try:
            assert (tmp_path / "var/lib/petmate/config.json").exists()
            assert app.api is None  # no identity → cloud disabled, but core runs

            sent = []
            app.webrtc.broadcast = lambda m: sent.append(m) or 0

            await app._on_ros_event("event", {"t": "display_game", "game": "fish"})
            assert app.display_state.mode == "game" and app.display_state.game == "fish"
            await app._on_ros_event("event", {"t": "display_game", "game": None})
            assert app.display_state.mode == "face"
            await app._on_ros_event("event", {"t": "meal_dispensed", "ts": 5.0, "grams": 25})
            assert sent == [{"t": "event", "event": {"t": "meal_dispensed", "ts": 5.0, "grams": 25}}]
            await app._on_ros_event("event", {"t": "button_provision", "ts": 1.0})  # no BLE → no crash
            assert len(sent) == 1

            app._on_ros_state("state", {"ts": 1.0, "activity": "sleeping", "mode": "sleep",
                                        "safety": {"estop": False}})
            assert sent[-1]["t"] == "state"
            assert app.display_state.snapshot()["expression"].value == "sleepy"

            # local command via router → config persisted + broadcast
            replies = []
            await app.router.handle({"t": "volume", "speaker": 33}, replies.append)
            assert replies[0]["ok"]
            on_disk = json.loads((tmp_path / "var/lib/petmate/config.json").read_text())
            assert on_disk["audio"]["speaker_volume"] == 33
            assert sent[-1] == {"t": "config", "config": on_disk}

            # privacy: disabling rear streaming forces front
            app.cameras.select("rear")
            app.config.apply({"privacy": {"rear_camera_streaming": False}})
            assert app.cameras.selected == "front"
        finally:
            await app.shutdown()

    asyncio.run(asyncio.wait_for(go(), 20))


def test_dc_event_shape():
    assert dc_event({"t": "docked", "ts": 1}) == {"t": "event", "event": {"t": "docked", "ts": 1}}
