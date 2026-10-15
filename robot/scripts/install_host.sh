#!/usr/bin/env bash
# Petmate host installer — Raspberry Pi 5, Raspberry Pi OS Bookworm 64-bit.
# Installs system packages (camera, Hailo, docker, BlueZ, NetworkManager),
# pm-core into /opt/petmate (venv with --system-site-packages), systemd units,
# polkit rule for nmcli, and /boot/firmware/config.txt snippets.
#
# Usage: sudo ./install_host.sh [--no-reboot-hint] [--skip-apt]
# Idempotent: re-running updates code and units.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # .../petmate
PREFIX=/opt/petmate
VENV=$PREFIX/venv
SVC_USER=petmate
CONFIG_TXT=/boot/firmware/config.txt
SKIP_APT=0
for a in "$@"; do
  case "$a" in
    --skip-apt) SKIP_APT=1 ;;
    *) ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
log() { printf '\033[1;32m[petmate]\033[0m %s\n' "$*"; }

# ------------------------------------------------------------------ apt
if [[ $SKIP_APT -eq 0 ]]; then
  log "installing apt packages"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip python3-dev \
    python3-picamera2 python3-libcamera libcamera-apps \
    hailo-all \
    ffmpeg libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev libswscale-dev libswresample-dev \
    libopus-dev libvpx-dev libsrtp2-dev libssl-dev libffi-dev pkg-config \
    libsdl2-2.0-0 libsdl2-ttf-2.0-0 libsdl2-image-2.0-0 \
    alsa-utils \
    bluez network-manager \
    libzmq5 \
    docker.io docker-compose \
    rpi-eeprom
fi

# ------------------------------------------------------------------ user
if ! id -u $SVC_USER >/dev/null 2>&1; then
  log "creating user $SVC_USER"
  useradd --system --create-home --home-dir /var/lib/petmate-home --shell /usr/sbin/nologin $SVC_USER
fi
for g in video render audio input bluetooth netdev docker gpio; do
  getent group $g >/dev/null && usermod -aG $g $SVC_USER
done

# ------------------------------------------------------------------ dirs
install -d -o $SVC_USER -g $SVC_USER -m 0750 /var/lib/petmate /var/lib/petmate/clips /var/lib/petmate/maps
install -d -m 0755 /etc/petmate
if [[ ! -f /etc/petmate/identity.json ]]; then
  log "WARNING: /etc/petmate/identity.json missing (factory identity) — cloud/BLE provisioning disabled until present"
fi
[[ -f /etc/petmate/identity.json ]] && { chown root:$SVC_USER /etc/petmate/identity.json; chmod 0640 /etc/petmate/identity.json; }

# ------------------------------------------------------------------ code + venv
log "installing pm-core into $PREFIX"
install -d $PREFIX
rsync -a --delete --exclude '__pycache__' --exclude '.pytest_cache' "$REPO_ROOT/robot/core/" $PREFIX/core/
install -d $PREFIX/docs && cp -f "$REPO_ROOT/docs/INTERFACES.md" $PREFIX/docs/ 2>/dev/null || true
if [[ ! -x $VENV/bin/python ]]; then
  python3 -m venv --system-site-packages $VENV
fi
$VENV/bin/pip install --upgrade pip wheel
$VENV/bin/pip install -r $PREFIX/core/requirements.txt
$VENV/bin/pip install --no-deps -e $PREFIX/core
$VENV/bin/python -c "import picamera2, pmcore; print('picamera2 + pmcore import OK')" || \
  log "WARNING: picamera2 not importable from venv (check python3-picamera2)"

# ------------------------------------------------------------------ polkit: allow nmcli for petmate
log "polkit rule for NetworkManager"
install -d /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/50-petmate-nm.rules <<'EOF'
// pm-core (user petmate) may manage Wi-Fi connections during BLE provisioning
polkit.addRule(function(action, subject) {
  if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 && subject.user == "petmate") {
    return polkit.Result.YES;
  }
});
EOF

# ------------------------------------------------------------------ BlueZ: LE peripheral
if [[ -f /etc/bluetooth/main.conf ]]; then
  sed -i 's/^#\?\s*ControllerMode\s*=.*/ControllerMode = dual/' /etc/bluetooth/main.conf || true
  grep -q '^\[GATT\]' /etc/bluetooth/main.conf || printf '\n[GATT]\nCache = no\n' >> /etc/bluetooth/main.conf
fi
rfkill unblock bluetooth || true

# ------------------------------------------------------------------ config.txt
log "updating $CONFIG_TXT"
MARK_BEGIN="# >>> petmate >>>"
MARK_END="# <<< petmate <<<"
if [[ -f $CONFIG_TXT ]]; then
  cp -n $CONFIG_TXT ${CONFIG_TXT}.pre-petmate || true
  sed -i "/$MARK_BEGIN/,/$MARK_END/d" $CONFIG_TXT
  cat >> $CONFIG_TXT <<EOF
$MARK_BEGIN
[all]
# Cameras: two Camera Module 3 Wide (imx708) on CAM/DISP0 (front, index 0) and CAM/DISP1 (rear, index 1)
camera_auto_detect=1
# If auto-detect is unreliable, use explicit overlays instead:
# camera_auto_detect=0
# dtoverlay=imx708,cam0
# dtoverlay=imx708
# AI HAT+ (Hailo-8L) on PCIe: enable and force Gen 3
dtparam=pciex1
dtparam=pciex1_gen=3
# USB peripherals (mic array, MCU) — allow 1.6 A on USB with a 5 A supply/robot PSU
usb_max_current_enable=1
# KMS display driver
dtoverlay=vc4-kms-v3d
max_framebuffers=2
# Round 5" 1080x1080 HDMI panel (custom CVT timing, 60 Hz)
hdmi_force_hotplug=1
hdmi_group=2
hdmi_mode=87
hdmi_cvt=1080 1080 60 6 0 0 0
disable_overscan=1
# Bluetooth stays on the UART (needed for BLE provisioning)
$MARK_END
EOF
  # KMS ignores hdmi_cvt: force the mode on the kernel command line as well
  CMDLINE=/boot/firmware/cmdline.txt
  if [[ -f $CMDLINE ]] && ! grep -q 'video=HDMI-A-1:1080x1080' $CMDLINE; then
    sed -i 's/$/ video=HDMI-A-1:1080x1080M@60/' $CMDLINE
  fi
else
  log "WARNING: $CONFIG_TXT not found — skipping firmware config"
fi

# ------------------------------------------------------------------ udev (MCU symlink, §8)
install -m 0644 "$REPO_ROOT/firmware/udev/99-petmate.rules" /etc/udev/rules.d/99-petmate.rules
cat > /etc/udev/rules.d/98-petmate-hailo.rules <<'EOF'
# Hailo device access
SUBSYSTEM=="hailo_chardev", GROUP="video", MODE="0660"
EOF
udevadm control --reload-rules || true

# ------------------------------------------------------------------ LiDAR network (eth0 dedicated, §8)
# Static profile for the Unitree L2; never used as default route.
# NOTE: if the home Wi-Fi is also 192.168.1.0/24 the subnets collide - re-address the
# LiDAR to a dedicated subnet with Unitree's tool and update lidar_ip/host_ip (open item).
if command -v nmcli >/dev/null; then
  nmcli -t -f NAME connection show | grep -qx petmate-lidar || \
    nmcli connection add type ethernet ifname eth0 con-name petmate-lidar \
      ipv4.method manual ipv4.addresses 192.168.1.2/24 ipv4.never-default yes \
      ipv6.method disabled connection.autoconnect yes || log "WARNING: could not create LiDAR eth0 profile"
fi

# ------------------------------------------------------------------ ROS 2 stack (docker)
log "deploying ROS 2 stack to $PREFIX/robot and building the image (long on first run)"
mkdir -p "$PREFIX/robot" /var/lib/petmate/maps
rm -rf "$PREFIX/robot/docker" "$PREFIX/robot/ros2_ws"
cp -a "$REPO_ROOT/robot/docker" "$REPO_ROOT/robot/ros2_ws" "$PREFIX/robot/"
chmod +x "$PREFIX/robot/docker/"*.sh
"$PREFIX/robot/docker/compose.sh" build || log "WARNING: docker image build failed - run $PREFIX/robot/docker/compose.sh build manually"

# ------------------------------------------------------------------ systemd
log "installing systemd units"
install -m 0644 "$REPO_ROOT/robot/systemd/petmate-core.service" /etc/systemd/system/petmate-core.service
if [[ -f "$REPO_ROOT/robot/systemd/petmate-ros.service" ]]; then
  install -m 0644 "$REPO_ROOT/robot/systemd/petmate-ros.service" /etc/systemd/system/petmate-ros.service
fi
systemctl daemon-reload
systemctl enable --now docker.service || true
systemctl enable NetworkManager.service bluetooth.service
systemctl enable petmate-core.service
[[ -f /etc/systemd/system/petmate-ros.service ]] && systemctl enable petmate-ros.service || \
  log "petmate-ros.service not found in repo (provided by the ROS team) — not enabled"

# ------------------------------------------------------------------ checks
log "checks"
command -v hailortcli >/dev/null && hailortcli fw-control identify || log "hailortcli not available yet (reboot needed?)"
[[ -f /usr/share/hailo-models/yolov8s_h8l.hef ]] || log "WARNING: yolov8s_h8l.hef missing (apt install hailo-models)"
log "done. Reboot required for config.txt changes: sudo reboot"
