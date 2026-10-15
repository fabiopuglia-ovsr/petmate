#!/usr/bin/env bash
# Petmate MVP installer (docs/MVP.md) — Raspberry Pi 5, Raspberry Pi OS Bookworm 64-bit,
# Logitech USB webcam, BlueZ, NetworkManager. No MCU/LiDAR/Hailo/display/ROS.
#
# What it does (idempotent, safe to re-run to update the code):
#   1. apt: python3-venv/pip, ffmpeg, v4l-utils, bluez, network-manager, rfkill, iw
#   2. venv /opt/petmate/venv (--system-site-packages) + pip wheels: av, aiortc, bless, ...
#      (aarch64/cp311 wheels exist on PyPI; PyAV's wheel bundles FFmpeg with libx264)
#   3. pm-core code → /opt/petmate/core (editable install), tools/scripts → /opt/petmate/robot
#   4. /etc/petmate/core.json with "profile": "mvp" (kept if present, unless --force)
#   5. /etc/petmate/identity.json via make_identity.sh (kept if present)
#   6. Wi-Fi country (an unset country keeps the Pi's Wi-Fi radio blocked) + rfkill unblock
#   7. BlueZ: bluetooth.service enabled, adapter auto-powered. bless only needs the stock
#      Bookworm bluetoothd (BlueZ 5.66: GattManager1 + LEAdvertisingManager1 are stable
#      APIs, no --experimental, no Privacy/ControllerMode changes needed)
#   8. NetworkManager enabled
#   9. petmate-core.service + MVP drop-in (runs as root: nmcli + BlueZ GATT; see the drop-in)
#  10. /usr/local/bin/petmate-doctor
#
# Usage: sudo robot/scripts/install_mvp.sh [--force] [--country IT] [--cloud-url URL]
#                                          [--skip-apt] [--no-start]
#   --force       overwrite /etc/petmate/core.json (identity is never overwritten here)
#   --country CC  Wi-Fi regulatory country (default IT)
#   --cloud-url   write cloud.url override into core.json (normally the URL comes from the
#                 BLE wizard); e.g. http://192.168.1.50:8000/v1
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # .../petmate
PREFIX=/opt/petmate
VENV=$PREFIX/venv
ETC=/etc/petmate
COUNTRY=IT
FORCE=0
SKIP_APT=0
START=1
CLOUD_URL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1 ;;
    --skip-apt) SKIP_APT=1 ;;
    --no-start) START=0 ;;
    --country) COUNTRY="${2:?}"; shift ;;
    --cloud-url) CLOUD_URL="${2:?}"; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
log()  { printf '\033[1;32m[petmate]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[petmate] WARNING:\033[0m %s\n' "$*" >&2; }

. /etc/os-release 2>/dev/null || true
[[ "${VERSION_CODENAME:-}" == "bookworm" ]] || warn "tested on Raspberry Pi OS Bookworm (found: ${PRETTY_NAME:-unknown})"
[[ "$(uname -m)" == "aarch64" ]] || warn "expected a 64-bit OS (aarch64), found $(uname -m)"

# ------------------------------------------------------------------ 1. apt
if [[ $SKIP_APT -eq 0 ]]; then
  log "installing apt packages"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    ffmpeg v4l-utils \
    bluez network-manager rfkill iw \
    rsync ca-certificates curl
fi

# ------------------------------------------------------------------ 2-3. code + venv
log "installing pm-core into $PREFIX"
install -d -m 0755 $PREFIX $PREFIX/robot $PREFIX/docs
rsync -a --delete --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.egg-info' \
  "$REPO_ROOT/robot/core/" $PREFIX/core/
rsync -a --delete --exclude '__pycache__' "$REPO_ROOT/robot/scripts/" $PREFIX/robot/scripts/
rsync -a --delete --exclude '__pycache__' "$REPO_ROOT/robot/tools/" $PREFIX/robot/tools/
rsync -a "$REPO_ROOT/robot/systemd/" $PREFIX/robot/systemd/
cp -f "$REPO_ROOT"/docs/*.md $PREFIX/docs/ 2>/dev/null || true

if [[ ! -x $VENV/bin/python ]]; then
  log "creating venv $VENV (--system-site-packages)"
  python3 -m venv --system-site-packages $VENV
fi
$VENV/bin/pip install --upgrade --quiet pip wheel
log "pip: binary wheels for av/aiortc/bless (this can take a few minutes the first time)"
$VENV/bin/pip install --prefer-binary --only-binary=av,aiortc,pylibsrtp,cryptography \
  -r $PREFIX/core/requirements-mvp.txt || {
  warn "pip install failed (see above). No binary wheel for this Python/arch?"
  warn "Fallback: apt install libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev libswscale-dev"
  warn "libswresample-dev libavfilter-dev libsrtp2-dev libopus-dev libvpx-dev pkg-config, then re-run"
  warn "  $VENV/bin/pip install -r $PREFIX/core/requirements-mvp.txt"
  exit 1
}
$VENV/bin/pip install --no-deps -e $PREFIX/core
if ! $VENV/bin/python - <<'EOF'
import av, aiortc, aioice, bless, pmcore
assert "libx264" in av.codecs_available, "PyAV without libx264"
print(f"OK: pmcore {pmcore.__version__}, av {av.__version__} (FFmpeg {av.library_versions['libavcodec']}), "
      f"aiortc {aiortc.__version__}, libx264 present")
EOF
then
  warn "Python dependencies incomplete. If no PyAV wheel with libx264 exists for this Python, install the"
  warn "FFmpeg dev packages (libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev libswscale-dev"
  warn "libswresample-dev libavfilter-dev pkg-config) and: $VENV/bin/pip install --no-binary av av"
  exit 1
fi

# ------------------------------------------------------------------ 4. core.json
install -d -m 0755 $ETC
if [[ ! -f $ETC/core.json || $FORCE -eq 1 ]]; then
  log "writing $ETC/core.json (profile mvp)"
  [[ -f $ETC/core.json ]] && cp -f $ETC/core.json $ETC/core.json.bak
  CLOUD_URL="$CLOUD_URL" python3 - "$ETC/core.json" <<'EOF'
import json, os, sys
cfg = {
    "profile": "mvp",
    "camera": {"backend": "uvc", "device": "auto", "main_size": [1280, 720], "fps": 30},
    "video": {"bitrate": 1500000, "x264_threads": 3},
    "log": {"level": "INFO", "format": "text"},
}
if os.environ.get("CLOUD_URL"):
    cfg["cloud"] = {"url": os.environ["CLOUD_URL"]}
with open(sys.argv[1], "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
EOF
  chmod 0644 $ETC/core.json
else
  log "$ETC/core.json present — kept (use --force to overwrite)"
  grep -q '"profile"[[:space:]]*:[[:space:]]*"mvp"' $ETC/core.json || warn "$ETC/core.json has no \"profile\": \"mvp\""
fi

# ------------------------------------------------------------------ 5. identity
"$REPO_ROOT/robot/scripts/make_identity.sh"

# ------------------------------------------------------------------ 6. Wi-Fi country
log "Wi-Fi country: $COUNTRY"
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country "$COUNTRY" || warn "raspi-config do_wifi_country failed"
else
  iw reg set "$COUNTRY" || true
  warn "raspi-config not found: set the country permanently (cfg80211.ieee80211_regdom=$COUNTRY on the kernel cmdline)"
fi
rfkill unblock wifi || true

# ------------------------------------------------------------------ 7. BlueZ
log "configuring BlueZ"
if [[ -f /etc/bluetooth/main.conf ]]; then
  # power the controller automatically at boot (default on 5.66; make it explicit)
  if grep -qE '^\s*#?\s*AutoEnable\s*=' /etc/bluetooth/main.conf; then
    sed -i -E 's/^\s*#?\s*AutoEnable\s*=.*/AutoEnable=true/' /etc/bluetooth/main.conf
  else
    printf '\n[Policy]\nAutoEnable=true\n' >> /etc/bluetooth/main.conf
  fi
fi
rfkill unblock bluetooth || true
systemctl enable bluetooth
systemctl restart bluetooth
sleep 1
bluetoothctl power on >/dev/null 2>&1 || warn "bluetoothctl power on failed (adapter missing?)"

# ------------------------------------------------------------------ 8. NetworkManager
systemctl enable --now NetworkManager
if systemctl is-active --quiet dhcpcd 2>/dev/null; then
  warn "dhcpcd is active: NetworkManager must manage wlan0 (sudo raspi-config → Advanced → Network Config)"
fi
nmcli radio wifi on || true

# ------------------------------------------------------------------ 9. systemd
log "installing petmate-core.service (+ MVP drop-in: runs as root)"
install -m 0644 "$REPO_ROOT/robot/systemd/petmate-core.service" /etc/systemd/system/petmate-core.service
install -d /etc/systemd/system/petmate-core.service.d
install -m 0644 "$REPO_ROOT/robot/systemd/petmate-core.service.d/mvp.conf" \
  /etc/systemd/system/petmate-core.service.d/mvp.conf
install -d -m 0750 /var/lib/petmate
systemctl daemon-reload
systemctl enable petmate-core.service
if [[ $START -eq 1 ]]; then
  systemctl restart petmate-core.service
fi

# ------------------------------------------------------------------ 10. doctor
ln -sf $PREFIX/robot/scripts/petmate_doctor.sh /usr/local/bin/petmate-doctor
chmod +x $PREFIX/robot/scripts/*.sh

DEV="$(python3 -c 'import json; print(json.load(open("/etc/petmate/identity.json"))["device_id"])')"
log "done. Device $DEV — BLE name Petmate-${DEV: -4}"
cat <<EOF

Next steps:
  * logs:        journalctl -u petmate-core -f
  * diagnostics: sudo petmate-doctor
  * cloud (PC):  ALLOW_TOFU=true uvicorn app.main:app --host 0.0.0.0 --port 8000
  * wizard:      app Flutter, console http://localhost:8000/console/ (Chrome, on the cloud PC), or
                 python3 $PREFIX/robot/tools/provision_cli.py --cloud http://<PC-IP>:8000/v1 --email ... --signup
  * re-provision: rm /var/lib/petmate/cloud.json && systemctl restart petmate-core
EOF
