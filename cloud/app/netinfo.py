"""LAN address detection for the developer setup (MVP: cloud on a laptop in the home LAN).

Used by `GET /v1/server-info` (prefill of `cloud_url` in the BLE wizard) and by
`run_dev.sh` / the startup log to print the URLs the app and the Raspberry Pi must use.
Standard library only; every probe is best effort.

`LAN_HOSTS` (comma separated IPs or names) overrides/extends detection — needed inside
Docker, where the container cannot see the host's LAN address.

    python -m app.netinfo [port]     # prints the banner
"""
from __future__ import annotations

import ipaddress
import os
import platform
import re
import socket
import subprocess
import sys

# virtual interfaces that are never the address the Pi/phone should use
_VIRTUAL_IF = re.compile(r"^(lo|docker|br-|veth|virbr|vmnet|vboxnet|utun|awdl|llw|bridge|tailscale|zt|cni|flannel|kube)")


def _usable(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and not (a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast)


def _route_ip() -> str | None:
    """Source address of the default route (UDP connect sends no packet)."""
    for target in ("192.0.2.1", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 9))
            ip = s.getsockname()[0]
            if _usable(ip):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    return None


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _interface_ips() -> list[str]:
    """(Linux) `ip -4 -o addr`, (macOS/BSD) `ifconfig`; skips virtual interfaces."""
    out: list[str] = []
    system = platform.system()
    if system == "Linux":
        for line in _run(["ip", "-4", "-o", "addr", "show"]).splitlines():
            m = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)/", line)
            if m and not _VIRTUAL_IF.match(m.group(1)):
                out.append(m.group(2))
    elif system == "Darwin" or "BSD" in system:
        iface = ""
        for line in _run(["ifconfig"]).splitlines():
            if line and not line[0].isspace():
                iface = line.split(":", 1)[0]
                continue
            m = re.match(r"\s+inet\s+([\d.]+)", line)
            if m and not _VIRTUAL_IF.match(iface):
                out.append(m.group(1))
    return out


def _hostname_ips() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return [i[4][0] for i in infos]


def lan_hosts() -> list[str]:
    """Candidate addresses, best first: LAN_HOSTS env, default-route IP, interfaces, hostname."""
    explicit = [h.strip() for h in os.environ.get("LAN_HOSTS", "").split(",") if h.strip()]
    detected: list[str] = []
    for ip in [_route_ip(), *_interface_ips(), *_hostname_ips()]:
        if ip and _usable(ip) and ip not in detected and ip not in explicit:
            detected.append(ip)
    # private (RFC 1918) addresses first; stable sort keeps the default-route IP ahead
    detected.sort(key=lambda ip: 0 if ipaddress.ip_address(ip).is_private else 1)
    return explicit + detected


def base_urls(port: int, hosts: list[str] | None = None) -> list[str]:
    return [f"http://{h}:{port}" for h in (hosts if hosts is not None else lan_hosts())]


def banner(port: int) -> str:
    urls = base_urls(port)
    lines = ["", "=" * 64, " Petmate cloud (dev) — HTTP porta %d" % port, "=" * 64,
             " Console di test (sul PC, Chrome/Edge, Web Bluetooth):",
             f"   http://localhost:{port}/console/"]
    if urls:
        lines += [" API per l'app Flutter e cloud_url per la Raspberry Pi:"]
        lines += [f"   {u}/v1" for u in urls]
        lines += [" Console da un altro PC della LAN (senza Bluetooth):"]
        lines += [f"   {u}/console/" for u in urls]
    else:
        lines += [" Nessun indirizzo LAN rilevato: imposta LAN_HOSTS=<ip del PC>."]
    lines += ["=" * 64, ""]
    return "\n".join(lines)


if __name__ == "__main__":
    print(banner(int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))))
