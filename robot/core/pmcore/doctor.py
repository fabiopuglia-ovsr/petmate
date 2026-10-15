"""``petmate-doctor``: diagnostica MVP sul robot (Raspberry Pi 5).

Controlli: identità, core.json/profilo, camera (rilevamento + cattura di 3 s),
velocità encoder x264 alla risoluzione configurata, Bluetooth/BlueZ,
NetworkManager + paese Wi-Fi, raggiungibilità del cloud (da cloud.json),
stato del servizio. Stampa un elenco PASS/WARN/FAIL in italiano; exit code 1
se almeno un FAIL.

    sudo /opt/petmate/robot/scripts/petmate_doctor.sh            # tutto
    sudo petmate_doctor.sh --skip-camera --seconds 5              # opzioni
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Result:
    status: str
    name: str
    detail: str = ""
    hint: str = ""


def sh(*args: str, timeout: float = 10.0) -> tuple[int, str]:
    if not shutil.which(args[0]):
        return 127, f"{args[0]} non installato"
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"


class Doctor:
    def __init__(self, root: str = "/", seconds: float = 3.0, skip_camera: bool = False,
                 skip_system: bool = False, out: Callable[[str], None] = print):
        from pathlib import Path

        from .paths import Paths
        from .settings import Settings

        self.paths = Paths(Path(root))
        self.settings = Settings.load(self.paths.host_settings)
        self.seconds = seconds
        self.skip_camera = skip_camera
        self.skip_system = skip_system
        self.out = out
        self.results: list[Result] = []
        self.capture_info: dict = {}

    # ---------------------------------------------------------------- helpers
    def add(self, status: str, name: str, detail: str = "", hint: str = "") -> Result:
        r = Result(status, name, detail, hint)
        self.results.append(r)
        line = f"[{status}] {name}" + (f": {detail}" if detail else "")
        self.out(line)
        if hint and status in (FAIL, WARN):
            self.out(f"       -> {hint}")
        return r

    def run(self) -> int:
        self.out("Petmate doctor — diagnostica MVP\n")
        checks = [self.check_identity, self.check_settings, self.check_python]
        if not self.skip_camera:
            checks += [self.check_camera, self.check_encoder]
        else:
            checks += [self.check_encoder]
        if not self.skip_system:
            checks += [self.check_bluetooth, self.check_network, self.check_wifi_country, self.check_service]
        checks += [self.check_cloud]
        for c in checks:
            try:
                c()
            except Exception as e:  # noqa: BLE001
                self.add(FAIL, c.__name__.replace("check_", ""), f"errore interno: {e!r}")
        n = {s: sum(1 for r in self.results if r.status == s) for s in (PASS, WARN, FAIL, SKIP)}
        self.out(f"\nRisultato: {n[PASS]} PASS, {n[WARN]} WARN, {n[FAIL]} FAIL, {n[SKIP]} SKIP")
        return 1 if n[FAIL] else 0

    # ---------------------------------------------------------------- checks
    def check_identity(self) -> None:
        from .identity import Identity, IdentityError

        try:
            ident = Identity.load(self.paths.identity)
        except IdentityError as e:
            self.add(FAIL, "Identità dispositivo", str(e), "sudo /opt/petmate/robot/scripts/make_identity.sh")
            return
        mode = oct(os.stat(self.paths.identity).st_mode & 0o777)
        st = PASS if mode in ("0o600", "0o640") else WARN
        self.add(st, "Identità dispositivo", f"{ident.device_id} (BLE Petmate-{ident.short_id}, permessi {mode})",
                 "chmod 600 /etc/petmate/identity.json" if st == WARN else "")

    def check_settings(self) -> None:
        s = self.settings
        cam = s["camera"]
        detail = (f"profilo={s.profile}, camera={cam['backend']} device={cam.get('device')} "
                  f"{cam['main_size'][0]}x{cam['main_size'][1]}@{cam['fps']}")
        if s.profile != "mvp":
            self.add(WARN, "Configurazione core.json", detail,
                     f'{self.paths.host_settings}: impostare "profile": "mvp"')
        else:
            self.add(PASS, "Configurazione core.json", detail)

    def check_python(self) -> None:
        from importlib import metadata

        missing, vers = [], []
        for mod in ("av", "aiortc", "aioice", "aiohttp", "cryptography", "bless"):
            try:
                __import__(mod)
                try:
                    vers.append(f"{mod} {metadata.version(mod)}")
                except metadata.PackageNotFoundError:
                    vers.append(mod)
            except ImportError:
                missing.append(mod)
        try:
            import av

            if "libx264" not in av.codecs_available:
                missing.append("libx264 (in PyAV)")
        except ImportError:
            pass
        if missing:
            self.add(FAIL, "Dipendenze Python", "mancano: " + ", ".join(missing),
                     "sudo /opt/petmate/robot/scripts/install_mvp.sh")
        else:
            self.add(PASS, "Dipendenze Python", ", ".join(vers))

    def _camera_obj(self):
        from .camera import AvCaptureCam, CameraManager

        s = dict(self.settings["camera"])
        backend = s["backend"] if s["backend"] in ("uvc", "lavfi") else "uvc"
        mgr = CameraManager({**s, "backend": backend}, lores_rate_hz=0.0)
        return AvCaptureCam("front", mgr, backend, device=str(s.get("device", "auto")),
                            input_format=str(s.get("input_format", "mjpeg")),
                            lavfi_source=str(s.get("lavfi_source")))

    def check_camera(self) -> None:
        from .camera import v4l2_querycap

        cam = self._camera_obj()
        src = cam.resolve_source()
        if src is None:
            self.add(FAIL, "Camera rilevata", f"nessuna webcam UVC trovata (device={cam.device})",
                     "collegare la Logitech a una porta USB 3 (blu); `v4l2-ctl --list-devices`")
            self.add(SKIP, f"Cattura {self.seconds:.0f} s", "nessuna camera")
            return
        label = src
        if cam.backend == "uvc":
            q = v4l2_querycap(os.path.realpath(src))
            if q:
                label = f"{src} ({q['card']}, driver {q['driver']})"
        self.add(PASS, "Camera rilevata", label)
        name = f"Cattura {self.seconds:.0f} s"
        try:
            container, stream, mode = cam.open_source(src)
        except Exception as e:  # noqa: BLE001
            busy = getattr(e, "errno", None) == 16 or "busy" in str(e).lower()
            self.add(FAIL, name, str(e)[:200],
                     "camera occupata: c'è uno streaming attivo? riprovare a viewer chiusi o "
                     "`sudo systemctl stop petmate-core`" if busy else "verificare cavo/alimentazione USB")
            return
        frames, dec_ms, size, fmt = 0, 0.0, None, None
        t0 = time.monotonic()
        try:
            for pkt in container.demux(stream):
                if pkt.size == 0:
                    continue
                d0 = time.perf_counter()
                for vf in pkt.decode():
                    frames += 1
                    size, fmt = (vf.width, vf.height), vf.format.name
                    self.capture_info["last_frame"] = vf
                dec_ms += (time.perf_counter() - d0) * 1000
                if time.monotonic() - t0 >= self.seconds:
                    break
                if cam.backend == "lavfi":
                    time.sleep(1.0 / max(1, cam.mgr.fps))
        finally:
            container.close()
        el = time.monotonic() - t0
        fps = frames / el if el > 0 else 0.0
        self.capture_info.update(size=size, fps=fps)
        want = cam.mgr.fps
        detail = (f"{frames} frame in {el:.1f} s = {fps:.1f} fps, {size[0]}x{size[1]} "
                  f"{mode.get('input_format', stream.codec_context.name)}→{fmt}, decodifica "
                  f"{dec_ms / max(1, frames):.1f} ms/frame" if size else f"0 frame in {el:.1f} s")
        if frames == 0:
            self.add(FAIL, name, detail, "nessun frame: provare un'altra porta USB / cavo")
        elif fps < 0.6 * want:
            self.add(WARN, name, detail, "fps bassi: poca luce (esposizione automatica Logitech) o banda USB")
        else:
            self.add(PASS, name, detail)

    def check_encoder(self) -> None:
        import fractions

        import av

        from .video import TIME_BASE

        s = self.settings
        w, h = s["camera"]["main_size"]
        size = self.capture_info.get("size")
        if size and tuple(size) != (w, h):
            w, h = size  # the camera fell back to another mode: measure what will be encoded
        fps_req = int(s["camera"]["fps"])
        n = max(30, int(fps_req * 2))
        # realistic input: moving pattern in yuvj422p (MJPEG decoder output) → 1 swscale pass
        src = av.open(f"testsrc2=size={w}x{h}:rate={fps_req},format=yuvj422p", format="lavfi")
        frames = []
        for f in src.decode(video=0):
            frames.append(f)
            if len(frames) >= n:
                break
        src.close()
        cc = av.CodecContext.create("libx264", "w")
        cc.width, cc.height, cc.pix_fmt = w, h, "yuv420p"
        cc.time_base = TIME_BASE
        cc.framerate = fractions.Fraction(fps_req, 1)
        cc.bit_rate = int(s["video"]["bitrate"])
        cc.profile = "Baseline"
        cc.options = {"preset": "ultrafast", "tune": "zerolatency", "threads": str(s["video"].get("x264_threads", 3))}
        t0 = time.perf_counter()
        nbytes = 0
        for i, f in enumerate(frames):
            vf = f.reformat(width=w, height=h, format="yuv420p")
            vf.pts = i * 3000
            vf.time_base = TIME_BASE
            for p in cc.encode(vf):
                nbytes += p.size
        for p in cc.encode(None):
            nbytes += p.size
        el = time.perf_counter() - t0
        fps = len(frames) / el
        detail = f"{w}x{h}: {fps:.0f} fps ({1000 / fps:.1f} ms/frame, incl. conversione colore), richiesti {fps_req}"
        if fps >= fps_req * 1.2:
            self.add(PASS, "Encoder x264", detail)
        elif fps >= fps_req * 0.9:
            self.add(WARN, "Encoder x264", detail, "margine CPU ridotto: valutare fps 25 o 960x540 in core.json")
        else:
            self.add(FAIL, "Encoder x264", detail, "ridurre camera.main_size/fps in /etc/petmate/core.json")

    def check_bluetooth(self) -> None:
        adapter = self.settings["ble"]["adapter"]
        if not os.path.exists(f"/sys/class/bluetooth/{adapter}"):
            self.add(FAIL, "Bluetooth", f"adattatore {adapter} assente", "`sudo rfkill unblock bluetooth`; "
                     "verificare `dtoverlay=disable-bt` assente in /boot/firmware/config.txt")
            return
        rc, svc = sh("systemctl", "is-active", "bluetooth")
        rc2, show = sh("bluetoothctl", "show")
        powered = "Powered: yes" in show
        rc3, rf = sh("rfkill", "list", "bluetooth")
        blocked = "blocked: yes" in rf
        detail = f"{adapter}, bluetoothd {svc}, powered={'sì' if powered else 'no'}" + (", RFKILL BLOCCATO" if blocked else "")
        if svc != "active" or blocked:
            self.add(FAIL, "Bluetooth", detail, "sudo rfkill unblock bluetooth && sudo systemctl restart bluetooth")
        elif not powered:
            self.add(WARN, "Bluetooth", detail, "bluetoothctl power on (AutoEnable=true in /etc/bluetooth/main.conf)")
        else:
            self.add(PASS, "Bluetooth", detail)

    def check_network(self) -> None:
        rc, out = sh("nmcli", "-t", "-f", "RUNNING,WIFI", "general")
        if rc == 127:
            self.add(FAIL, "NetworkManager", "nmcli non installato", "sudo apt install network-manager")
            return
        if rc != 0 or not out.startswith("running"):
            self.add(FAIL, "NetworkManager", out or "non in esecuzione", "sudo systemctl enable --now NetworkManager")
            return
        wifi_on = out.endswith("enabled")
        iface = self.settings["ble"]["nm_iface"]
        rc, dev = sh("nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device")
        line = next((d for d in dev.splitlines() if d.startswith(iface + ":")), "")
        detail = f"radio Wi-Fi {'on' if wifi_on else 'OFF'}, {line or iface + ' assente'}"
        if not wifi_on or not line:
            self.add(FAIL, "NetworkManager", detail, "sudo nmcli radio wifi on; sudo rfkill unblock wifi")
        elif "unavailable" in line:
            self.add(FAIL, "NetworkManager", detail, "Wi-Fi bloccato: impostare il paese (raspi-config)")
        else:
            self.add(PASS, "NetworkManager", detail)

    def check_wifi_country(self) -> None:
        rc, out = sh("raspi-config", "nonint", "get_wifi_country")
        if rc == 0 and out.strip():
            self.add(PASS, "Paese Wi-Fi", out.strip())
            return
        rc, out = sh("iw", "reg", "get")
        if rc == 0:
            import re

            m = re.search(r"country (\w\w)", out)
            c = m.group(1) if m else "?"
            if c in ("00", "?"):
                self.add(FAIL, "Paese Wi-Fi", "non impostato (regdom 00)", "sudo raspi-config nonint do_wifi_country IT")
            else:
                self.add(PASS, "Paese Wi-Fi", c)
            return
        self.add(WARN, "Paese Wi-Fi", "impossibile verificare (raspi-config/iw assenti)")

    def check_service(self) -> None:
        rc, act = sh("systemctl", "is-active", "petmate-core")
        rc2, en = sh("systemctl", "is-enabled", "petmate-core")
        act, en = (act.splitlines() or ["?"])[0], (en.splitlines() or ["?"])[0]
        detail = f"{act}, {en}"
        if act == "active" and en == "enabled":
            self.add(PASS, "Servizio petmate-core", detail)
        elif act == "active":
            self.add(WARN, "Servizio petmate-core", detail, "sudo systemctl enable petmate-core")
        else:
            self.add(FAIL, "Servizio petmate-core", detail, "journalctl -u petmate-core -n 50 --no-pager")

    def check_cloud(self) -> None:
        from .identity import CloudState

        st = CloudState.load(self.paths.cloud_state)
        override = str(self.settings["cloud"].get("url") or "").strip()
        url = (override or st.cloud_url).rstrip("/")
        if not st.provisioned and not override:
            self.add(SKIP, "Cloud raggiungibile", "robot non ancora associato: il cloud_url arriverà dal wizard BLE")
            return
        tried = []
        for u in (url + "/health", url.rsplit("/v1", 1)[0] + "/health"):
            if u in tried:
                continue
            tried.append(u)
            try:
                with urllib.request.urlopen(u, timeout=5) as r:
                    body = r.read(200).decode(errors="replace")
                    if r.status == 200:
                        own = f", proprietario {st.owner_user_id}" if st.provisioned else ""
                        self.add(PASS, "Cloud raggiungibile", f"{u} → {body.strip()}{own}")
                        return
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
            except Exception as e:  # noqa: BLE001
                last = str(getattr(e, "reason", e))
        self.add(FAIL, "Cloud raggiungibile", f"{url}: {last}",
                 "il PC del cloud è acceso e nella stessa LAN? uvicorn avviato con --host 0.0.0.0? firewall porta 8000?")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="petmate-doctor", description="Diagnostica Petmate MVP")
    p.add_argument("--root", default=os.environ.get("PETMATE_ROOT", "/"), help="prefisso per /etc e /var (dev)")
    p.add_argument("--seconds", type=float, default=3.0, help="durata cattura camera")
    p.add_argument("--skip-camera", action="store_true")
    p.add_argument("--skip-system", action="store_true", help="salta BLE/rete/servizio (dev)")
    p.add_argument("--json", action="store_true", help="stampa anche i risultati in JSON")
    a = p.parse_args(argv)
    from .log import setup_logging

    setup_logging("ERROR", "text")
    d = Doctor(a.root, a.seconds, a.skip_camera, a.skip_system)
    rc = d.run()
    if a.json:
        print(json.dumps([r.__dict__ for r in d.results], ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
