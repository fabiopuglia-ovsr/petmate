"""Provisioning state machine + bless GATT server + window management (§7).

:class:`Provisioner` is transport-agnostic (pure asyncio) and unit-tested:
``device_info()`` / ``wifi_scan()`` / ``on_write(chunk)`` / ``status`` with an
``on_status`` callback for notifications. :class:`BleGattServer` maps it onto
BlueZ via ``bless``. :class:`ProvisioningWindow` decides when BLE is on:
* always while the device is not provisioned (no ``cloud.json`` owner);
* for ``window_s`` (10 min) after a ``button_provision`` event from ROS
  (MCU EVENT code 7 forwarded by petmate_bridge as ``{"t":"button_provision"}``).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional

from .. import MODEL, __version__
from ..log import get_logger, kv
from . import CHAR_DEVICE_INFO, CHAR_PAYLOAD, CHAR_STATUS, CHAR_WIFI_SCAN, SERVICE_UUID
from .crypto import ChunkReassembler, DeviceKeypair, ProvisioningCryptoError, ProvisioningPayload

log = get_logger(__name__)

STATES = ("idle", "receiving", "connecting", "wifi_ok", "registering", "done", "error")

# Error codes in the status characteristic ({"state":"error","error":<code>}).
# Wi-Fi codes come from pmcore.ble.wifi (wifi_auth_failed, wifi_not_found,
# wifi_timeout, wifi_unavailable, wifi_failed).
ERR_PAYLOAD = "payload_invalid"       # bad chunks / JSON / fields (e.g. http cloud_url not allowed)
ERR_DECRYPT = "decrypt"               # X25519/ChaCha20 failure (stale pubkey, wrong device_id)
ERR_CLOUD_UNREACHABLE = "cloud_unreachable"  # DNS/TCP/TLS/timeout, or 5xx/429 after retries
ERR_CLAIM_INVALID = "claim_invalid"   # 400/404/409/410: claim token unknown, used or expired
ERR_DEVICE_REJECTED = "device_rejected"  # 401/403: device_id/secret refused (no TOFU, wrong secret)
ERR_REGISTER = "register_failed"      # anything else


def register_error_code(e: BaseException) -> str:
    status = getattr(e, "status", None)
    if isinstance(status, int):
        if status in (400, 404, 409, 410, 422):
            return ERR_CLAIM_INVALID
        if status in (401, 403):
            return ERR_DEVICE_REJECTED
        if status in (408, 429) or status >= 500:
            return ERR_CLOUD_UNREACHABLE
        return ERR_REGISTER
    if isinstance(e, (OSError, asyncio.TimeoutError, ConnectionError)):
        return ERR_CLOUD_UNREACHABLE
    try:
        import aiohttp

        if isinstance(e, aiohttp.ClientError):
            return ERR_CLOUD_UNREACHABLE
    except ImportError:  # pragma: no cover
        pass
    return ERR_REGISTER

WifiConnect = Callable[[str, str], Awaitable[Optional[str]]]       # (ssid, password) -> ip|None
WifiScan = Callable[[], Awaitable[list[dict]]]
Register = Callable[[str, Optional[str]], Awaitable[Any]]          # (claim_token, cloud_url)
OnDone = Callable[[ProvisioningPayload], Awaitable[None] | None]


class Provisioner:
    def __init__(
        self,
        device_id: str,
        wifi_connect: WifiConnect,
        wifi_scan: WifiScan,
        register: Register,
        on_done: Optional[OnDone] = None,
        fw: str = __version__,
        model: str = MODEL,
        allow_http: bool = False,
        register_attempts: int = 4,
        register_retry_s: float = 2.0,
    ):
        self.device_id = device_id
        self.fw = fw
        self.model = model
        self._wifi_connect = wifi_connect
        self._wifi_scan = wifi_scan
        self._register = register
        self._on_done = on_done
        self.allow_http = allow_http
        self.register_attempts = max(1, register_attempts)
        self.register_retry_s = register_retry_s
        self.keys = DeviceKeypair()  # fresh X25519 per window
        self.reasm = ChunkReassembler()
        self.status: dict[str, Any] = {"state": "idle", "error": None, "ip": None}
        self.on_status: Optional[Callable[[dict], None]] = None
        self._scan_cache: list[dict] = []
        self._scan_ts = 0.0
        self._job: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ reads
    def device_info(self) -> dict:
        return {"device_id": self.device_id, "model": self.model, "fw": self.fw, "pubkey": self.keys.public_b64}

    def device_info_bytes(self) -> bytes:
        return json.dumps(self.device_info(), separators=(",", ":")).encode()

    def wifi_scan_bytes(self) -> bytes:
        """Must be synchronous for the GATT read path: returns cached list and
        refreshes it in the background when stale."""
        if time.monotonic() - self._scan_ts > 10:
            self._scan_ts = time.monotonic()
            asyncio.ensure_future(self.refresh_scan())
        return json.dumps(self._scan_cache[:15], separators=(",", ":"), ensure_ascii=False).encode()

    async def refresh_scan(self) -> list[dict]:
        try:
            self._scan_cache = (await self._wifi_scan())[:15]
            self._scan_ts = time.monotonic()
        except Exception as e:  # noqa: BLE001
            log.warning("wifi scan failed", extra=kv(error=str(e)))
        return self._scan_cache

    def status_bytes(self) -> bytes:
        return json.dumps(self.status, separators=(",", ":")).encode()

    # ------------------------------------------------------------ state
    def _set(self, state: str, error: Optional[str] = None, ip: Optional[str] = None) -> None:
        assert state in STATES
        self.status = {"state": state, "error": error, "ip": ip if ip is not None else self.status.get("ip")}
        if state in ("idle", "receiving", "connecting"):
            self.status["ip"] = ip
        log.info("provisioning status", extra=kv(**self.status))
        if self.on_status:
            try:
                self.on_status(dict(self.status))
            except Exception:  # noqa: BLE001
                log.exception("status notify failed")

    @property
    def busy(self) -> bool:
        return self._job is not None and not self._job.done()

    # ------------------------------------------------------------ writes
    def on_write(self, chunk: bytes) -> None:
        """GATT write handler (sync). Starts the async job on the last chunk."""
        if self.busy:
            log.warning("provisioning write ignored: job in progress")
            return
        try:
            first = bool(chunk[:1]) and bool(chunk[0] & 0x01)
            msg = self.reasm.feed(bytes(chunk))
        except ProvisioningCryptoError as e:
            log.warning("provisioning chunk rejected", extra=kv(error=str(e)))
            self.reasm.reset()
            self._set("error", ERR_PAYLOAD)
            return
        if msg is None:
            if first:
                self._set("receiving")
            return
        self._job = asyncio.ensure_future(self._run(msg))

    async def _run(self, message: bytes) -> None:
        """decrypt → Wi-Fi → register. Any error leaves the window open: the app
        can simply send a new payload (state goes back to ``receiving``)."""
        try:
            obj = self.keys.decrypt_message(message, self.device_id)
        except ProvisioningCryptoError as e:
            log.warning("provisioning decrypt failed", extra=kv(error=str(e)))
            self._set("error", ERR_DECRYPT)
            return
        try:
            payload = ProvisioningPayload.parse(obj, allow_http=self.allow_http)
        except ProvisioningCryptoError as e:
            log.warning("provisioning payload invalid", extra=kv(error=str(e)))
            self._set("error", ERR_PAYLOAD)
            return
        self._set("connecting")
        try:
            ip = await self._wifi_connect(payload.ssid, payload.password)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None) or "wifi_failed"
            log.error("wifi connect failed", extra=kv(code=code, error=str(e)))
            self._set("error", code)
            return
        if not ip:
            self._set("error", "wifi_failed")
            return
        self._set("wifi_ok", ip=ip)
        self._set("registering", ip=ip)
        delay = self.register_retry_s
        for attempt in range(1, self.register_attempts + 1):
            try:
                await self._register(payload.claim_token, payload.cloud_url)
                break
            except Exception as e:  # noqa: BLE001
                code = register_error_code(e)
                log.error("register failed", extra=kv(error=str(e), status=getattr(e, "status", None),
                                                      code=code, attempt=attempt))
                # Right after association DNS/routes may still settle: retry network
                # errors (the claim is idempotent for the same device on the cloud).
                if code == ERR_CLOUD_UNREACHABLE and attempt < self.register_attempts:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                self._set("error", code, ip=ip)
                return
        self._set("done", ip=ip)
        if self._on_done:
            r = self._on_done(payload)
            if asyncio.iscoroutine(r):
                await r

    async def wait(self) -> None:
        if self._job:
            await self._job


_OFFSET_PATCHED = False


def _offset_slice(value: bytes, options: Any) -> bytes:
    off = 0
    if isinstance(options, dict) and "offset" in options:
        o = options["offset"]
        off = int(getattr(o, "value", o) or 0)
    return bytes(value)[off:] if off > 0 else bytes(value)


def patch_bless_long_reads() -> bool:
    """bless' BlueZ ReadValue ignores ``options["offset"]``: for values longer
    than ATT_MTU-1 the central issues Read Blob requests and would receive the
    head of the value again. Wrap the dbus method to honour the offset."""
    global _OFFSET_PATCHED
    if _OFFSET_PATCHED:
        return True
    try:
        from bless.backends.bluezdbus.dbus.characteristic import BlueZGattCharacteristic
    except ImportError:
        return False
    m = BlueZGattCharacteristic.ReadValue.__dict__.get("__DBUS_METHOD")
    if m is None:
        log.warning("bless internals changed; long reads not patched")
        return False
    orig = m.fn

    def read_with_offset(self: Any, options: Any) -> bytes:  # dbus_next calls fn(interface, *args)
        return _offset_slice(orig(self, options), options)

    m.fn = read_with_offset
    _OFFSET_PATCHED = True
    return True


class BleGattServer:
    """bless-based GATT server exposing a :class:`Provisioner`.

    Values are re-generated on every read; with offset patching a long read
    (Read Blob) sees a consistent value as long as it doesn't change between
    blobs (wifi_scan is served from a cache refreshed at most every 10 s).
    """

    def __init__(self, prov: Provisioner, name: str, adapter: Optional[str] = None):
        self.prov = prov
        self.name = name
        self.adapter = adapter
        self.server = None
        self.loop = asyncio.get_event_loop()

    async def start(self) -> None:
        from bless import (  # pip: bless (BlueZ via dbus-next)
            BlessServer,
            GATTAttributePermissions,
            GATTCharacteristicProperties,
        )

        patch_bless_long_reads()
        kw = {"adapter": self.adapter} if self.adapter else {}
        self.server = BlessServer(name=self.name, loop=self.loop, **kw)
        self.server.read_request_func = self._read
        self.server.write_request_func = self._write
        P, A = GATTCharacteristicProperties, GATTAttributePermissions
        await self.server.add_new_service(SERVICE_UUID)
        await self.server.add_new_characteristic(SERVICE_UUID, CHAR_DEVICE_INFO, P.read, None, A.readable)
        await self.server.add_new_characteristic(SERVICE_UUID, CHAR_WIFI_SCAN, P.read, None, A.readable)
        await self.server.add_new_characteristic(
            SERVICE_UUID, CHAR_PAYLOAD, P.write | P.write_without_response, None, A.writeable
        )
        await self.server.add_new_characteristic(SERVICE_UUID, CHAR_STATUS, P.read | P.notify, None, A.readable)
        self.prov.on_status = self._notify
        await self.prov.refresh_scan()  # before advertising (scan while not yet connected)
        self._set_value(CHAR_DEVICE_INFO, self.prov.device_info_bytes())
        self._set_value(CHAR_STATUS, self.prov.status_bytes())
        await self.server.start()
        log.info("BLE advertising", extra=kv(name=self.name))

    async def stop(self) -> None:
        if self.server is not None:
            try:
                await self.server.stop()  # unregisters advertisement + GATT application
                log.info("BLE advertising stopped", extra=kv(name=self.name))
            except Exception as e:  # noqa: BLE001
                log.warning("BLE stop error", extra=kv(error=str(e)))
            bus = getattr(self.server, "bus", None)
            if bus is not None:
                try:
                    bus.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self.server = None

    def _set_value(self, uuid: str, value: bytes) -> None:
        assert self.server is not None
        ch = self.server.get_characteristic(uuid)
        if ch is not None:
            ch.value = bytearray(value)

    def _read(self, characteristic: Any, **_: Any) -> bytearray:
        u = str(characteristic.uuid).lower()
        if u == CHAR_DEVICE_INFO:
            v = self.prov.device_info_bytes()
        elif u == CHAR_WIFI_SCAN:
            v = self.prov.wifi_scan_bytes()
        elif u == CHAR_STATUS:
            v = self.prov.status_bytes()
        else:
            v = bytes(characteristic.value or b"")
        # Note: BlueZ handles long reads (offset) on top of the full value.
        return bytearray(v)

    def _write(self, characteristic: Any, value: Any, **_: Any) -> None:
        if str(characteristic.uuid).lower() == CHAR_PAYLOAD:
            # called from dbus-next's handler on the loop thread
            self.prov.on_write(bytes(value))

    def _notify(self, status: dict) -> None:
        if self.server is None:
            return
        self._set_value(CHAR_STATUS, json.dumps(status, separators=(",", ":")).encode())
        self.server.update_value(SERVICE_UUID, CHAR_STATUS)


class ProvisioningWindow:
    """Opens/closes the BLE GATT server according to §7 rules."""

    def __init__(
        self,
        make_provisioner: Callable[[], Provisioner],
        ble_name: str,
        is_provisioned: Callable[[], bool],
        window_s: float = 600.0,
        adapter: Optional[str] = None,
        server_factory: Optional[Callable[[Provisioner, str], Any]] = None,
        retry_s: float = 5.0,
    ):
        self._make = make_provisioner
        self.ble_name = ble_name
        self.is_provisioned = is_provisioned
        self.window_s = window_s
        self.adapter = adapter
        self._factory = server_factory or (lambda p, n: BleGattServer(p, n, adapter))
        self.server: Any = None
        self.prov: Optional[Provisioner] = None
        self.deadline: Optional[float] = None
        self._timer: Optional[asyncio.Task] = None
        self._retry: Optional[asyncio.Task] = None
        self.retry_s = retry_s
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self.server is not None

    async def start_if_needed(self) -> None:
        if not self.is_provisioned():
            if not await self.open(None) and self._retry is None:
                self._retry = asyncio.ensure_future(self._retry_open())

    async def _retry_open(self) -> None:
        try:
            while self.server is None and not self.is_provisioned():
                await asyncio.sleep(self.retry_s)
                if await self.open(None):
                    return
        finally:
            self._retry = None

    async def open(self, duration_s: Optional[float]) -> bool:
        async with self._lock:
            if self.server is not None:
                if self.prov and self.prov.busy:
                    return True
                await self._stop_locked()  # restart with a fresh keypair
            self.prov = self._make()
            srv = self._factory(self.prov, self.ble_name)
            try:
                await srv.start()
            except Exception as e:  # noqa: BLE001
                log.error("BLE GATT start failed (retrying while unprovisioned)", extra=kv(error=str(e)))
                try:
                    await srv.stop()
                except Exception:  # noqa: BLE001
                    pass
                return False
            self.server = srv
            self.deadline = time.monotonic() + duration_s if duration_s else None
            if self._timer:
                self._timer.cancel()
            if duration_s:
                self._timer = asyncio.ensure_future(self._expire(duration_s))
            log.info("provisioning window open", extra=kv(duration_s=duration_s, name=self.ble_name))
            return True

    async def _expire(self, d: float) -> None:
        await asyncio.sleep(d)
        while self.prov and self.prov.busy:
            await asyncio.sleep(1)
        if self.is_provisioned():
            await self.close("timeout")
        else:
            log.info("window expired but still unprovisioned; keeping BLE on")
            self.deadline = None

    async def on_button_provision(self) -> None:
        await self.open(self.window_s)

    async def finished(self) -> None:
        # allow the final "done" notify to reach the app
        await asyncio.sleep(2.0)
        await self.close("done")

    async def close(self, reason: str) -> None:
        if self._retry is not None and self._retry is not asyncio.current_task():
            self._retry.cancel()
            self._retry = None
        async with self._lock:
            await self._stop_locked()
        log.info("provisioning window closed", extra=kv(reason=reason))

    async def _stop_locked(self) -> None:
        if self._timer and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None
        if self.server is not None:
            await self.server.stop()
        self.server = None
        self.deadline = None
