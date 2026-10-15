"""pm-core orchestrator: wires config, cloud, ZMQ, cameras, detector, WebRTC,
display, audio and BLE provisioning together, and implements the
DataChannel-local command handlers (:class:`pmcore.commands.LocalHandlers`).

Each subsystem can be disabled from the host settings; the ``mvp`` profile
(docs/MVP.md) runs only cameras(uvc) + encoder + WebRTC + cloud + BLE, and
publishes a placeholder §4.3 state itself since there is no ROS stack.
"""

from __future__ import annotations

import asyncio
import signal
import time
from typing import Any, Optional

from . import __version__
from .audio import MicSource, SpeakerMixer, set_volume
from .camera import CameraNotAvailable
from .commands import CommandError, CommandRouter
from .config import ConfigError, ConfigStore
from .identity import CloudState, Identity, IdentityError
from .log import get_logger, kv
from .paths import Paths
from .settings import Settings
from .zmq_bridge import ZmqBridge

log = get_logger(__name__)

# ROS→core events consumed by pm-core and not forwarded to app/cloud.
LOCAL_EVENTS = {"display_game", "button_provision"}
# display_game "game" values accepted (ROS may send the §4.2 play name too).
DISPLAY_GAME_ALIASES = {"birds": "birds", "fish": "fish", "mouse": "mouse", "screen_birds": "birds"}
# ack error for ROS commands when the ROS stack is disabled (MVP profile)
ERR_NOT_SUPPORTED = "not_supported"
ERR_CAMERA_NOT_AVAILABLE = "camera_not_available"


def placeholder_state(fw: str = __version__, camera: Optional[str] = None) -> dict:
    """§4.3-shaped state when no ROS stack publishes one (MVP profile).

    Unknown quantities are ``null`` (§9.11 style): battery soc/voltage/current,
    hopper, pet sighting. Shape and enums match §4.3 so the app can parse it.
    """
    return {
        "t": "state", "ts": time.time(),
        "battery": {"soc": None, "voltage": None, "current": None, "charging": False, "docked": False},
        "pose": {"x": 0.0, "y": 0.0, "th": 0.0, "localized": False},
        "mode": "manual", "activity": "idle",
        "feeder": {"state": "idle", "hopper_g": None, "today_g": 0.0, "last_meal": None},
        "launcher": {"state": "idle", "balls": 0},
        "safety": {"estop": False, "cliff_mask": 0, "bump_mask": 0, "wheel_drop": False, "motor_fault": False},
        "nav": {"status": "idle", "goal": None, "mapping": False},
        "pet": {"last_seen_ts": None, "camera": camera, "bearing_rad": None},
        "pois": [],
        "fw": fw,
    }


def dc_event(ev: dict) -> dict:
    """ZMQ ``event`` payload (§4.4, ``t`` = event type) → DataChannel message.

    §4.4 says events go to the DataChannel as ``{"t":"event",...}`` while the
    ZMQ payload already uses ``t`` for the event type; to avoid the key clash
    the original event is nested: ``{"t":"event","event":{"t":"meal_dispensed",...}}``.
    """
    return {"t": "event", "event": ev}


class App:
    def __init__(self, paths: Optional[Paths] = None, settings: Optional[Settings] = None,
                 enable_hw: bool = True):
        self.paths = paths or Paths.from_env()
        self.settings = settings or Settings.load(self.paths.host_settings)
        self.enable_hw = enable_hw
        self.config = ConfigStore(self.paths.config)
        self.identity: Optional[Identity] = None
        self.cloud_state = CloudState.load(self.paths.cloud_state)
        url_override = str(self.settings["cloud"].get("url") or "").strip()
        if url_override:  # PMCORE_CLOUD_URL / core.json cloud.url (dev, E2E)
            self.cloud_state.cloud_url = url_override.rstrip("/")
        self.api: Any = None
        self.signaling: Any = None
        self.zmq: Optional[ZmqBridge] = None
        if self.settings["zmq"].get("enabled", True):
            self.zmq = ZmqBridge(self.settings["zmq"]["pub"], self.settings["zmq"]["sub"],
                                 config_provider=lambda: self.config.data)
        self.router = CommandRouter(
            publish_cmd=self.zmq.publish_cmd if self.zmq is not None else self._no_ros,
            local=self,
            portion_max_g=lambda: self.config.get("feeding.portion_max_g"),
            rear_streaming_allowed=lambda: bool(self.config.get("privacy.rear_camera_streaming", True)),
        )
        self.cameras: Any = None
        self.encoder: Any = None
        self.detector: Any = None
        self.clips: Any = None
        self.webrtc: Any = None
        self.display_state: Any = None
        self.renderer: Any = None
        self.speaker: Optional[SpeakerMixer] = None
        self.mic: Optional[MicSource] = None
        self.ble: Any = None
        self.fake_wifi: Any = None
        self._stop = asyncio.Event()
        self._cloud_started = False
        self._tasks: list[asyncio.Task] = []
        self._last_owner_push = 0.0

    # =============================================================== startup
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # pragma: no cover
                pass
        await self.start()
        log.info("pm-core running", extra=kv(version=__version__))
        await self._stop.wait()
        log.info("shutdown requested")
        await self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()

    async def start(self) -> None:
        cfg = self.config.load()
        self.config.on_change(self._on_config_changed)
        try:
            self.identity = Identity.load(self.paths.identity)
        except IdentityError as e:
            log.error("no device identity: cloud and BLE provisioning disabled", extra=kv(error=str(e)))

        log.info("pm-core starting", extra=kv(
            profile=self.settings.profile, camera_backend=self.settings["camera"].get("backend"),
            ros=self.zmq is not None, cloud_url=self.cloud_state.cloud_url))
        if self.zmq is not None:
            await self.zmq.start()
            self.zmq.on("state", self._on_ros_state)
            self.zmq.on("event", self._on_ros_event)
            self.zmq.on("ack", self._on_ros_ack)

        await self._start_media(cfg)
        if self.zmq is None:
            self._tasks.append(asyncio.ensure_future(self._local_state_loop()))
        if self.settings["audio"].get("enabled", True):
            await set_volume(self.settings["audio"]["mixer_card"], self.settings["audio"]["mixer_control"],
                             cfg["audio"]["speaker_volume"])

        if self.identity is not None:
            from .cloud.api import CloudApi

            self.api = CloudApi(self.identity, self.cloud_state, self.paths.cloud_state,
                                timeout_s=float(self.settings["cloud"]["http_timeout_s"]))
            await self.api.start()
            if self.settings["ble"].get("enabled", True):
                await self._start_ble()
            if self.cloud_state.provisioned:
                await self._start_cloud()

        self._tasks.append(asyncio.ensure_future(self._health_loop()))

    async def _start_media(self, cfg: dict) -> None:
        from .camera import CameraManager
        from .display.state import DisplayState
        from .video import SharedH264Encoder

        st = self.settings
        s_cam, s_det = st["camera"], st["detector"]
        loop = asyncio.get_running_loop()
        w, h = s_cam["main_size"]
        det_enabled = bool(s_det.get("enabled", True))
        self.encoder = SharedH264Encoder(w, h, int(s_cam["fps"]), int(st["video"]["bitrate"]),
                                         threads=int(st["video"].get("x264_threads", 3)))
        self.cameras = CameraManager(s_cam, lores_rate_hz=float(s_det["rate_hz_per_camera"]) if det_enabled else 0.0)
        self.encoder.on_active_change = self.cameras.set_encoder_active
        self.cameras.main_sinks.append(self.encoder.push)
        if st["clips"].get("enabled", True):
            from .clips import ClipRecorder

            self.clips = ClipRecorder(
                self.encoder, self.paths.clips_dir,
                uploader=self._upload_clip,
                pre_roll_s=float(st["clips"]["pre_roll_s"]),
                max_seconds=float(st["clips"]["max_seconds"]),
                keep_local=int(st["clips"]["keep_local"]),
                current_camera=lambda: self.cameras.selected,
            )
        self.display_state = DisplayState()
        self.display_state.stream_led = bool(cfg["privacy"]["stream_led"])
        if det_enabled:
            from .detector import Detector

            self.detector = Detector(
                s_det, float(s_cam["hfov_deg"]), loop,
                publish=lambda m: self.zmq.publish("detections", m) if self.zmq is not None else None,
                on_pet_event=self._on_pet_event,
                on_sighting=lambda s: self.display_state.pet_sighting(s.bearing_rad if s else None),
            )
        if self.enable_hw:
            if self.detector is not None:
                self.detector.start()
                self.cameras.lores_sinks.append(self.detector.push)
            self.encoder.start()
            self.cameras.start(allow_synthetic=True)
            if st["display"]["enabled"]:
                from .display.renderer import Renderer

                d = st["display"]
                self.renderer = Renderer(self.display_state, int(d["size"]), int(d["fps"]), d["driver"])
                self.renderer.start()
            if st["audio"].get("enabled", True):
                a = st["audio"]
                self.speaker = SpeakerMixer(a["playback_device"], int(a["sample_rate"]))
                self.mic = MicSource(a["capture_device"], int(a["sample_rate"]), int(a["channels"]))

        from .webrtc import WebRtcManager

        self.webrtc = WebRtcManager(
            self.encoder, self.router, self._ice_servers,
            mic=self.mic, speaker=self.speaker,
            on_owner_frame=self._on_owner_frame,
            on_viewers_changed=self._on_viewers_changed,
            max_viewers=int(st["webrtc"]["max_viewers"]),
            ice_ttl_s=float(st["webrtc"]["ice_refresh_s"]),
            accept_owner_video=bool(st["display"]["enabled"]),
        )

    async def _start_cloud(self) -> None:
        if self._cloud_started or self.api is None:
            return
        from .cloud.signaling import SignalingClient

        self._cloud_started = True
        self.signaling = SignalingClient(
            cloud_url=lambda: self.cloud_state.cloud_url,
            token=lambda force: self.api.token(force=force),
            handler=self,
            ping_interval_s=float(self.settings["cloud"]["ping_interval_s"]),
            pong_timeout_s=float(self.settings["cloud"]["pong_timeout_s"]),
        )
        self.signaling.start()
        self._tasks.append(asyncio.ensure_future(self._sync_config_at_boot()))

    async def _sync_config_at_boot(self) -> None:
        """GET desired config (§6) → apply → POST reported. Retries until success."""
        delay = 5.0
        while True:
            try:
                desired = await self.api.get_config()
                if desired:
                    try:
                        self.config.apply(desired)
                    except ConfigError as e:
                        log.error("cloud desired config invalid; keeping local", extra=kv(error=str(e)))
                await self.api.post_config_reported(self.config.data)
                log.info("config synced with cloud")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("config sync failed", extra=kv(error=str(e), retry_s=delay))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def _start_ble(self) -> None:
        from .ble import wifi
        from .ble.provisioning import Provisioner, ProvisioningWindow

        assert self.identity is not None
        sb = self.settings["ble"]
        iface = sb["nm_iface"]
        ident = self.identity
        if sb.get("wifi", "nmcli") == "fake":
            self.fake_wifi = wifi.FakeWifi()
            wifi_connect = self.fake_wifi.connect
            wifi_scan = self.fake_wifi.scan
        else:
            wto = float(sb.get("wifi_timeout_s", 45))

            def wifi_connect(ssid: str, pw: str):  # type: ignore[no-redef]
                return wifi.connect(ssid, pw, iface, timeout_s=wto)

            scans = {"n": 0}

            def wifi_scan():  # type: ignore[no-redef]
                # Fresh scan before advertising; afterwards "auto" (rescan only if the
                # list is >30 s old): active scans while a phone is connected over BLE
                # can disturb the Pi's shared Wi-Fi/BT radio.
                scans["n"] += 1
                return wifi.scan(iface, rescan="yes" if scans["n"] == 1 else "auto")

        factory = None
        if sb.get("transport", "bless") == "loopback":
            from .ble.loopback import LoopbackGattServer

            factory = LoopbackGattServer

        def make() -> Provisioner:
            return Provisioner(
                ident.device_id,
                wifi_connect=wifi_connect,
                wifi_scan=wifi_scan,
                register=self._provision_register,
                on_done=self._provision_done,
                allow_http=bool(self.settings["cloud"].get("allow_http", False)),
            )

        self.ble = ProvisioningWindow(
            make, f"Petmate-{ident.short_id}",
            is_provisioned=lambda: self.cloud_state.provisioned,
            window_s=float(sb["window_s"]),
            adapter=sb["adapter"],
            server_factory=factory,
        )
        await self.ble.start_if_needed()

    # =============================================================== shutdown
    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        steps = [
            ("webrtc", lambda: self.webrtc.close_all() if self.webrtc else None),
            ("signaling", lambda: self.signaling.stop() if self.signaling else None),
            ("ble", lambda: self.ble.close("shutdown") if self.ble and self.ble.active else None),
            ("speaker", lambda: self.speaker.close() if self.speaker else None),
            ("api", lambda: self.api.close() if self.api else None),
            ("zmq", lambda: self.zmq.stop() if self.zmq else None),
        ]
        for name, fn in steps:
            try:
                r = fn()
                if asyncio.iscoroutine(r):
                    await asyncio.wait_for(r, 5)
            except Exception as e:  # noqa: BLE001
                log.warning("shutdown step failed", extra=kv(step=name, error=repr(e)))
        for comp in (self.cameras, self.encoder, self.detector, self.renderer):
            if comp is not None:
                try:
                    comp.stop()
                except Exception as e:  # noqa: BLE001
                    log.warning("stop failed", extra=kv(component=type(comp).__name__, error=repr(e)))
        log.info("pm-core stopped")

    # =============================================================== ROS → core
    def _no_ros(self, cmd: dict) -> dict:
        raise CommandError(ERR_NOT_SUPPORTED)

    async def _local_state_loop(self) -> None:
        """No ROS (MVP): publish a placeholder §4.3 state at ``state.local_rate_hz``."""
        period = 1.0 / max(0.1, float(self.settings["state"].get("local_rate_hz", 2.0)))
        while True:
            self._on_ros_state("state", placeholder_state(camera=self.cameras.selected if self.cameras else None))
            await asyncio.sleep(period)

    def _on_ros_state(self, _topic: str, st: dict) -> None:
        msg = dict(st)
        msg["t"] = "state"
        if self.display_state is not None:
            self.display_state.update_robot(msg)
        if self.webrtc is not None:
            self.webrtc.broadcast(msg)

    async def _on_ros_event(self, _topic: str, ev: dict) -> None:
        t = ev.get("t")
        if not isinstance(t, str):
            return
        if t == "display_game":
            game = DISPLAY_GAME_ALIASES.get(str(ev.get("game")))
            if game is None or ev.get("active") is False:
                self.display_state.set_mode("face")
            else:
                dur = ev.get("duration_s")
                self.display_state.set_mode("game", game, float(dur) if isinstance(dur, (int, float)) else None)
            log.info("display_game from ROS", extra=kv(game=game))
            return
        if t == "button_provision":
            log.info("button_provision: opening BLE provisioning window")
            if self.ble is not None:
                await self.ble.on_button_provision()
            return
        ev = dict(ev)
        ev.setdefault("ts", time.time())
        self._publish_event(ev)

    async def _on_ros_ack(self, _topic: str, ack: dict) -> None:
        await self.router.on_ros_ack(ack)

    def _publish_event(self, ev: dict) -> None:
        if self.webrtc is not None:
            self.webrtc.broadcast(dc_event(ev))
        if self.api is not None:
            self.api.enqueue_event(ev)

    def _on_pet_event(self, ev: dict) -> None:
        self._publish_event(ev)
        if self.settings["clips"]["auto_on_pet_detected"] and self.clips is not None:
            asyncio.ensure_future(self._auto_clip())

    async def _auto_clip(self) -> None:
        try:
            await self.clips.record(float(self.settings["clips"]["default_seconds"]), reason="pet_detected")
        except Exception as e:  # noqa: BLE001
            log.info("auto clip skipped", extra=kv(error=str(e)))

    # =============================================================== signaling handler
    async def on_offer(self, peer: str, sdp: str) -> Optional[str]:
        return await self.webrtc.on_offer(peer, sdp)

    async def on_ice(self, peer: str, candidate: dict) -> None:
        await self.webrtc.on_ice(peer, candidate)

    async def on_bye(self, peer: str) -> None:
        await self.webrtc.on_bye(peer)

    async def on_command(self, cmd: dict) -> None:
        await self.router.handle(cmd, reply=None, source="cloud")

    async def on_config_set(self, config: dict) -> None:
        try:
            await self.config_set(config)
        except ConfigError as e:
            log.error("config_set from cloud rejected", extra=kv(error=str(e)))
            # report the effective config so the cloud sees the divergence
            if self.api is not None:
                asyncio.ensure_future(self._report_config())

    # =============================================================== LocalHandlers
    async def set_camera(self, cam: str) -> None:
        try:
            self.cameras.select(cam)
        except CameraNotAvailable:
            raise CommandError(ERR_CAMERA_NOT_AVAILABLE)
        self.encoder.request_keyframe()

    async def set_display(self, mode: str, game: Optional[str]) -> None:
        self.display_state.set_mode(mode, game)

    async def set_volume(self, speaker: int) -> None:
        self.config.apply({"audio": {"speaker_volume": int(speaker)}})

    async def snapshot_jpeg(self) -> bytes:
        from .clips import snapshot

        if not self.cameras.cams:
            raise CommandError(ERR_CAMERA_NOT_AVAILABLE)
        try:
            return await snapshot(self.cameras, self.cameras.selected, int(self.settings["video"]["jpeg_quality"]))
        except asyncio.TimeoutError:  # e.g. USB webcam unplugged
            raise CommandError(ERR_CAMERA_NOT_AVAILABLE)

    async def record_clip(self, seconds: float) -> dict:
        if self.clips is None:
            raise CommandError(ERR_NOT_SUPPORTED)
        try:
            return await self.clips.record(seconds, reason="manual")
        except RuntimeError as e:
            raise CommandError(str(e))

    def config_get(self) -> dict:
        return self.config.data

    async def config_set(self, cfg: dict) -> dict:
        new, _changed = self.config.apply(cfg)
        return new

    # =============================================================== config changes
    def _on_config_changed(self, new: dict, old: dict) -> None:
        log.info("config changed")
        if self.zmq is not None:
            self.zmq.publish_config(new)
        if (new["audio"]["speaker_volume"] != old["audio"]["speaker_volume"]
                and self.settings["audio"].get("enabled", True)):
            a = self.settings["audio"]
            asyncio.ensure_future(set_volume(a["mixer_card"], a["mixer_control"], new["audio"]["speaker_volume"]))
        if self.display_state is not None:
            self.display_state.stream_led = bool(new["privacy"]["stream_led"])
        if not new["privacy"]["rear_camera_streaming"] and self.cameras and self.cameras.selected == "rear":
            self.cameras.select("front")
            self.encoder.request_keyframe()
        if self.webrtc is not None:
            self.webrtc.broadcast({"t": "config", "config": new})
        if self.api is not None and self.cloud_state.provisioned:
            asyncio.ensure_future(self._report_config())

    async def _report_config(self) -> None:
        try:
            await self.api.post_config_reported(self.config.data)
        except Exception as e:  # noqa: BLE001
            log.warning("config reported failed", extra=kv(error=str(e)))

    # =============================================================== media callbacks
    def _on_viewers_changed(self, n: int) -> None:
        if self.display_state is not None:
            self.display_state.streaming = n > 0

    def _on_owner_frame(self, frame: Any) -> None:
        ds = self.display_state
        if ds is None:
            return
        if frame is None:
            ds.set_owner_frame(None)
            if ds.mode == "owner":
                ds.set_mode("face")
            return
        now = time.monotonic()
        if now - self._last_owner_push < 1.0 / 20:
            return
        self._last_owner_push = now
        try:
            # downscale before handing over; display smoothscales to 1080
            w = 540
            h = max(2, int(frame.height * w / max(1, frame.width)) // 2 * 2)
            rgb = frame.reformat(width=w, height=h, format="rgb24").to_ndarray()
        except Exception:  # noqa: BLE001
            return
        first = ds.owner_frame is None
        ds.set_owner_frame(rgb)
        if first and ds.mode != "owner":
            ds.set_mode("owner")

    async def _ice_servers(self) -> list[dict]:
        if self.api is None:
            return []
        return await self.api.ice_servers()

    async def _upload_clip(self, path: Any, meta: dict) -> Optional[str]:
        if self.api is None or not self.cloud_state.provisioned:
            raise RuntimeError("cloud not available")
        return await self.api.upload_clip(path, meta)

    # =============================================================== provisioning
    async def _provision_register(self, claim_token: str, cloud_url: Optional[str]) -> Any:
        assert self.api is not None
        # an explicit settings override (cloud.url / PMCORE_CLOUD_URL) wins over the payload
        override = str(self.settings["cloud"].get("url") or "").strip()
        return await self.api.register(claim_token, override.rstrip("/") if override else cloud_url)

    async def _provision_done(self, payload: Any) -> None:
        if payload.timezone:
            try:
                self.config.apply({"timezone": payload.timezone})
            except ConfigError as e:
                log.warning("provisioned timezone rejected", extra=kv(error=str(e)))
        if self.cloud_state.timezone != payload.timezone:
            self.cloud_state.timezone = payload.timezone
            self.cloud_state.save(self.paths.cloud_state)
        await self._start_cloud()
        if self.ble is not None:
            asyncio.ensure_future(self.ble.finished())

    # =============================================================== health
    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            log.info("health", extra=kv(
                viewers=self.webrtc.viewers if self.webrtc else 0,
                ros_alive=self.zmq.ros_alive if self.zmq else None,
                cams=self.cameras.status() if self.cameras else {},
                enc=dict(self.encoder.stats) if self.encoder else {},
                det=dict(self.detector.stats) if self.detector else {},
                detector=self.detector.backend_name if self.detector else None,
                signaling=bool(self.signaling and self.signaling.connected.is_set()),
                provisioned=self.cloud_state.provisioned,
                ble=bool(self.ble and self.ble.active),
            ))
