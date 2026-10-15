"""Signaling hub (INTERFACES.md §6, "Signaling WebSocket").

`SignalingHub` is the abstract interface used by the REST layer and the WebSocket
endpoint. `InMemoryHub` keeps everything in the process: fine for a single API
instance. A multi-instance deployment can implement the same interface on top of
Redis:

  * presence: `SADD petmate:online:devices <id>` / `HSET petmate:peer:<peer> instance <iid>`
    with a TTL refreshed on ping;
  * delivery: `PUBLISH petmate:peer:<peer> <json>`; each instance subscribes to the
    channels of its locally connected peers and writes to the local socket;
  * `deliver()` returns True when PUBLISH reports >= 1 receiver.

Peer naming: `device:<device_id>` or `user:<user_id>:<session_uuid>`.
"""
from __future__ import annotations

import abc
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("petmate.hub")

Message = dict[str, Any]


def device_peer(device_id: str) -> str:
    return f"device:{device_id}"


def parse_peer(peer: str) -> tuple[str, str, Optional[str]] | None:
    """Return (kind, id, session) or None if malformed."""
    if not isinstance(peer, str):
        return None
    parts = peer.split(":")
    if len(parts) == 2 and parts[0] == "device" and parts[1]:
        return "device", parts[1], None
    if len(parts) == 3 and parts[0] == "user" and parts[1] and parts[2]:
        return "user", parts[1], parts[2]
    return None


class Connection:
    """One live WebSocket. Outgoing messages go through a queue drained by a writer task
    running on the connection's own event loop, so `send()` is safe from any thread."""

    _CLOSE = "__close__"

    def __init__(
        self,
        peer: str,
        sender: Callable[[Message], Awaitable[None]],
        closer: Optional[Callable[[int], Awaitable[None]]] = None,
        max_queue: int = 256,
    ):
        self.peer = peer
        self._sender = sender
        self._closer = closer
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_queue)
        self._task = self._loop.create_task(self._writer())
        self.closed = False
        # peers this connection exchanged signaling with (for bye-on-disconnect)
        self.contacts: set[str] = set()

    async def _writer(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, tuple) and item[0] == self._CLOSE:
                self.closed = True
                if self._closer:
                    try:
                        await self._closer(item[1])
                    except Exception:
                        pass
                return
            try:
                await self._sender(item)
            except Exception as exc:  # socket gone
                log.debug("send to %s failed: %s", self.peer, exc)
                self.closed = True
                return

    def _put(self, item: Any) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            log.warning("outgoing queue full for %s, closing connection", self.peer)
            self.closed = True
            self._task.cancel()
            if self._closer:
                self._loop.create_task(self._closer(1013))

    def _submit(self, item: Any) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._put(item)
        else:
            self._loop.call_soon_threadsafe(self._put, item)

    def send(self, msg: Message) -> bool:
        """Thread-safe, non-blocking enqueue."""
        if self.closed:
            return False
        self._submit(msg)
        return True

    def kick(self, error: str, code: int = 4403) -> None:
        """Send an `error` message then close the socket (thread-safe)."""
        if self.closed:
            return
        self._submit({"type": "error", "error": error})
        self._submit((self._CLOSE, code))

    async def aclose(self) -> None:
        self.closed = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(self._task, timeout=2)
        except BaseException:
            self._task.cancel()


class SignalingHub(abc.ABC):
    """Connection registry + message routing. Authorization is done by the caller."""

    @abc.abstractmethod
    async def register(self, conn: Connection, user_id: Optional[int] = None,
                       device_id: Optional[str] = None) -> Optional[Connection]:
        """Register a connection. Returns a previous connection for the same peer
        (e.g. a device reconnecting) that the caller must close."""

    @abc.abstractmethod
    async def unregister(self, conn: Connection) -> bool:
        """Remove a connection. Returns True if it was the active one for its peer."""

    @abc.abstractmethod
    def deliver(self, peer: str, msg: Message) -> bool:
        """Send to a peer. Returns False if the peer is not connected."""

    @abc.abstractmethod
    def is_device_online(self, device_id: str) -> bool: ...

    @abc.abstractmethod
    def user_peers(self, user_id: int) -> list[str]: ...

    @abc.abstractmethod
    def is_connected(self, peer: str) -> bool: ...

    @abc.abstractmethod
    def kick(self, peer: str, error: str, code: int = 4403) -> bool:
        """Send an error to the peer and close its socket."""

    def notify_user(self, user_id: int, msg: Message) -> int:
        n = 0
        for p in self.user_peers(user_id):
            n += int(self.deliver(p, msg))
        return n

    def send_to_device(self, device_id: str, msg: Message) -> bool:
        return self.deliver(device_peer(device_id), msg)


@dataclass
class InMemoryHub(SignalingHub):
    peers: dict[str, Connection] = field(default_factory=dict)
    users: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))

    async def register(self, conn, user_id=None, device_id=None):
        old = self.peers.get(conn.peer)
        self.peers[conn.peer] = conn
        if user_id is not None:
            self.users[user_id].add(conn.peer)
        return old if old is not conn else None

    async def unregister(self, conn):
        if self.peers.get(conn.peer) is not conn:
            return False
        del self.peers[conn.peer]
        parsed = parse_peer(conn.peer)
        if parsed and parsed[0] == "user":
            uid = int(parsed[1])
            self.users[uid].discard(conn.peer)
            if not self.users[uid]:
                self.users.pop(uid, None)
        return True

    def deliver(self, peer, msg):
        conn = self.peers.get(peer)
        if conn is None or conn.closed:
            return False
        return conn.send(msg)

    def is_device_online(self, device_id):
        c = self.peers.get(device_peer(device_id))
        return c is not None and not c.closed

    def user_peers(self, user_id):
        return list(self.users.get(user_id, ()))

    def is_connected(self, peer):
        c = self.peers.get(peer)
        return c is not None and not c.closed

    def kick(self, peer, error, code=4403):
        c = self.peers.get(peer)
        if c is None:
            return False
        c.kick(error, code)
        return True
