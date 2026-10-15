"""ZeroMQ IPC with the ROS 2 bridge (INTERFACES.md §3).

* PUB **bind** tcp://127.0.0.1:5560 — topics ``cmd``, ``detections``, ``config``.
* SUB **connect** tcp://127.0.0.1:5561 — topics ``state``, ``event``, ``ack``.
* Multipart ``[topic utf-8, payload JSON utf-8]``.

Slow-joiner handling: ZMQ PUB drops messages until the subscriber has
connected, so the full ``config`` is re-published every
``config_republish_s`` seconds and immediately when ``state`` resumes after a
silence (i.e. the ROS container (re)started).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from .log import get_logger, kv

log = get_logger(__name__)

IN_TOPICS = ("state", "event", "ack")
OUT_TOPICS = ("cmd", "detections", "config")

Handler = Callable[[str, dict], Any]


def ensure_cmd_id(cmd: dict) -> dict:
    """Return a copy of ``cmd`` with a mandatory string ``id`` (uuid4 if absent)."""
    out = dict(cmd)
    cid = out.get("id")
    if not isinstance(cid, str) or not cid:
        out["id"] = str(uuid.uuid4())
    return out


def encode(topic: str, payload: dict) -> list[bytes]:
    return [topic.encode("utf-8"), json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")]


def decode(frames: list[bytes]) -> Optional[tuple[str, dict]]:
    if len(frames) != 2:
        return None
    try:
        topic = frames[0].decode("utf-8")
        payload = json.loads(frames[1].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return topic, payload


class ZmqBridge:
    def __init__(
        self,
        pub_endpoint: str = "tcp://127.0.0.1:5560",
        sub_endpoint: str = "tcp://127.0.0.1:5561",
        config_provider: Optional[Callable[[], dict]] = None,
        config_republish_s: float = 30.0,
        state_gap_s: float = 5.0,
    ):
        self.pub_endpoint = pub_endpoint
        self.sub_endpoint = sub_endpoint
        self.config_provider = config_provider
        self.config_republish_s = config_republish_s
        self.state_gap_s = state_gap_s
        self._handlers: dict[str, list[Handler]] = {t: [] for t in IN_TOPICS}
        self._ctx = None
        self._pub = None
        self._sub = None
        self._tasks: list[asyncio.Task] = []
        self._last_state = 0.0
        self.last_rx: dict[str, float] = {}

    def on(self, topic: str, handler: Handler) -> None:
        if topic not in self._handlers:
            raise ValueError(f"unknown ros→core topic {topic}")
        self._handlers[topic].append(handler)

    async def start(self) -> None:
        import zmq
        import zmq.asyncio

        self._ctx = zmq.asyncio.Context.instance()
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.LINGER, 0)
        self._pub.setsockopt(zmq.SNDHWM, 1000)
        self._pub.bind(self.pub_endpoint)
        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.setsockopt(zmq.LINGER, 0)
        self._sub.setsockopt(zmq.RCVHWM, 1000)
        self._sub.setsockopt(zmq.RECONNECT_IVL, 200)
        self._sub.setsockopt(zmq.RECONNECT_IVL_MAX, 2000)
        for t in IN_TOPICS:
            # exact-topic match: subscription is a prefix filter on frame 0
            self._sub.setsockopt(zmq.SUBSCRIBE, t.encode())
        self._sub.connect(self.sub_endpoint)
        self._tasks.append(asyncio.ensure_future(self._rx_loop()))
        self._tasks.append(asyncio.ensure_future(self._config_loop()))
        log.info("zmq bridge started", extra=kv(pub=self.pub_endpoint, sub=self.sub_endpoint))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        for s in (self._pub, self._sub):
            if s is not None:
                s.close(0)
        self._pub = self._sub = None

    # -------------------------------------------------------------- publish
    def publish(self, topic: str, payload: dict) -> None:
        if topic not in OUT_TOPICS:
            raise ValueError(f"unknown core→ros topic {topic}")
        if self._pub is None:
            log.debug("zmq publish before start", extra=kv(topic=topic))
            return
        import zmq

        try:
            self._pub.send_multipart(encode(topic, payload), flags=zmq.NOBLOCK)
        except zmq.Again:
            log.warning("zmq pub HWM reached, dropping", extra=kv(topic=topic))

    def publish_cmd(self, cmd: dict) -> dict:
        out = ensure_cmd_id(cmd)
        self.publish("cmd", out)
        return out

    def publish_config(self, cfg: Optional[dict] = None) -> None:
        if cfg is None and self.config_provider is not None:
            cfg = self.config_provider()
        if cfg is not None:
            self.publish("config", cfg)

    # -------------------------------------------------------------- receive
    async def _rx_loop(self) -> None:
        assert self._sub is not None
        while True:
            frames = await self._sub.recv_multipart()
            dec = decode(frames)
            if dec is None:
                log.warning("zmq: malformed message dropped", extra=kv(frames=len(frames)))
                continue
            topic, payload = dec
            if topic not in self._handlers:
                continue
            now = time.monotonic()
            if topic == "state":
                if now - self._last_state > self.state_gap_s:
                    log.info("ros state stream (re)started; republishing config")
                    self.publish_config()
                self._last_state = now
            self.last_rx[topic] = now
            for h in self._handlers[topic]:
                try:
                    r = h(topic, payload)
                    if asyncio.iscoroutine(r):
                        await r
                except Exception:  # noqa: BLE001
                    log.exception("zmq handler failed", extra=kv(topic=topic))

    async def _config_loop(self) -> None:
        # Give the subscriber a moment to connect, then publish periodically.
        await asyncio.sleep(0.5)
        while True:
            self.publish_config()
            await asyncio.sleep(self.config_republish_s)

    @property
    def ros_alive(self) -> bool:
        return time.monotonic() - self._last_state < self.state_gap_s
