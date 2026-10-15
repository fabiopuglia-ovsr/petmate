#!/usr/bin/env bash
# Petmate MVP device identity (docs/MVP.md, INTERFACES.md §8).
#
# Writes /etc/petmate/identity.json = {"device_id": "PM-XXXXXX", "device_secret": "<32 B base64url>"}
#   device_id     = "PM-" + last 6 hex digits of the Raspberry Pi serial (upper case)
#                   (/proc/device-tree/serial-number, else "Serial" in /proc/cpuinfo,
#                   else random — printed as a warning)
#   device_secret = 32 random bytes, base64url without padding (43 chars)
#   permissions   = 0600 root:root (pm-core runs as root in the MVP)
#
# Idempotent: an existing valid identity is kept (the cloud pins the secret on first
# registration with ALLOW_TOFU=true; changing it later makes /devices/token fail with 401).
#
# Usage: sudo make_identity.sh [--force] [--print]
#   --force   regenerate even if present (then remove the device on the cloud / re-provision)
#   --print   only print the current identity (without the secret)
# Env: PETMATE_ROOT=<prefix> (tests/dev: writes <prefix>/etc/petmate/identity.json),
#      PETMATE_SERIAL=<hex> (override the detected serial).
set -euo pipefail

ROOT="${PETMATE_ROOT:-}"
ROOT="${ROOT%/}"
DIR="$ROOT/etc/petmate"
FILE="$DIR/identity.json"
FORCE=0
PRINT=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --print) PRINT=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

valid_identity() {
  python3 - "$1" <<'EOF'
import json, re, sys
try:
    d = json.load(open(sys.argv[1]))
    ok = re.match(r"^PM-[0-9A-Za-z]{4,32}$", d.get("device_id", "")) and len(d.get("device_secret", "")) >= 16
except Exception:
    ok = False
sys.exit(0 if ok else 1)
EOF
}

show() {
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("device_id:", d["device_id"]); print("BLE name:  Petmate-" + d["device_id"][3:][-4:].upper())' "$FILE"
}

if [[ $PRINT -eq 1 ]]; then
  [[ -f "$FILE" ]] || { echo "no identity at $FILE" >&2; exit 1; }
  show
  exit 0
fi

if [[ -z "$ROOT" && $EUID -ne 0 ]]; then
  echo "run as root (sudo)" >&2
  exit 1
fi

if [[ -f "$FILE" && $FORCE -eq 0 ]]; then
  if valid_identity "$FILE"; then
    echo "identity already present ($FILE) — keeping it"
    show
    [[ -z "$ROOT" ]] && { chown root:root "$FILE"; chmod 0600 "$FILE"; }
    exit 0
  fi
  echo "WARNING: $FILE exists but is invalid — regenerating (old copy: $FILE.bak)" >&2
  cp -f "$FILE" "$FILE.bak"
fi

serial="${PETMATE_SERIAL:-}"
if [[ -z "$serial" && -r /proc/device-tree/serial-number ]]; then
  serial="$(tr -d '\0' < /proc/device-tree/serial-number)"
fi
if [[ -z "$serial" && -r /proc/cpuinfo ]]; then
  serial="$(awk -F': *' '/^Serial/ {print $2; exit}' /proc/cpuinfo || true)"
fi
serial="$(printf '%s' "$serial" | tr -cd '0-9a-fA-F')"
if [[ ${#serial} -lt 6 || "$serial" =~ ^0+$ ]]; then
  serial="$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"
  echo "WARNING: Raspberry Pi serial not found — using random id suffix $serial" >&2
fi
suffix="$(printf '%s' "${serial: -6}" | tr 'a-f' 'A-F')"
device_id="PM-$suffix"

umask 077
mkdir -p "$DIR"
tmp="$(mktemp "$DIR/.identity.json.XXXXXX")"
python3 - "$device_id" "$tmp" <<'EOF'
import json, secrets, sys
device_id, path = sys.argv[1], sys.argv[2]
with open(path, "w") as f:
    json.dump({"device_id": device_id, "device_secret": secrets.token_urlsafe(32)}, f, indent=2)
    f.write("\n")
EOF
chmod 0600 "$tmp"
[[ -z "$ROOT" ]] && chown root:root "$tmp"
mv -f "$tmp" "$FILE"
echo "identity written to $FILE"
show
