"""`GET /v1/server-info` — unauthenticated, harmless: tells the test console (and the app)
which base URL the robot should use as `cloud_url` in BLE provisioning (§7).

With `PUBLIC_BASE_URL` set (real deployment) only that URL is returned and no LAN
address is disclosed; otherwise the detected LAN addresses of the dev machine.
"""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Request

from .. import netinfo

router = APIRouter()


def _is_local(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@router.get("/server-info")
def server_info(request: Request):
    s = request.app.state.settings
    prefix = "/v1"
    if s.public_base_url:
        bases = [s.public_base_url]
        hosts: list[str] = []
    else:
        hosts = netinfo.lan_hosts()
        port = request.url.port or s.port
        bases = netinfo.base_urls(port, hosts)
        # the address the caller used, if it is reachable from elsewhere (not localhost)
        req_host = request.url.hostname or ""
        here = str(request.base_url).rstrip("/")
        if req_host and not _is_local(req_host) and here not in bases:
            bases.insert(0, here)
    return {
        "service": "petmate-cloud",
        "version": request.app.version,
        "api_prefix": prefix,
        "lan_hosts": hosts,
        "base_urls": [b + prefix for b in bases],
        "console_urls": [b + "/console/" for b in bases],
    }
