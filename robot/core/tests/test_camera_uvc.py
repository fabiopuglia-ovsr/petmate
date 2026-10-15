"""Camera backends for the MVP: ``lavfi`` (same code path as ``uvc``), device
auto-detection, V4L2 mode fallbacks, on-demand open/idle close, reconnect."""

import os
import threading
import time

import numpy as np
import pytest

av = pytest.importorskip("av")

from pmcore.camera import (  # noqa: E402
    AvCaptureCam,
    CameraManager,
    CameraNotAvailable,
    MainFrame,
    find_uvc_device,
    uvc_modes,
)
from pmcore.settings import Settings  # noqa: E402


def cam_settings(**over):
    s = Settings.load(None, env={}, overrides={"profile": "mvp"})["camera"]
    s = dict(s)
    s.update({"backend": "lavfi", "main_size": [320, 240], "fps": 30, "reconnect_s": 0.2, "idle_close_s": 0.3})
    s.update(over)
    return s


def wait_for(pred, timeout=10.0, step=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


def test_lavfi_backend_produces_frames_on_demand_and_closes_when_idle():
    mgr = CameraManager(cam_settings(), lores_rate_hz=0.0)
    assert mgr.available == ("front",)
    got: list[MainFrame] = []
    mgr.main_sinks.append(got.append)
    mgr.start()
    try:
        cam = mgr.cams["front"]
        time.sleep(0.4)
        assert cam.opens == 0 and not cam.ok  # nobody needs frames → device stays closed
        mgr.set_encoder_active(True)
        assert wait_for(lambda: len(got) >= 15), f"only {len(got)} frames"
        f = got[-1]
        assert (f.width, f.height) == (320, 240) and f.yuv420 is None
        assert f.vframe.format.name == "yuvj422p"  # like a Logitech MJPEG stream
        assert f.i420().shape == (360, 320)
        ts = [g.t for g in got]
        assert all(b > a for a, b in zip(ts, ts[1:]))  # monotonic capture times
        # paced to ~real time (lavfi would otherwise run as fast as possible)
        n0, t0 = len(got), time.monotonic()
        time.sleep(1.0)
        rate = (len(got) - n0) / (time.monotonic() - t0)
        assert 15 < rate < 45, rate
        st = mgr.status()["front"]
        assert st["ok"] and st["size"] == [320, 240] and st["backend"] == "lavfi"
        mgr.set_encoder_active(False)
        assert wait_for(lambda: not cam.ok, 5), "camera not closed when idle"
        assert cam.failures == 0
    finally:
        mgr.stop()


def test_snapshot_from_lavfi_and_jpeg():
    import asyncio

    from pmcore.clips import snapshot

    mgr = CameraManager(cam_settings(), lores_rate_hz=0.0)
    mgr.start()
    try:
        jpeg = asyncio.run(snapshot(mgr, "front", 80, timeout_s=10))
        assert jpeg[:2] == b"\xff\xd8" and len(jpeg) > 1000
        img = av.open(__import__("io").BytesIO(jpeg))
        fr = next(img.decode(video=0))
        assert (fr.width, fr.height) == (320, 240)
    finally:
        mgr.stop()


def test_only_front_camera_exists_for_uvc_backends():
    mgr = CameraManager(cam_settings(backend="uvc"), lores_rate_hz=0.0)
    mgr.select("front")
    with pytest.raises(CameraNotAvailable):
        mgr.select("rear")
    assert mgr.selected == "front"
    # product backend keeps both
    prod = CameraManager(Settings.load(None, env={})["camera"], lores_rate_hz=0.0)
    prod.select("rear")


def test_lores_from_av_frames_when_detector_enabled():
    mgr = CameraManager(cam_settings(lores_size=[64, 64]), lores_rate_hz=10.0)
    lores = []
    mgr.lores_sinks.append(lores.append)
    mgr.start()  # lores sinks alone create demand
    try:
        assert wait_for(lambda: len(lores) >= 3)
        assert lores[0].rgb.shape == (64, 64, 3) and lores[0].rgb.dtype == np.uint8
    finally:
        mgr.stop()


def test_reconnect_after_open_failures():
    """Camera missing/unplugged → retry every reconnect_s, then frames flow."""
    mgr = CameraManager(cam_settings(), lores_rate_hz=0.0)
    got = []
    mgr.main_sinks.append(got.append)
    mgr.start()
    cam: AvCaptureCam = mgr.cams["front"]
    real_open = cam.open_source
    fails = {"n": 0}

    def flaky(src):
        if fails["n"] < 2:
            fails["n"] += 1
            raise OSError("No such device")
        return real_open(src)

    cam.open_source = flaky
    try:
        mgr.set_encoder_active(True)
        assert wait_for(lambda: len(got) >= 5, 10)
        assert fails["n"] == 2 and cam.failures == 2 and cam.opens == 1

        # mid-stream loss: the source ends (like ENODEV on unplug) → reopen
        class Dying:
            def __init__(self, c):
                self.c = c

            def demux(self, st):
                for i, p in enumerate(self.c.demux(st)):
                    if i == 5:
                        raise OSError(19, "No such device")
                    yield p

            def close(self):
                self.c.close()

        def dying_open(src):
            c, st, mode = real_open(src)
            return Dying(c), st, mode

        cam.open_source = dying_open
        mgr.set_encoder_active(False)
        assert wait_for(lambda: not cam.ok, 5)
        mgr.set_encoder_active(True)
        assert wait_for(lambda: cam.opens >= 3, 10), cam.status if hasattr(cam, "status") else cam.opens
        assert "No such device" in (cam.last_error or "")
    finally:
        mgr.stop()


def test_missing_device_keeps_retrying_without_crash(tmp_path):
    s = cam_settings(backend="uvc", device=str(tmp_path / "video9"))
    mgr = CameraManager(s, lores_rate_hz=0.0)
    mgr.start()
    try:
        mgr.set_encoder_active(True)
        time.sleep(0.6)
        cam = mgr.cams["front"]
        assert not cam.ok and cam.last_error == "not_found" and cam._thread.is_alive()
    finally:
        mgr.stop()


def test_uvc_mode_fallbacks():
    m = uvc_modes((1280, 720), 30)
    assert m[0] == {"input_format": "mjpeg", "video_size": "1280x720", "framerate": "30"}
    assert m[1] == {"input_format": "mjpeg", "video_size": "640x480", "framerate": "30"}
    assert m[2] == {"input_format": "yuyv422", "video_size": "640x480", "framerate": "30"}
    assert m[3] == {}
    assert len(uvc_modes((640, 480), 30)) == 3  # dedup


def _mk_node(sysfs, dev, n, driver, index=0):
    node = sysfs / f"video{n}"
    drv = sysfs.parent / "drivers" / driver
    drv.mkdir(parents=True, exist_ok=True)
    (node / "device").mkdir(parents=True)
    os.symlink(drv, node / "device" / "driver")
    (node / "index").write_text(f"{index}\n")
    (dev / f"video{n}").write_text("")


def test_find_uvc_device(tmp_path):
    sysfs, dev, byid = tmp_path / "sys", tmp_path / "dev", tmp_path / "by-id"
    for d in (sysfs, dev, byid):
        d.mkdir()
    # Pi 5 ISP/codec nodes (not uvc) + a UVC camera with capture (video2) and metadata (video3)
    _mk_node(sysfs, dev, 19, "rpivid")
    _mk_node(sysfs, dev, 2, "uvcvideo", 0)
    _mk_node(sysfs, dev, 3, "uvcvideo", 1)
    caps = {str(dev / "video2"): {"capture": True}, str(dev / "video3"): {"capture": False}}
    q = caps.get
    args = dict(by_id_dir=str(byid), sysfs_dir=str(sysfs), dev_dir=str(dev))
    assert find_uvc_device(querycap=q, **args) == str(dev / "video2")
    # metadata node first in order → still skipped thanks to QUERYCAP
    caps2 = {str(dev / "video2"): {"capture": False}, str(dev / "video3"): {"capture": True}}
    assert find_uvc_device(querycap=caps2.get, **args) == str(dev / "video3")
    # no ioctl possible → sysfs index 0
    assert find_uvc_device(querycap=lambda d: None, **args) == str(dev / "video2")
    # by-id Logitech link wins (name or vendor id 046d)
    os.symlink(dev / "video3", byid / "usb-046d_HD_Pro_Webcam_C920_ABC-video-index0")
    assert find_uvc_device(querycap=q, **args) == str(byid / "usb-046d_HD_Pro_Webcam_C920_ABC-video-index0")
    os.symlink(dev / "video3", byid / "usb-Generic_Cam_1-video-index0")
    assert "046d" in find_uvc_device(querycap=q, **args)
    # nothing
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_uvc_device(by_id_dir=str(empty), sysfs_dir=str(empty), dev_dir=str(empty)) is None


def test_encoder_consumes_av_frames_and_follows_source_size():
    """MJPEG-like yuvj422p frames → libx264 without ndarray round-trip; size change → new SPS."""
    from pmcore.video import SharedH264Encoder

    enc = SharedH264Encoder(320, 240, 30, 300_000, threads=2)
    pkts = []
    ev = threading.Event()
    enc.add_sink(lambda p: (pkts.append((p.is_keyframe, enc.width, enc.height)), ev.set()))
    enc.start()
    try:
        def frame(w, h, k):
            arr = np.full((h, w, 3), k % 255, np.uint8)
            vf = av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuvj422p")
            vf.pict_type = av.video.frame.PictureType.I  # decoder output is all-intra
            return MainFrame("front", time.monotonic(), None, w, h, vf)

        for k in range(20):
            enc.push(frame(320, 240, k))
            time.sleep(0.01)
        assert wait_for(lambda: len(pkts) >= 10)
        # decoder pict_type I must NOT force an IDR every frame
        assert sum(1 for kf, *_ in pkts if kf) <= 2
        for k in range(10):
            enc.push(frame(640, 480, k))
            time.sleep(0.01)
        assert wait_for(lambda: any(w == 640 for _, w, _ in pkts))
        first_big = next(i for i, (_, w, _) in enumerate(pkts) if w == 640)
        assert pkts[first_big][0]  # resolution change starts with a keyframe
    finally:
        enc.stop()
