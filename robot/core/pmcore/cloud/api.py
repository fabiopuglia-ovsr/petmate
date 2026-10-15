"""Cloud REST client for the device (INTERFACES.md §6).

Base URL ``<cloud_url>`` already includes ``/v1``. Device auth is a JWT
obtained from ``POST /devices/token`` (or ``/devices/register`` during
provisioning) and refreshed ahead of its 24 h expiry; on HTTP 401 the token
is refreshed once and the request retried.

Event delivery is store-and-forward: events go to an in-memory bounded queue
drained by a background task with backoff, so a cloud outage never blocks the
robot.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

from ..identity import CloudState, Identity
from ..log import get_logger, kv
from ..util import Backoff

log = get_logger(__name__)


class CloudError(RuntimeError):
    def __init__(self, status: int, msg: str):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


class AuthError(CloudError):
    pass


class CloudApi:
    def __init__(
        self,
        identity: Identity,
        state: CloudState,
        state_path: Path,
        timeout_s: float = 20.0,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.identity = identity
        self.state = state
        self.state_path = state_path
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session = session
        self._own_session = session is None
        self._token_lock = asyncio.Lock()
        self._events: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
        self._tasks: list[asyncio.Task] = []

    # ---------------------------------------------------------------- basics
    @property
    def base(self) -> str:
        return self.state.cloud_url.rstrip("/")

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._own_session = True
        return self._session

    async def start(self) -> None:
        self._tasks.append(asyncio.ensure_future(self._event_pump()))
        self._tasks.append(asyncio.ensure_future(self._token_refresher()))

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        if self._own_session and self._session is not None:
            await self._session.close()

    def _persist(self) -> None:
        try:
            self.state.save(self.state_path)
        except OSError as e:
            log.error("cannot persist cloud state", extra=kv(error=str(e)))

    @staticmethod
    async def _json_or_error(resp: aiohttp.ClientResponse) -> Any:
        text = await resp.text()
        if resp.status >= 400:
            detail = text[:300]
            try:
                j = json.loads(text)
                detail = j.get("detail") or j.get("error") or detail
            except ValueError:
                pass
            cls = AuthError if resp.status in (401, 403) else CloudError
            raise cls(resp.status, str(detail))
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise CloudError(resp.status, "invalid JSON response")

    # ---------------------------------------------------------------- auth
    async def register(self, claim_token: str, cloud_url: Optional[str] = None) -> dict:
        """POST /devices/register (provisioning). Persists token + owner."""
        if cloud_url:
            self.state.cloud_url = cloud_url
        s = await self.session()
        body = {
            "device_id": self.identity.device_id,
            "device_secret": self.identity.device_secret,
            "claim_token": claim_token,
        }
        async with s.post(f"{self.base}/devices/register", json=body) as r:
            data = await self._json_or_error(r)
        if not isinstance(data, dict) or "device_token" not in data:
            raise CloudError(500, "register: missing device_token")
        self.state.set_token(data["device_token"])
        self.state.owner_user_id = str(data.get("owner_user_id")) if data.get("owner_user_id") is not None else None
        self.state.registered_at = time.time()
        self._persist()
        log.info("device registered", extra=kv(owner=self.state.owner_user_id))
        return data

    async def fetch_token(self) -> str:
        """POST /devices/token with device_id/secret."""
        s = await self.session()
        body = {"device_id": self.identity.device_id, "device_secret": self.identity.device_secret}
        async with s.post(f"{self.base}/devices/token", json=body) as r:
            data = await self._json_or_error(r)
        tok = data.get("device_token") if isinstance(data, dict) else None
        if not tok:
            raise CloudError(500, "token: missing device_token")
        self.state.set_token(tok)
        self._persist()
        log.info("device token refreshed", extra=kv(exp=self.state.token_exp))
        return tok

    async def token(self, force: bool = False) -> str:
        async with self._token_lock:
            if force or not self.state.token_valid():
                return await self.fetch_token()
            assert self.state.device_token
            return self.state.device_token

    async def _token_refresher(self) -> None:
        bo = Backoff(5, 300)
        while True:
            try:
                if self.state.provisioned:
                    await self.token()
                bo.reset()
                exp = self.state.token_exp
                delay = 600.0 if exp is None else max(30.0, exp - time.time() - 600.0)
                await asyncio.sleep(min(delay, 3600.0))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                d = bo.next_delay()
                log.warning("token refresh failed", extra=kv(error=str(e), retry_s=round(d, 1)))
                await asyncio.sleep(d)

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        s = await self.session()
        for attempt in (0, 1):
            tok = await self.token(force=attempt == 1)
            headers = dict(kw.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {tok}"
            async with s.request(method, f"{self.base}{path}", headers=headers, **kw) as r:
                if r.status == 401 and attempt == 0:
                    await r.release()
                    continue
                return await self._json_or_error(r)
        raise AuthError(401, "unauthorized")

    # ---------------------------------------------------------------- REST
    async def ice_servers(self) -> list[dict]:
        data = await self._request("GET", "/ice-servers")
        servers = data.get("ice_servers", []) if isinstance(data, dict) else []
        return [s for s in servers if isinstance(s, dict) and s.get("urls")]

    async def get_config(self) -> Optional[dict]:
        try:
            data = await self._request("GET", f"/devices/{self.device_id}/config")
        except CloudError as e:
            if e.status == 404:  # cloud: no desired config stored yet
                return None
            raise
        return data if isinstance(data, dict) and data else None

    async def post_config_reported(self, cfg: dict) -> None:
        await self._request("POST", f"/devices/{self.device_id}/config/reported", json=cfg)

    async def post_event(self, event: dict) -> None:
        await self._request("POST", f"/devices/{self.device_id}/events", json=event)

    async def upload_clip(self, path: Path, meta: dict) -> Optional[str]:
        # aiohttp FormData is consumed on send; rebuild per attempt via a factory.
        s = await self.session()
        for attempt in (0, 1):
            tok = await self.token(force=attempt == 1)
            with open(path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("file", f, filename=Path(path).name, content_type="video/mp4")
                form.add_field("meta", json.dumps(meta), content_type="application/json")
                async with s.post(
                    f"{self.base}/devices/{self.device_id}/clips",
                    data=form,
                    headers={"Authorization": f"Bearer {tok}"},
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as r:
                    if r.status == 401 and attempt == 0:
                        await r.release()
                        continue
                    data = await self._json_or_error(r)
                    return data.get("clip_id") if isinstance(data, dict) else None
        return None

    # ---------------------------------------------------------------- events
    def enqueue_event(self, event: dict) -> None:
        """Non-blocking; drops the oldest event when the queue is full."""
        if "ts" not in event:
            event = {**event, "ts": time.time()}
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._events.put_nowait(event)

    async def _event_pump(self) -> None:
        bo = Backoff(2, 120)
        while True:
            ev = await self._events.get()
            while True:
                if not self.state.provisioned:
                    await asyncio.sleep(10)
                    continue
                try:
                    await self.post_event(ev)
                    bo.reset()
                    break
                except asyncio.CancelledError:
                    raise
                except CloudError as e:
                    if 400 <= e.status < 500 and e.status not in (401, 408, 429):
                        log.warning("event rejected by cloud, dropping", extra=kv(t=ev.get("t"), error=str(e)))
                        break
                    d = bo.next_delay()
                except Exception as e:  # noqa: BLE001
                    d = bo.next_delay()
                    log.debug("event post failed", extra=kv(error=str(e)))
                await asyncio.sleep(d)
