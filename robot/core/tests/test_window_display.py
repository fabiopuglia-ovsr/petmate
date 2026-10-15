import asyncio
import math
import time

import numpy as np
import pytest

from pmcore.ble.provisioning import Provisioner, ProvisioningWindow
from pmcore.display.state import DisplayState, FaceExpression, expression_for, gaze_from_bearing


class FakeServer:
    instances = []

    def __init__(self, prov, name):
        self.prov, self.name, self.running = prov, name, False
        FakeServer.instances.append(self)

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False


def test_window_unprovisioned_and_button():
    async def go():
        FakeServer.instances.clear()
        provisioned = {"v": False}
        w = ProvisioningWindow(lambda: Provisioner("PM-000123", None, None, None), "Petmate-0123",
                               lambda: provisioned["v"], window_s=0.05, server_factory=FakeServer)
        await w.start_if_needed()
        assert w.active and w.deadline is None  # unprovisioned: no timeout
        provisioned["v"] = True
        await w.close("done")
        assert not w.active
        await w.start_if_needed()
        assert not w.active  # provisioned → BLE stays off
        await w.on_button_provision()
        assert w.active and FakeServer.instances[-1].name == "Petmate-0123"
        k1 = w.prov.device_info()["pubkey"]
        await w.on_button_provision()  # re-press → fresh key
        assert w.prov.device_info()["pubkey"] != k1
        await asyncio.sleep(0.15)
        assert not w.active  # expired

    asyncio.run(go())


def test_expression_and_gaze():
    assert expression_for("sleeping", "auto", True, False) == FaceExpression.SLEEPY
    assert expression_for("idle", "auto", True, False) == FaceExpression.HAPPY
    assert expression_for("idle", "auto", False, True) == FaceExpression.ALERT
    assert expression_for("patrolling", "auto", False, False) == FaceExpression.IDLE
    assert gaze_from_bearing(None) == 0.0
    assert gaze_from_bearing(math.pi / 2) == pytest.approx(1.0)
    assert gaze_from_bearing(-0.3) < 0
    assert gaze_from_bearing(3.0) == 1.0 and gaze_from_bearing(-3.0) == -1.0


def test_display_state_game_timeout_and_owner():
    ds = DisplayState()
    ds.set_mode("game", "mouse", duration_s=0.01)
    assert ds.snapshot()["mode"] == "game"
    time.sleep(0.02)
    assert ds.snapshot()["mode"] == "face"
    with pytest.raises(ValueError):
        ds.set_mode("game", "snake")


def test_renderer_headless_smoke(monkeypatch):
    pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    from pmcore.display.renderer import Renderer

    ds = DisplayState()
    ds.streaming = True
    r = Renderer(ds, size=300, fps=60, driver="dummy")
    r.start()
    try:
        for mode, game in (("face", None), ("game", "birds"), ("game", "fish"), ("game", "mouse"), ("owner", None)):
            ds.set_mode(mode, game)
            if mode == "owner":
                ds.set_owner_frame(np.zeros((120, 160, 3), np.uint8))
            ds.pet_sighting(0.4)
            time.sleep(0.15)
            assert r._thread.is_alive()
        assert r.ok
    finally:
        r.stop()


def test_bless_long_read_offset_patch():
    pytest.importorskip("bless")
    from pmcore.ble.provisioning import _offset_slice, patch_bless_long_reads

    assert patch_bless_long_reads() is True
    assert patch_bless_long_reads() is True  # idempotent

    class V:
        def __init__(self, v):
            self.value = v

    assert _offset_slice(b"abcdef", {"offset": V(2)}) == b"cdef"
    assert _offset_slice(b"abcdef", {}) == b"abcdef"
