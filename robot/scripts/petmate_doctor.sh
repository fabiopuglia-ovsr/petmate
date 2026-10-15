#!/usr/bin/env bash
# Petmate MVP doctor: PASS/FAIL checklist (camera, x264, BLE, rete, identità, cloud, servizio).
# Usage: sudo petmate_doctor.sh [--seconds 3] [--skip-camera] [--json]
# The camera capture test needs the webcam to be idle: pm-core opens it only while
# someone watches, so close the app/console stream first (or stop the service).
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PY=/opt/petmate/venv/bin/python
if [[ ! -x $PY ]]; then
  PY=python3
  export PYTHONPATH="$HERE/../core${PYTHONPATH:+:$PYTHONPATH}"
fi
[[ $EUID -eq 0 ]] || echo "(suggerito: eseguire con sudo per leggere identity.json e la camera)" >&2
exec "$PY" -m pmcore.doctor "$@"
