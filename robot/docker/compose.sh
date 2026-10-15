#!/bin/sh
# Run docker compose v2 plugin if present, else docker-compose v1 (Debian Bookworm 'docker-compose').
cd "$(dirname "$0")" || exit 1
if docker compose version >/dev/null 2>&1; then
  exec docker compose -p petmate -f docker-compose.yml "$@"
fi
exec docker-compose -p petmate -f docker-compose.yml "$@"
