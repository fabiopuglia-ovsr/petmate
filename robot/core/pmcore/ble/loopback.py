"""In-process "BLE" transport for tests and end-to-end runs without radios.

:class:`LoopbackGattServer` is a drop-in for :class:`~pmcore.ble.provisioning.BleGattServer`
(``ProvisioningWindow`` server factory; settings ``ble.transport = "loopback"``)
and at the same time implements the central-side link used by
``robot/tools/provision_cli.py`` (``read`` / ``write`` / ``subscribe``). An E2E
test can therefore do::

    app = App(paths, Settings.load(overrides={"profile": "mvp",
              "ble": {"transport": "loopback", "wifi": "fake"}, ...}))
    await app.start()
    link = app.ble.server                       # LoopbackGattServer
    await provision_cli.provision(link, ssid=..., password=..., claim_token=..., cloud_url=...)

Writes are delivered exactly like BlueZ does (one GATT write = one call of
``Provisioner.on_write``) and status notifications are delivered via
``subscribe`` callbacks.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from ..log import get_logger, kv
from . import CHAR_DEVICE_INFO, CHAR_PAYLOAD, CHAR_STATUS, CHAR_WIFI_SCAN
from .crypto import CHUNK_MAX

log = get_logger(__name__)

# The same server objects are reachable by name for tests that cannot get hold
# of the App instance (most recent server per advertised name).
REGISTRY: dict[str, "LoopbackGattServer"] = {}


class LoopbackGattServer:
    def __init__(self, prov: Any, name: str, mtu: int = 185):
        self.prov = prov
        self.name = name
        self.mtu = mtu
        self.advertising = False
        self._subs: dict[str, list[Callable[[bytes], Any]]] = {}

    # ------------------------------------------------------- server side
    async def start(self) -> None:
        self.prov.on_status = self._notify
        await self.prov.refresh_scan()
        self.advertising = True
        REGISTRY[self.name] = self
        log.info("loopback BLE 'advertising'", extra=kv(name=self.name))

    async def stop(self) -> None:
        self.advertising = False
        if REGISTRY.get(self.name) is self:
            del REGISTRY[self.name]
        log.info("loopback BLE stopped", extra=kv(name=self.name))

    def _notify(self, status: dict) -> None:
        import json

        data = json.dumps(status, separators=(",", ":")).encode()
        for cb in list(self._subs.get(CHAR_STATUS, [])):
            try:
                r = cb(data)
                if asyncio.iscoroutine(r):
                    asyncio.ensure_future(r)
            except Exception:  # noqa: BLE001
                log.exception("loopback notify callback failed")

    # ------------------------------------------------------- central side (link)
    def _check(self) -> None:
        if not self.advertising:
            raise ConnectionError("BLE server not advertising (window closed)")

    async def read(self, uuid: str) -> bytes:
        self._check()
        u = uuid.lower()
        if u == CHAR_DEVICE_INFO:
            return self.prov.device_info_bytes()
        if u == CHAR_WIFI_SCAN:
            return self.prov.wifi_scan_bytes()
        if u == CHAR_STATUS:
            return self.prov.status_bytes()
        raise KeyError(uuid)

    async def write(self, uuid: str, data: bytes) -> None:
        self._check()
        if uuid.lower() != CHAR_PAYLOAD:
            raise KeyError(uuid)
        if len(data) > min(CHUNK_MAX, self.mtu - 3):
            raise ValueError(f"write of {len(data)} B exceeds ATT MTU/§7 chunk limit")
        self.prov.on_write(bytes(data))
        await asyncio.sleep(0)

    async def subscribe(self, uuid: str, callback: Callable[[bytes], Any]) -> None:
        self._check()
        self._subs.setdefault(uuid.lower(), []).append(callback)

    @property
    def max_write(self) -> int:
        return min(CHUNK_MAX, self.mtu - 3)


def get(name: Optional[str] = None) -> Optional[LoopbackGattServer]:
    if name is not None:
        return REGISTRY.get(name)
    return next(iter(REGISTRY.values()), None)
