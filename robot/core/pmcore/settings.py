"""Host-level pm-core settings (NOT the §5 device config).

Optional file ``/etc/petmate/core.json`` overrides any default below; env
``PMCORE_<SECTION>_<KEY>`` overrides too (e.g. ``PMCORE_AUDIO_CAPTURE_DEVICE``,
``PMCORE_CLOUD_URL``). These are installation/hardware knobs (ALSA device
names, ports, sizes) that the app never touches.

Profiles
--------
``core.json`` may contain a top-level ``"profile"`` (or env ``PMCORE_PROFILE``).
Resolution order: ``DEFAULTS`` → ``PROFILES[profile]`` → rest of ``core.json``
→ env. So a profile only changes defaults: anything written explicitly in
core.json (e.g. ``camera.main_size``) still wins.

* ``product`` (default): full robot (picamera2 ×2, Hailo, display, audio, ROS).
* ``mvp`` (docs/MVP.md): Raspberry Pi 5 + Logitech USB webcam only — detector,
  display, audio, clips and the ZMQ/ROS bridge are disabled, camera backend
  ``uvc`` with ``device: "auto"``, pm-core publishes a placeholder §4.3 state
  itself, the provisioning payload may carry an ``http://`` cloud_url (LAN
  developer cloud).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Optional

from .config import deep_merge
from .log import get_logger, kv

log = get_logger(__name__)

DEFAULTS: dict[str, Any] = {
    "zmq": {
        "enabled": True,                  # False: no ROS bridge (no bind, no state wait)
        "pub": "tcp://127.0.0.1:5560",    # §3 core→ros (bind)
        "sub": "tcp://127.0.0.1:5561",    # §3 ros→core (connect)
    },
    "camera": {
        # "picamera2" (product, synthetic fallback on dev machines), "uvc" (USB webcam via
        # PyAV/V4L2), "lavfi" (FFmpeg test source through the uvc code path), "synthetic".
        "backend": "picamera2",
        "device": "auto",                 # uvc: "auto" | "/dev/videoN" | "/dev/v4l/by-id/..."
        "input_format": "mjpeg",          # uvc: preferred V4L2 input format (first attempt)
        "reconnect_s": 2.0,               # uvc: retry interval when the camera is missing/unplugged
        "idle_close_s": 5.0,              # uvc: close the device this long after the last consumer left
        "lavfi_source": "testsrc2=size={w}x{h}:rate={fps},format=yuvj422p",  # = MJPEG decoder output
        "front_index": 0,
        "rear_index": 1,
        "main_size": [1280, 720],
        "lores_size": [640, 640],
        "fps": 25,
        "hfov_deg": 102.0,  # Camera Module 3 Wide
        "front_bearing_offset_rad": 0.0,
        "hflip": False,
        "vflip": False,
    },
    "video": {
        "bitrate": 1_500_000,  # §4.1 1.5 Mb/s
        "jpeg_quality": 85,
        "x264_threads": 3,
    },
    "detector": {
        "enabled": True,
        "hef": "/usr/share/hailo-models/yolov8s_h8l.hef",
        "rate_hz_per_camera": 10.0,
        "conf_threshold": 0.4,
        "iou_match": 0.3,
        "track_max_age_s": 1.5,
        "pet_event_min_interval_s": 300.0,
        "pet_event_min_conf": 0.55,
    },
    "clips": {
        "enabled": True,
        "default_seconds": 15,
        "max_seconds": 60,
        "pre_roll_s": 5,
        "keep_local": 20,
        "auto_on_pet_detected": False,
    },
    "display": {
        "enabled": True,
        "size": 1080,
        "fps": 30,
        "driver": "kmsdrm",
    },
    "audio": {
        "enabled": True,
        "capture_device": "plughw:CARD=Array,DEV=0",
        "playback_device": "plughw:CARD=Array,DEV=0",
        "mixer_card": "Array",
        "mixer_control": "PCM",
        "sample_rate": 48000,
        "channels": 1,
    },
    "state": {
        # Only used when zmq.enabled is False: pm-core publishes a §4.3 state itself.
        "local_rate_hz": 2.0,
    },
    "webrtc": {
        "max_viewers": 2,
        "ice_refresh_s": 3000,
    },
    "cloud": {
        "url": "",                        # non-empty: overrides cloud.json/BLE cloud_url (dev/E2E)
        "allow_http": False,              # accept http:// cloud_url in the BLE payload
        "ping_interval_s": 20,
        "pong_timeout_s": 15,
        "http_timeout_s": 20,
    },
    "ble": {
        "enabled": True,
        "transport": "bless",             # "bless" (BlueZ) | "loopback" (in-process, tests/E2E)
        "wifi": "nmcli",                  # "nmcli" | "fake" (tests/E2E: no Wi-Fi change)
        "window_s": 600,  # §7: 10 min after 5 s button press
        "adapter": "hci0",
        "nm_iface": "wlan0",
        "wifi_timeout_s": 45,
    },
    "log": {"level": "INFO", "format": "json"},
}

PROFILES: dict[str, dict[str, Any]] = {
    "product": {},
    "mvp": {
        "zmq": {"enabled": False},
        "camera": {"backend": "uvc", "device": "auto", "main_size": [1280, 720], "fps": 30},
        "detector": {"enabled": False},
        "clips": {"enabled": False},
        "display": {"enabled": False},
        "audio": {"enabled": False},
        "cloud": {"allow_http": True},
    },
}


class Settings:
    def __init__(self, data: dict[str, Any], profile: str = "product"):
        self._d = data
        self.profile = profile

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self._d[section]

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self._d.get(section, {}).get(key, default)

    @property
    def data(self) -> dict[str, Any]:
        out = copy.deepcopy(self._d)
        out["profile"] = self.profile
        return out

    @classmethod
    def load(cls, path: Path | None = None, env: dict[str, str] | None = None,
             overrides: Optional[dict[str, Any]] = None) -> "Settings":
        """Load settings. ``overrides`` (tests) is applied like the core.json file."""
        env = os.environ if env is None else env
        user: dict[str, Any] = {}
        if path is not None and Path(path).exists():
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("core.json must contain an object")
                user = loaded
            except (OSError, ValueError) as e:
                log.error("core settings unreadable, using defaults", extra=kv(path=str(path), error=str(e)))
        if overrides:
            user = deep_merge(user, overrides)
        user = dict(user)
        profile = str(env.get("PMCORE_PROFILE") or user.pop("profile", None) or "product")
        user.pop("profile", None)
        if profile not in PROFILES:
            log.error("unknown profile, using product", extra=kv(profile=profile))
            profile = "product"
        d = deep_merge(copy.deepcopy(DEFAULTS), PROFILES[profile])
        d = deep_merge(d, {k: v for k, v in user.items() if isinstance(v, dict)})
        for section, vals in d.items():
            if not isinstance(vals, dict):
                continue
            for key, cur in list(vals.items()):
                ev = env.get(f"PMCORE_{section}_{key}".upper())
                if ev is None:
                    continue
                try:
                    if isinstance(cur, bool):
                        vals[key] = ev.lower() in ("1", "true", "yes", "on")
                    elif isinstance(cur, int):
                        vals[key] = int(ev)
                    elif isinstance(cur, float):
                        vals[key] = float(ev)
                    elif isinstance(cur, list):
                        vals[key] = json.loads(ev)
                    else:
                        vals[key] = ev
                except ValueError:
                    log.error("bad env override", extra=kv(var=f"PMCORE_{section}_{key}".upper()))
        return cls(d, profile)
