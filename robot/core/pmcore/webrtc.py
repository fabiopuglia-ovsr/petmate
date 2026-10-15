"""WebRTC sessions with the app (INTERFACES.md §4).

One :class:`Session` per app peer (``user:<uid>:<session_uuid>``), at most
``max_viewers`` concurrently. The app sends a complete (non-trickle) offer;
pm-core answers with all candidates gathered.

Per session:
* video transceiver, H.264 only (pre-encoded packets from the shared
  encoder; camera switch never renegotiates), direction sendrecv when the app
  offers a video send (owner mode), else sendonly;
* audio transceiver: mic (Opus) out, owner voice in → :class:`SpeakerMixer`;
  without mic/speaker (MVP profile) the audio m-line is answered ``inactive``;
* DataChannel ``ctl`` created by the app → :class:`CommandRouter`; state and
  events broadcast to every open ``ctl``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional

from .log import get_logger, kv

log = get_logger(__name__)


def ice_config(servers: list[dict]):
    from aiortc import RTCConfiguration, RTCIceServer

    out = []
    for s in servers:
        urls = s.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            continue
        out.append(RTCIceServer(urls=urls, username=s.get("username"), credential=s.get("credential")))
    return RTCConfiguration(iceServers=out)


def h264_preferences():
    """Codec preferences for the video transceiver: H.264 CB (42e01f) first, + RTX."""
    from aiortc import RTCRtpSender

    caps = RTCRtpSender.getCapabilities("video").codecs
    h264 = [c for c in caps if c.mimeType.lower() == "video/h264"]
    h264.sort(key=lambda c: 0 if c.parameters.get("profile-level-id", "").lower() == "42e01f" else 1)
    rtx = [c for c in caps if c.mimeType.lower() == "video/rtx"]
    return h264 + rtx


def offer_media(sdp: str) -> dict[str, str]:
    """Return {kind: direction} of the first audio/video m-lines of an SDP offer."""
    res: dict[str, str] = {}
    kind = None
    for line in sdp.splitlines():
        if line.startswith("m="):
            kind = line[2:].split(" ", 1)[0]
            if kind in ("audio", "video") and kind not in res:
                res[kind] = "sendrecv"
            else:
                kind = None
        elif kind and line.strip() in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"):
            res[kind] = line.strip()[2:]
            kind = None
    return res


class Session:
    def __init__(self, mgr: "WebRtcManager", peer: str):
        self.mgr = mgr
        self.peer = peer
        self.pc = None
        self.channel = None
        self.created = time.time()
        self._video_track = None
        self._mic_track = None
        self._speaker_id: Optional[int] = None
        self._owner_task: Optional[asyncio.Task] = None
        self.closed = False

    async def answer(self, offer_sdp: str) -> str:
        from aiortc import RTCPeerConnection, RTCSessionDescription

        mgr = self.mgr
        media = offer_media(offer_sdp)
        if "video" not in media:
            raise ValueError("offer has no video m-line")
        self.pc = pc = RTCPeerConnection(ice_config(await mgr.ice_servers()))

        @pc.on("datachannel")
        def _on_dc(ch) -> None:
            if ch.label != "ctl":
                log.info("ignoring datachannel", extra=kv(label=ch.label, peer=self.peer))
                return
            self.channel = ch
            log.info("ctl open", extra=kv(peer=self.peer))

            @ch.on("message")
            def _on_msg(m) -> None:
                asyncio.ensure_future(self._on_ctl(m))

            @ch.on("close")
            def _on_close() -> None:
                mgr.router.drop_origin(self.send)

            # initial snapshot of state for the newcomer
            if mgr.last_state is not None:
                self.send(mgr.last_state)

        @pc.on("track")
        def _on_track(track) -> None:
            log.info("incoming track", extra=kv(kind=track.kind, peer=self.peer))
            if track.kind == "audio":
                self._speaker_id = mgr.speaker.add_track(track) if mgr.speaker else None
            elif track.kind == "video":
                self._owner_task = asyncio.ensure_future(mgr.consume_owner_video(self, track))

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            st = pc.connectionState
            log.info("pc state", extra=kv(peer=self.peer, state=st))
            if st in ("failed", "closed"):
                await mgr.close_session(self.peer, reason=st)

        # Pre-create transceivers so codec preferences apply to the answer.
        from .video import EncodedVideoTrack

        self._video_track = EncodedVideoTrack(mgr.encoder, asyncio.get_running_loop())
        owner_video = media.get("video") in ("sendrecv", "sendonly") and mgr.accept_owner_video
        vt = pc.addTransceiver(self._video_track, direction="sendrecv" if owner_video else "sendonly")
        vt.setCodecPreferences(h264_preferences())
        if "audio" in media:
            # Offer direction is the app's view: sendrecv = mic to robot + listen to robot.
            remote_sends = media["audio"] in ("sendrecv", "sendonly")
            remote_recvs = media["audio"] in ("sendrecv", "recvonly")
            # Only receive owner audio when something plays it: an unconsumed aiortc
            # remote track would queue decoded frames forever.
            want_recv = remote_sends and mgr.speaker is not None
            self._mic_track = mgr.mic.track() if (mgr.mic and remote_recvs) else None
            if self._mic_track is not None:
                pc.addTransceiver(self._mic_track, direction="sendrecv" if want_recv else "sendonly")
            else:
                # MVP (audio disabled): the audio m-line is answered "inactive".
                pc.addTransceiver("audio", direction="recvonly" if want_recv else "inactive")

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        if not any(c.mimeType.lower() == "video/h264" for c in (vt._codecs or [])):
            raise ValueError("app offer does not support H.264 constrained baseline")
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)  # waits for ICE gathering (aiortc)
        return pc.localDescription.sdp

    async def add_ice(self, cand: dict) -> None:
        if self.pc is None:
            return
        from aiortc.sdp import candidate_from_sdp

        c = cand.get("candidate")
        if not c:
            return  # end-of-candidates
        ic = candidate_from_sdp(c.split(":", 1)[1] if c.startswith("candidate:") else c)
        ic.sdpMid = cand.get("sdpMid")
        ic.sdpMLineIndex = cand.get("sdpMLineIndex")
        await self.pc.addIceCandidate(ic)

    def send(self, msg: dict) -> bool:
        ch = self.channel
        if ch is None or ch.readyState != "open":
            return False
        try:
            ch.send(json.dumps(msg, separators=(",", ":"), ensure_ascii=False))
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("ctl send failed", extra=kv(peer=self.peer, error=str(e)))
            return False

    async def _on_ctl(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)
        except ValueError:
            self.send({"t": "ack", "id": None, "ok": False, "error": "invalid JSON"})
            return
        await self.mgr.router.handle(msg, reply=self.send, source=self.peer)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.mgr.router.drop_origin(self.send)
        if self._owner_task:
            self._owner_task.cancel()
        if self._speaker_id is not None and self.mgr.speaker:
            self.mgr.speaker.remove(self._speaker_id)
        for t in (self._video_track, self._mic_track):
            if t is not None:
                try:
                    t.stop()
                except Exception:  # noqa: BLE001
                    pass
        if self.pc is not None:
            try:
                await self.pc.close()
            except Exception:  # noqa: BLE001
                pass


class WebRtcManager:
    def __init__(
        self,
        encoder: Any,
        router: Any,
        ice_provider: Callable[[], Awaitable[list[dict]]],
        mic: Any = None,
        speaker: Any = None,
        on_owner_frame: Optional[Callable[[Optional[Any]], None]] = None,
        on_viewers_changed: Optional[Callable[[int], None]] = None,
        max_viewers: int = 2,
        ice_ttl_s: float = 3000.0,
        accept_owner_video: bool = True,
    ):
        self.encoder = encoder
        self.router = router
        self._ice_provider = ice_provider
        self.mic = mic
        self.speaker = speaker
        self.on_owner_frame = on_owner_frame
        self.on_viewers_changed = on_viewers_changed
        self.max_viewers = max_viewers
        self.ice_ttl_s = ice_ttl_s
        self.accept_owner_video = accept_owner_video  # False without a display (MVP)
        self.sessions: dict[str, Session] = {}
        self.last_state: Optional[dict] = None
        self._ice_cache: tuple[float, list[dict]] = (0.0, [])
        self._owner_peer: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def viewers(self) -> int:
        return len(self.sessions)

    async def ice_servers(self) -> list[dict]:
        ts, servers = self._ice_cache
        if servers and time.monotonic() - ts < self.ice_ttl_s:
            return servers
        try:
            servers = await self._ice_provider()
            self._ice_cache = (time.monotonic(), servers)
        except Exception as e:  # noqa: BLE001
            log.warning("ice-servers fetch failed; using cache/none", extra=kv(error=str(e)))
        return self._ice_cache[1]

    def _notify(self) -> None:
        if self.on_viewers_changed:
            self.on_viewers_changed(self.viewers)

    # ------------------------------------------------------ signaling handler
    async def on_offer(self, peer: str, sdp: str) -> Optional[str]:
        async with self._lock:
            if peer in self.sessions:  # renegotiation/reconnect from same app session
                await self._close(peer, "replaced")
            if len(self.sessions) >= self.max_viewers:
                log.warning("viewer limit reached; rejecting", extra=kv(peer=peer, max=self.max_viewers))
                return None
            s = Session(self, peer)
            self.sessions[peer] = s
        try:
            ans = await s.answer(sdp)
        except Exception as e:  # noqa: BLE001
            log.error("answer failed", extra=kv(peer=peer, error=str(e)))
            await self.close_session(peer, "answer_failed")
            raise
        log.info("session answered", extra=kv(peer=peer, viewers=self.viewers))
        self._notify()
        return ans

    async def on_ice(self, peer: str, candidate: dict) -> None:
        s = self.sessions.get(peer)
        if s:
            try:
                await s.add_ice(candidate)
            except Exception as e:  # noqa: BLE001
                log.debug("addIceCandidate failed", extra=kv(peer=peer, error=str(e)))

    async def on_bye(self, peer: str) -> None:
        await self.close_session(peer, "bye")

    # ------------------------------------------------------ lifecycle
    async def _close(self, peer: str, reason: str) -> None:
        s = self.sessions.pop(peer, None)
        if s is None:
            return
        await s.close()
        if self._owner_peer == peer:
            self._owner_peer = None
            if self.on_owner_frame:
                self.on_owner_frame(None)
        log.info("session closed", extra=kv(peer=peer, reason=reason, viewers=self.viewers))
        if self.mic:
            self.mic.release_if_idle(self.viewers)

    async def close_session(self, peer: str, reason: str = "") -> None:
        await self._close(peer, reason)
        self._notify()

    async def close_all(self) -> None:
        for p in list(self.sessions):
            await self._close(p, "shutdown")
        self._notify()

    # ------------------------------------------------------ broadcast
    def broadcast(self, msg: dict) -> int:
        if msg.get("t") == "state":
            self.last_state = msg
        n = 0
        for s in list(self.sessions.values()):
            n += 1 if s.send(msg) else 0
        return n

    # ------------------------------------------------------ owner video
    async def consume_owner_video(self, session: Session, track: Any) -> None:
        from aiortc.mediastreams import MediaStreamError

        self._owner_peer = session.peer
        try:
            while True:
                frame = await track.recv()
                if self._owner_peer != session.peer:
                    continue  # another viewer took over the display
                if self.on_owner_frame:
                    self.on_owner_frame(frame)
        except (MediaStreamError, asyncio.CancelledError):
            pass
