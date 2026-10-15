# Petmate Cloud

FastAPI backend for Petmate: accounts, device claim/registration, desired/reported config,
commands, events + push, clips, TURN credentials and the WebRTC signaling WebSocket.
Contract: `../docs/INTERFACES.md` §6 (plus §4.2, §4.4, §5, §7). All routes live under `/v1`.

## Layout

```
app/
  main.py        create_app(): wiring, CORS, /health
  config.py      Settings from env (see deploy/.env.example)
  db.py models.py  SQLAlchemy 2.x (SQLite default; Postgres via DATABASE_URL), create_all at startup
  security.py    argon2 hashing, JWT HS256 (typ=user|device, 24 h)
  services.py    claim tokens, factory secret check (+ALLOW_TOFU), TURN REST credentials
  hub.py         SignalingHub interface + InMemoryHub (notes on Redis pub/sub implementation)
  routers/       auth.py, devices.py (REST), signal.py (WS /v1/signal)
  push.py        PushSender (FCM HTTP v1 | logging no-op), Italian texts, notifications flags
  storage.py     Storage interface + LocalStorage (S3 backend can implement presigned_url)
  ratelimit.py   per-IP sliding window on /auth/* and /devices/{register,token}
  cli.py         factory registry admin
  netinfo.py     LAN address detection (dev banner, /v1/server-info)
  routers/info.py  GET /v1/server-info
  static/console/  web test console (/console/), vanilla ES modules, vendored @noble/* 2.0.1
deploy/          Dockerfile, docker-compose.yml (api + caddy + coturn), Caddyfile, turnserver.conf, .env.example
                 docker-compose.dev.yml + dev.env + coturn/turnserver.dev.conf (MVP dev setup)
run_dev.sh       dev run without Docker (venv + uvicorn 0.0.0.0:8000)
tests/           pytest suite (TestClient, HTTP + WebSocket; Playwright console tests)
```

## MVP dev setup (docs/MVP.md): cloud on the laptop, home LAN, plain HTTP :8000

Same settings for both modes (`deploy/dev.env`): `ALLOW_TOFU=true`, dev `JWT_SECRET`, STUN only
(`stun:stun.l.google.com:19302`; `STUN_URLS=none` for a LAN without internet), no TURN.
At startup the LAN URLs are printed: **app** and **robot `cloud_url`** = `http://<LAN IP>:8000/v1`,
**console** (Web Bluetooth) = `http://localhost:8000/console/` in Chrome/Edge on the same laptop.

**Without Docker** (macOS / Linux, Python ≥ 3.10):

```bash
cd cloud && ./run_dev.sh            # creates .venv, installs deps, SQLite in ./data/dev.db
LAN_HOSTS=192.168.1.50 ./run_dev.sh # force the address if detection picks the wrong interface
```

**Windows (PowerShell)**:

```powershell
cd cloud
py -3.12 -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
Get-Content deploy\dev.env | Where-Object { $_ -match '^[A-Z_]+=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2; if (-not (Test-Path "env:$k")) { Set-Item "env:$k" $v } }
$env:DATABASE_URL = "sqlite:///./data/dev.db"; $env:STORAGE_DIR = "./data/clips"
New-Item -ItemType Directory -Force data\clips | Out-Null
.\.venv\Scripts\python -m app.netinfo 8000      # prints the LAN URLs
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Allow Python on "Private networks" when Windows Firewall asks (or open TCP 8000 inbound).

**Docker** (the container cannot see the host LAN IP, so pass it):

```bash
cd cloud/deploy
LAN_HOSTS=192.168.1.50 docker compose -f docker-compose.dev.yml up --build
```

Firewall: macOS asks to allow incoming connections for Python/Docker on first run — allow it;
Linux `ufw allow 8000/tcp`. Check from the Pi: `curl http://192.168.1.50:8000/v1/health`.
Everything works over plain HTTP from other LAN hosts (no HTTPS redirects, HSTS or cookies; auth is
the Bearer header; the console is same-origin). `GET /v1/server-info` (no auth) returns the detected
`base_urls`/`console_urls`; with `PUBLIC_BASE_URL` set it returns only that URL.

> pm-core must accept an `http://` `cloud_url` for the MVP (the robot side currently validates `https://`).

### TURN per test da remoto (optional, compose profile `turn`)

Phone on 4G ↔ robot at home normally works with STUN alone; behind symmetric NAT/CGNAT use TURN:

1. Router: forward **UDP+TCP 3478** and **UDP 49160–49200** to the laptop's LAN IP; for the app outside
   the LAN to reach the API also forward TCP 8000 (dev only!) or use a tunnel.
2. Start with the public IP (or a DDNS name) of the router:
   ```bash
   export TURN_EXTERNAL_IP=203.0.113.7
   export TURN_URLS="turn:203.0.113.7:3478?transport=udp,turn:203.0.113.7:3478?transport=tcp"
   LAN_HOSTS=192.168.1.50 docker compose -f docker-compose.dev.yml --profile turn up --build
   ```
   `/v1/ice-servers` then returns STUN + TURN with REST credentials (`TURN_SECRET` shared by api and coturn,
   default `petmate-dev-turn-secret`). `turnserver.dev.conf` is plain TURN (no TLS) and, unlike production,
   allows relaying to private ranges (the robot is on the same LAN as coturn). Without Docker run
   coturn yourself with that file and the same flags, and export `TURN_URLS`/`TURN_SECRET` before `run_dev.sh`.
3. In the console Live page the "Candidato locale/remoto" row shows `relay` when TURN is used.

### Console di test (`/console/`)

Developer tool, Italian UI, no build step (ES modules + import map; `@noble/curves|ciphers|hashes` 2.0.1
vendored under `static/console/vendor/`, no CDN). Screens: login/registrazione, I miei robot (live presence,
rinomina, rimuovi), Aggiungi robot (BLE wizard §7: X25519 → HKDF-SHA256 → ChaCha20-Poly1305, chunks ≤ 180 B
incl. flags; `cloud_url` prefilled from `/v1/server-info`), Live (non-trickle offer, video recvonly + DataChannel
`ctl`, stats, snapshot). Web Bluetooth needs a secure context: open it as `http://localhost:8000/console/`
with Chrome/Edge on the laptop running the cloud.

Hooks for automated tests (`window.__petmate`): `view`, `errors[]`, `devices`, `session.loggedIn`,
`signal` (`state`, `peer`, `online` Map), `prov` (`encryptPayload`, `chunkMessage`, UUIDs),
`wizard` (`step`, `info`, `status`, `error`, `done`), `live` (`state`: idle|connecting|gathering|waiting_answer|
negotiating|connected|closed|error, `error`: peer_offline|timeout|ice_failed|…, `pc`, `dc`, `stats`
{width,height,fps,kbps,rttMs,localCandidate,remoteCandidate,codec}, `lastState`, `lastSnapshot`, `messages`,
`iceServers`, `video()` → {readyState,videoWidth,videoHeight,currentTime,paused,hasStream}, `start()`, `stop()`).
Selectors: `#login-email #login-password #login-submit #register-submit #login-error #logout #signal-state[data-state]`;
`#robots-list[data-loaded] .robot-row[data-device-id][data-online] .robot-name .robot-live .robot-rename .robot-remove #add-robot`;
wizard `#ble-unavailable #ble-scan #wifi-list li[data-ssid] #wifi-ssid #wifi-password #cloud-url #prov-send #prov-error #prov-retry #prov-done #prov-online`;
live (`#/live/<device_id>`, auto-connects) `#live-video #live-status[data-state] #live-error #live-stats #live-state #live-connect #snapshot-btn #snapshot-img #disconnect-btn`.
Rename/remove use `prompt()`/`confirm()` dialogs.

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export JWT_SECRET=dev-secret-dev-secret-dev-secret TURN_SECRET=dev TURN_HOST=localhost
python -m app.cli add-device PM-000123 my-factory-secret    # or ALLOW_TOFU=true for dev
uvicorn app.main:app --reload --port 8000
pytest -q          # includes Playwright console tests (skipped if Playwright/Chromium missing,
                   # or PETMATE_SKIP_BROWSER=1); screenshots in tests/artifacts/
```

CLI: `init-db`, `add-device <id> <secret> [--model]`, `import-devices file.csv` (`id,secret[,model]`),
`list-devices`, `remove-device <id>`.

## Deploy (single VM)

```bash
cd deploy && cp .env.example .env    # set domains, JWT_SECRET, TURN_SECRET, TURN_EXTERNAL_IP
docker compose up -d --build
docker compose exec api python -m app.cli add-device PM-000123 <secret>
```

- Caddy obtains certificates automatically for `API_DOMAIN` (and `TURN_HOST`); ports 80/443 open.
- coturn runs with host networking: open 3478 udp/tcp, 5349 tcp, 49160–49200 udp.
  For TURNS put `fullchain.pem`/`privkey.pem` for `TURN_HOST` in `deploy/coturn/certs/`
  (copy from the caddy-data volume or use certbot). `TURN_SECRET` must be identical for api and coturn.
- Run the API with **one worker**: the signaling hub is in-memory. For several instances, implement
  `SignalingHub` on Redis pub/sub (sketch in `app/hub.py`) and move the rate limiter to Redis as well.
- FCM: mount a service-account JSON and set `FCM_PROJECT_ID` + `FCM_CREDENTIALS_FILE`;
  otherwise notifications are only logged.

## End-to-end flow

1. **Account** — app: `POST /v1/auth/register|login` → user JWT.
2. **Claim** — app: `POST /v1/claim-tokens` → `{claim_token, expires_at}` (10 min, single use);
   sends it encrypted over BLE (§7) together with Wi-Fi credentials and `cloud_url`.
3. **Register** — pm-core: `POST /v1/devices/register {device_id, device_secret, claim_token}`.
   The secret is checked against `factory_devices` (or auto-pinned with `ALLOW_TOFU=true`),
   the token is consumed, the device is bound to the user → `{device_token, owner_user_id}`.
   Re-claim by another user purges the old owner's events/clips/config.
4. **Token refresh** — pm-core: `POST /v1/devices/token {device_id, device_secret}` before the 24 h expiry.
   `403 {"detail":"device_unclaimed"}` means the owner removed it → go back to BLE provisioning.
5. **Signaling** — both connect to `wss://API_DOMAIN/v1/signal?token=<JWT>` and get
   `{"type":"hello","peer":...}`. Users also get one `presence` per owned device right away, and
   `presence` updates when the device connects/disconnects. `GET /v1/devices` `online` comes from the hub.
6. **Call** — app: `GET /v1/ice-servers`, gathers ICE, sends `offer` to `device:<id>`; the server adds
   `from` and relays; pm-core answers to the `from` peer. `ice`/`bye` relayed the same way.
   If a user socket drops, the server sends `bye` (from that user peer) to devices it was talking to.
7. **Control** — media + DataChannel `ctl` P2P. REST fallbacks: `PUT /config` → WS `config_set`,
   `POST /commands` → WS `command` (409 if the device is offline).
8. **Telemetry** — pm-core: `POST /events` (stored, pushed to owner per `notifications` flags),
   `POST /clips` (multipart `file` + `meta`), `POST /config/reported`.

## Notes for other components

- **WS errors**: `{"type":"error","error":"forbidden"|"peer_offline"|"invalid json"|"unsupported type: X"|...}`;
  relay errors also carry `to` and `ref` (original type). Close codes: 4401 bad/expired token or
  device unclaimed/re-claimed, 4403 unclaimed device at connect, 4409 replaced by a newer connection
  of the same device (pm-core must not reconnect-loop with two instances).
- WS token may also be passed as `Authorization: Bearer` header (handy for pm-core).
- Server sends `pong` to `{"type":"ping"}`; device pings also refresh `last_seen` (≤ once/min).
- `POST /commands` adds `id` (uuid) if missing and returns `202 {"id","delivered":true}`; delivery to the
  WS only, no execution ack through the cloud.
- `PUT /config` returns the stored config and header `X-Petmate-Delivered: true|false`.
- `GET /events?limit=50[&before=<ts>]` → newest first, `[{"id","t","ts",...event fields}]`.
- Clips: `meta` JSON may contain `ts`, `duration_s` (others kept). Optional extra multipart part
  `thumb` (JPEG) → `thumb_url`, otherwise `thumb_url` is null. `url`/`thumb_url` require the user
  `Authorization` header (no signed URLs): the app player must send it. Absolute when `PUBLIC_BASE_URL` set.
- Extensions not in §6: `GET /devices/{id}/config/reported`, `GET /clips/{id}/thumb`, `DELETE /clips/{id}`,
  optional `fw` in `/devices/register` and `/devices/token` bodies (updates `GET /devices` `fw`).
- Push payload `data`: `{"type":"event","event":<t>,"device_id":...}`; Android channel `petmate_events`.
