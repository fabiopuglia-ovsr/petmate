#!/usr/bin/env bash
# Petmate cloud — developer run without Docker (docs/MVP.md). macOS and Linux.
#
#   ./run_dev.sh                 # venv in .venv, deps, uvicorn on 0.0.0.0:8000
#   PORT=9000 ./run_dev.sh       # any variable of deploy/dev.env can be overridden
#   LAN_HOSTS=192.168.1.50 ./run_dev.sh   # force the LAN address shown / proposed as cloud_url
#   ./run_dev.sh --reload        # extra args go to uvicorn
#
# Data: ./data/dev.db (SQLite) and ./data/clips. Windows: see README (PowerShell).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# --- environment: deploy/dev.env, without overriding what is already set in the shell
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue ;; esac
  key="${line%%=*}"
  val="${line#*=}"
  if [ -z "${!key+x}" ]; then export "$key=$val"; fi
done < deploy/dev.env
: "${DATABASE_URL:=sqlite:///./data/dev.db}"
: "${STORAGE_DIR:=./data/clips}"
export DATABASE_URL STORAGE_DIR
mkdir -p data/clips

# --- python + venv
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || { echo "Serve Python >= 3.10 (macOS: brew install python@3.12)" >&2; exit 1; }

if [ ! -x .venv/bin/python ]; then
  echo "Creo l'ambiente virtuale .venv con $PY…"
  "$PY" -m venv .venv
fi
VPY=.venv/bin/python
STAMP=.venv/.requirements.stamp
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "Installo le dipendenze…"
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install --quiet -r requirements.txt
  touch "$STAMP"
fi

# --- banner with the LAN URLs for the app and the Raspberry Pi
"$VPY" -m app.netinfo "$PORT"

exec "$VPY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" \
  --ws-ping-interval 20 --ws-ping-timeout 20 "$@"
