"""Signaling WebSocket client (INTERFACES.md §6).

``wss://<host>/v1/signal?token=<device JWT>``. The server sends
``{"type":"hello","peer":"device:<id>"}`` first. Supported inbound types:
hello, offer, ice, bye, command, config_set, ping, pong, presence, error.

Outbound: answer, ice (unused: aiortc does not trickle; answer carries all
candidates), bye, ping (every 20 s; reconnect if nothing is received within
``ping_interval + pong_timeout``).

The socket is abstracted behind :class:`WsTransport` so tests can inject a
fake; production uses aiohttp.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional, Protocol
from urllib.parse import quote

from ..log import get_logger, kv
from ..util import Backoff

log = get_logger(__name__)


class WsTransport(Protocol):
    async def send(self, text: str) -> None: ...
    async def recv(self) -> Optional[str]: ...  # None when closed
    async def close(self) -> None: ...


class SignalingHandler(Protocol):
    async def on_offer(self, peer: str, sdp: str) -> Optional[str]: ...  # returns answer SDP
    async def on_ice(self, peer: str, candidate: dict) -> None: ...
    async def on_bye(self, peer: str) -> None: ...
    async def on_command(self, cmd: dict) -> None: ...
    async def on_config_set(self, config: dict) -> None: ...


Connector = Callable[[str], Awaitable[WsTransport]]
TokenProvider = Callable[[bool], Awaitable[str]]  # force_refresh -> token


def signal_url(cloud_url: str, token: str) -> str:
    base = cloud_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/signal?token={quote(token, safe='')}"


class AiohttpTransport:
    def __init__(self, session: Any, ws: Any):
        self._session = session
        self._ws = ws

    @classmethod
    async def connect(cls, url: str) -> "AiohttpTransport":
        import aiohttp

        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=15))
        try:
            ws = await session.ws_connect(url, heartbeat=None, autoping=True, max_msg_size=1 << 20)
        except BaseException:
            await session.close()
            raise
        return cls(session, ws)

    async def send(self, text: str) -> None:
        await self._ws.send_str(text)

    async def recv(self) -> Optional[str]:
        import aiohttp

        while True:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                return msg.data
            if msg.type == aiohttp.WSMsgType.BINARY:
                return msg.data.decode("utf-8", errors="replace")
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                return None

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            await self._session.close()


class SignalingClient:
    def __init__(
        self,
        cloud_url: Callable[[], str],
        token: TokenProvider,
        handler: SignalingHandler,
        connector: Connector = AiohttpTransport.connect,
        ping_interval_s: float = 20.0,
        pong_timeout_s: float = 15.0,
        backoff: Optional[Backoff] = None,
    ):
        self._cloud_url = cloud_url
        self._token = token
        self.handler = handler
        self._connector = connector
        self.ping_interval_s = ping_interval_s
        self.pong_timeout_s = pong_timeout_s
        self._backoff = backoff or Backoff(1.0, 60.0)
        self._ws: Optional[WsTransport] = None
        self._task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self.peer: Optional[str] = None
        self.connected = asyncio.Event()
        self._last_rx = 0.0
        self._stopping = False
        self._handler_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._close_ws()

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        self.connected.clear()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        force_token = False
        while not self._stopping:
            try:
                tok = await self._token(force_token)
                force_token = False
                url = signal_url(self._cloud_url(), tok)
                self._ws = await self._connector(url)
                log.info("signaling connected")
                self._backoff.reset()
                await self.run_session(self._ws)
                log.info("signaling disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status", None)
                if status in (401, 403):
                    force_token = True
                log.warning("signaling error", extra=kv(error=repr(e), status=status))
            finally:
                await self._close_ws()
            if self._stopping:
                break
            d = self._backoff.next_delay()
            log.info("signaling reconnect", extra=kv(in_s=round(d, 1)))
            await asyncio.sleep(d)

    async def run_session(self, ws: WsTransport) -> None:
        """Pump one connected socket until it closes or goes silent."""
        self._ws = ws
        self._last_rx = time.monotonic()
        pinger = asyncio.ensure_future(self._ping_loop())
        try:
            while True:
                text = await ws.recv()
                if text is None:
                    return
                self._last_rx = time.monotonic()
                await self.dispatch(text)
        finally:
            pinger.cancel()
            try:
                await pinger
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.connected.clear()
            self.peer = None

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval_s)
            if time.monotonic() - self._last_rx > self.ping_interval_s + self.pong_timeout_s:
                log.warning("signaling: pong timeout, reconnecting")
                ws = self._ws
                if ws is not None:
                    await ws.close()
                return
            try:
                await self.send({"type": "ping"})
            except Exception:  # noqa: BLE001
                return

    # ------------------------------------------------------------- messages
    async def send(self, msg: dict) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("signaling not connected")
        async with self._send_lock:
            await ws.send(json.dumps(msg, separators=(",", ":")))

    def _spawn(self, coro: Awaitable[Any]) -> None:
        t = asyncio.ensure_future(coro)
        self._handler_tasks.add(t)
        t.add_done_callback(self._handler_tasks.discard)

    async def drain(self) -> None:
        """Wait for in-flight handler tasks (used by tests and shutdown)."""
        while self._handler_tasks:
            await asyncio.gather(*list(self._handler_tasks), return_exceptions=True)

    async def dispatch(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            log.warning("signaling: invalid JSON")
            return
        if not isinstance(msg, dict):
            return
        typ = msg.get("type")
        frm = msg.get("from")
        if typ == "hello":
            self.peer = msg.get("peer")
            self.connected.set()
            log.info("signaling hello", extra=kv(peer=self.peer))
        elif typ == "ping":
            await self.send({"type": "pong"})
        elif typ == "pong":
            pass
        elif typ == "offer":
            if not isinstance(frm, str) or not frm.startswith("user:") or not isinstance(msg.get("sdp"), str):
                log.warning("signaling: bad offer", extra=kv(frm=frm))
                return
            # SDP negotiation may take a while (ICE gathering): don't block the reader.
            self._spawn(self._answer(frm, msg["sdp"]))
        elif typ == "ice":
            if isinstance(frm, str) and isinstance(msg.get("candidate"), dict):
                self._spawn(self.handler.on_ice(frm, msg["candidate"]))
        elif typ == "bye":
            if isinstance(frm, str):
                self._spawn(self.handler.on_bye(frm))
        elif typ == "command":
            if isinstance(msg.get("cmd"), dict):
                self._spawn(self.handler.on_command(msg["cmd"]))
        elif typ == "config_set":
            if isinstance(msg.get("config"), dict):
                self._spawn(self.handler.on_config_set(msg["config"]))
        elif typ == "error":
            log.warning("signaling server error", extra=kv(error=msg.get("error")))
        elif typ == "presence":
            pass
        else:
            log.debug("signaling: unknown type", extra=kv(type=typ))

    async def _answer(self, peer: str, sdp: str) -> None:
        try:
            answer = await self.handler.on_offer(peer, sdp)
        except Exception as e:  # noqa: BLE001
            log.exception("offer handling failed", extra=kv(peer=peer))
            answer = None
            err = str(e)
        else:
            err = "rejected"
        if answer is None:
            try:
                await self.send({"type": "bye", "to": peer, "reason": err})
            except Exception:  # noqa: BLE001
                pass
            return
        await self.send({"type": "answer", "to": peer, "sdp": answer})

    async def send_bye(self, peer: str) -> None:
        try:
            await self.send({"type": "bye", "to": peer})
        except Exception:  # noqa: BLE001
            pass
