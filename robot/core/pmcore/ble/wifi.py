"""NetworkManager (nmcli) helpers for Wi-Fi scan/connect (BLE provisioning, §7).

Failures raise :class:`WifiError` with a stable ``code`` that ends up in the
provisioning status characteristic (``{"state":"error","error":<code>}``):

* ``wifi_auth_failed`` — wrong password / 4-way handshake / SAE failure;
* ``wifi_not_found``   — SSID not in range (also hidden SSIDs that never answer);
* ``wifi_timeout``     — association/DHCP did not complete in time;
* ``wifi_unavailable`` — Wi-Fi radio off / rfkill / device unmanaged (on a Pi this
  is typically an unset Wi-Fi country: ``raspi-config nonint do_wifi_country IT``);
* ``wifi_failed``      — anything else (see logs).

A :class:`FakeWifi` backend (settings ``ble.wifi = "fake"``) is provided for
tests and end-to-end runs without touching the host network; it simulates the
error codes with magic values (see the class docstring).
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from ..log import get_logger, kv
from ..util import run_cmd

log = get_logger(__name__)

CON_NAME = "petmate-wifi"
CON_NAME_NEW = "petmate-wifi-new"

WIFI_AUTH_FAILED = "wifi_auth_failed"
WIFI_NOT_FOUND = "wifi_not_found"
WIFI_TIMEOUT = "wifi_timeout"
WIFI_UNAVAILABLE = "wifi_unavailable"
WIFI_FAILED = "wifi_failed"


class WifiError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


_AUTH_MARKERS = (
    "secrets were required", "no secrets", "802-11-wireless-security", "wifi-sec.psk",
    "supplicant", "authentication", "4-way", "handshake", "wrong password",
)
_NOT_FOUND_MARKERS = ("could not be found", "no network with ssid", "ssid not found", "ssid-not-found")
_UNAVAILABLE_MARKERS = (
    "not available because", "radio", "rfkill", "unmanaged", "no suitable device",
    "device not found", "unavailable",
)
_TIMEOUT_MARKERS = ("timeout expired", "timed out", "timeout")


def classify_nmcli_error(rc: int, err: str) -> str:
    """Map an ``nmcli`` failure (exit code + stderr) to a provisioning error code."""
    e = (err or "").lower()
    if any(m in e for m in _NOT_FOUND_MARKERS):
        return WIFI_NOT_FOUND
    if any(m in e for m in _AUTH_MARKERS):
        return WIFI_AUTH_FAILED
    if rc == 124 or any(m in e for m in _TIMEOUT_MARKERS):
        return WIFI_TIMEOUT
    if any(m in e for m in _UNAVAILABLE_MARKERS):
        return WIFI_UNAVAILABLE
    return WIFI_FAILED


def split_terse(line: str) -> list[str]:
    """Split an ``nmcli -t`` line on unescaped ':' and unescape ``\\:`` / ``\\\\``."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def signal_to_rssi(signal_pct: int) -> int:
    """NetworkManager reports 0..100 %; it derives it as 2*(dBm+100). Invert."""
    s = max(0, min(100, signal_pct))
    return int(round(s / 2.0 - 100))


# SSID → nmcli SECURITY string of the last scan (used to pick key-mgmt and to
# decide whether the requested SSID is hidden).
_last_security: dict[str, str] = {}


def parse_scan(output: str, limit: int = 15, security: Optional[dict[str, str]] = None) -> list[dict]:
    """Parse ``nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list`` → §7 list, dedup by SSID."""
    best: dict[str, dict] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        f = split_terse(line)
        if len(f) < 3:
            continue
        ssid, sig, sec = f[0], f[1], f[2]
        if not ssid:
            continue  # hidden
        try:
            rssi = signal_to_rssi(int(sig))
        except ValueError:
            continue
        secure = bool(sec.strip()) and sec.strip() != "--"
        if security is not None:
            security[ssid] = sec.strip()
        cur = best.get(ssid)
        if cur is None or rssi > cur["rssi"]:
            best[ssid] = {"ssid": ssid, "rssi": rssi, "secure": secure}
    return sorted(best.values(), key=lambda e: -e["rssi"])[:limit]


def key_mgmt_for(security: Optional[str]) -> str:
    """WPA3-only (SAE) networks need key-mgmt ``sae``; WPA2 and WPA2/WPA3 transition use ``wpa-psk``."""
    s = (security or "").upper()
    if "WPA3" in s and "WPA2" not in s and "WPA1" not in s:
        return "sae"
    return "wpa-psk"


async def scan(iface: str = "wlan0", limit: int = 15, rescan: str = "yes") -> list[dict]:
    rc, out, err = await run_cmd(
        "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", iface, "--rescan", rescan,
        timeout=20,
    )
    if rc != 0:
        log.warning("wifi scan failed", extra=kv(rc=rc, err=err.strip(), hint=_hint(classify_nmcli_error(rc, err))))
        return []
    sec: dict[str, str] = {}
    res = parse_scan(out, limit, sec)
    _last_security.clear()
    _last_security.update(sec)
    return res


def _hint(code: str) -> Optional[str]:
    if code == WIFI_UNAVAILABLE:
        return "Wi-Fi radio unavailable: check `rfkill list`, `nmcli radio wifi`, and set the Wi-Fi country"
    return None


async def connect(ssid: str, password: str, iface: str = "wlan0", timeout_s: float = 45.0,
                  hidden: Optional[bool] = None) -> str:
    """Create a dedicated NM connection and bring it up. Returns the IPv4 address.

    * A temporary profile ``petmate-wifi-new`` is activated; only on success it
      replaces ``petmate-wifi`` (so a wrong password never destroys a working
      configuration, and a failed profile never lingers with autoconnect).
    * ``nmcli connection add`` instead of ``dev wifi connect`` also works for a
      hidden/not-yet-scanned SSID (``802-11-wireless.hidden yes`` is set when the
      SSID was not in the last scan) and lets us pick ``sae`` for WPA3-only.
    * Runs while the BLE GATT server stays up: on the Pi's combo chip Wi-Fi
      association can briefly degrade BLE; the app re-reads the status
      characteristic after a reconnect, so the final state is never lost.

    Raises :class:`WifiError` (see module docstring for codes).
    """
    await run_cmd("nmcli", "radio", "wifi", "on", timeout=10)
    if hidden is None:
        hidden = bool(_last_security) and ssid not in _last_security
    await run_cmd("nmcli", "connection", "delete", CON_NAME_NEW, timeout=10)
    args = [
        "nmcli", "connection", "add", "type", "wifi", "ifname", iface, "con-name", CON_NAME_NEW,
        "ssid", ssid, "connection.autoconnect", "yes", "connection.autoconnect-priority", "10",
    ]
    if hidden:
        args += ["802-11-wireless.hidden", "yes"]
    if password:
        args += ["wifi-sec.key-mgmt", key_mgmt_for(_last_security.get(ssid)), "wifi-sec.psk", password]
    rc, _, err = await run_cmd(*args, timeout=15)
    if rc != 0:
        code = classify_nmcli_error(rc, err)
        log.error("nmcli add failed", extra=kv(err=err.strip(), code=code))
        raise WifiError(code, err.strip())
    log.info("wifi connecting", extra=kv(ssid=ssid, hidden=hidden, secure=bool(password)))
    rc, _, err = await run_cmd("nmcli", "--wait", str(int(timeout_s)), "connection", "up", CON_NAME_NEW,
                               timeout=timeout_s + 5)
    if rc != 0:
        code = classify_nmcli_error(rc, err)
        log.error("nmcli up failed", extra=kv(err=err.strip(), rc=rc, code=code, hint=_hint(code)))
        await run_cmd("nmcli", "connection", "delete", CON_NAME_NEW, timeout=10)
        raise WifiError(code, err.strip())
    # success: the new profile becomes the canonical one
    await run_cmd("nmcli", "connection", "delete", CON_NAME, timeout=10)
    await run_cmd("nmcli", "connection", "modify", CON_NAME_NEW, "connection.id", CON_NAME, timeout=10)
    for _ in range(20):
        ip = await ipv4(iface)
        if ip:
            return ip
        await asyncio.sleep(0.5)
    raise WifiError(WIFI_TIMEOUT, "connected but no IPv4 address (DHCP)")


async def ipv4(iface: str = "wlan0") -> Optional[str]:
    rc, out, _ = await run_cmd("nmcli", "-t", "-g", "IP4.ADDRESS", "device", "show", iface, timeout=5)
    if rc != 0:
        return None
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


async def wait_online(timeout_s: float = 30.0) -> bool:
    rc, _, _ = await run_cmd("nm-online", "-q", "-t", str(int(timeout_s)), timeout=timeout_s + 5)
    return rc == 0


class FakeWifi:
    """No-op Wi-Fi for tests/E2E (settings ``ble.wifi = "fake"``).

    ``connect`` succeeds with ``ip`` except for these magic inputs, which
    exercise the app's error UI:
    * password ``"wrongpassword"``      → ``wifi_auth_failed``
    * SSID ``"PetmateTest-NotFound"``   → ``wifi_not_found``
    * SSID ``"PetmateTest-Timeout"``    → ``wifi_timeout``
    """

    def __init__(self, networks: Optional[list[dict]] = None, ip: str = "127.0.0.1", delay_s: float = 0.2):
        self.networks = networks if networks is not None else [
            {"ssid": "PetmateTest", "rssi": -45, "secure": True},
            {"ssid": "PetmateTest-Open", "rssi": -60, "secure": False},
        ]
        self.ip = ip
        self.delay_s = delay_s
        self.connects: list[tuple[str, str]] = []

    async def scan(self) -> list[dict]:
        return list(self.networks)

    async def connect(self, ssid: str, password: str) -> str:
        self.connects.append((ssid, password))
        await asyncio.sleep(self.delay_s)
        if password == "wrongpassword":
            raise WifiError(WIFI_AUTH_FAILED, "fake")
        if ssid == "PetmateTest-NotFound":
            raise WifiError(WIFI_NOT_FOUND, "fake")
        if ssid == "PetmateTest-Timeout":
            raise WifiError(WIFI_TIMEOUT, "fake")
        return self.ip
