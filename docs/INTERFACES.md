# Petmate — Contratti di interfaccia (v0.2, prototipo EVT)

Questo file è la **fonte unica di verità** per tutte le interfacce tra i sottosistemi.
Ogni componente (firmware, ROS 2, pm-core, cloud, app) DEVE rispettarlo alla lettera.

## 0. Architettura in breve

```
 App Flutter (iOS/Android)
   │  HTTPS REST + WSS signaling          WebRTC (video H.264, audio Opus x2, DataChannel "ctl")
   ▼                                        │  (P2P in LAN o via STUN/TURN da remoto)
 Cloud (FastAPI + coturn)  ◄── WSS ──► pm-core (Raspberry Pi OS, host, Python asyncio)
                                          │  camere x2 (picamera2) + Hailo AI HAT+ (YOLO)
                                          │  display tondo HDMI (pygame/KMS), audio USB (AEC)
                                          │  BLE provisioning (bless), clip recorder, config store
                                          │  ZeroMQ localhost  (5560 core→ros, 5561 ros→core)
                                          ▼
                              ROS 2 Jazzy in Docker (network_mode: host)
                              petmate_bridge · petmate_base · petmate_behavior
                              unitree_lidar (L2) · pointcloud_to_laserscan · EKF · slam_toolbox · Nav2 · twist_mux
                                          │  USB CDC, COBS + CRC16
                                          ▼
                              MCU RP2350 (Pico 2) — motori, encoder, IMU, cliff, bumper,
                              feeder (coclea + cella di carico), lanciapalline, rullo raccolta,
                              braccio "teaser", LED, batteria, dock
```

Lingua del codice: identificatori/commenti in inglese; stringhe UI app in italiano (con i18n pronto per EN).

---

## 1. Hardware di riferimento e pinout MCU (RP2350 / Raspberry Pi Pico 2)

| GPIO | Funzione | Note |
|---|---|---|
| GP0 | Servo braccio teaser (PWM 50 Hz) | 500–2500 µs |
| GP1 | Servo gate palline (PWM 50 Hz) | 500–2500 µs; chiuso=1000 µs, aperto=1900 µs |
| GP2 | Motore SX PWM (DRV8874 EN) | 20 kHz |
| GP3 | Motore SX DIR (DRV8874 PH) | |
| GP4 | Motore DX PWM (DRV8874 EN) | 20 kHz |
| GP5 | Motore DX DIR (DRV8874 PH) | |
| GP6/GP7 | Encoder SX A/B | quadratura via PIO |
| GP8/GP9 | Encoder DX A/B | quadratura via PIO |
| GP10 | HX711 SCK | cella di carico tramoggia |
| GP11 | HX711 DOUT | |
| GP12/GP13 | I2C0 SDA/SCL 400 kHz | ICM-42688-P (0x68), INA226 (0x40), ADS1115 (0x48), PCA9685 (0x41) |
| GP14/GP15 | I2C1 SDA/SCL 400 kHz | 3× VL53L1X (indirizzi riassegnati 0x30,0x31,0x32 via XSHUT) |
| GP16 | WS2812 (24 LED anello + 2 orecchie) | PIO |
| GP17 | Bumper anteriore (NC, pull-up) | attivo = livello alto |
| GP18 | Bumper posteriore (NC, pull-up) | attivo = livello alto |
| GP19 | Wheel-drop (2 switch in serie NC) | attivo = livello alto |
| GP20 | Sensore Hall giri coclea | 1 impulso/giro |
| GP21 | Barriera IR caricatore palline | basso = pallina presente |
| GP22 | nFAULT DRV8874 (wired-OR) | attivo basso |
| GP26 (ADC0) | Tensione batteria (partitore 1:6) | backup di INA226 |
| GP27 (ADC1) | Rilevamento contatti dock | > 1.5 V = docked |
| GP28 | Pulsante "zampa" sul dorso | attivo basso; corto=play, lungo 2 s=sleep/stop, 5 s=provisioning |

PCA9685 @ 0x41, PWM 1.5 kHz, uscite verso 2× DRV8833:
| Canale | Funzione |
|---|---|
| 0/1 | Coclea feeder IN1/IN2 (DRV8833 #1 A) |
| 2/3 | Rullo raccolta IN1/IN2 (DRV8833 #1 B) |
| 4/5 | Flywheel SX IN1/IN2 (DRV8833 #2 A) |
| 6/7 | Flywheel DX IN1/IN2 (DRV8833 #2 B) |
| 8/9/10 | XSHUT VL53L1X sinistra/centro/destra (usati come GPIO full-on/full-off) |
| 11 | Abilitazione 5 V servo (MOSFET) |

ADS1115 @ 0x48: AIN0..3 = 4 sensori cliff Sharp GP2Y0A51SK0F (FL, FR, RL, RR). Soglia cliff: distanza > 60 mm.

Geometria (per URDF e odometria):
- Diametro scocca 340 mm, altezza scocca 165 mm, LiDAR al centro del dorso, centro ottico a z = 0.235 m.
- Ruote motrici Ø 72 mm, carreggiata (track) 0.260 m, asse ruote passante per il centro geometrico (x=0).
- Caster anteriore a x=+0.130 m, posteriore a x=−0.130 m, Ø 40 mm; ruote motrici su sospensione a bilanciere con molla.
- Camera frontale: x=+0.155, z=0.135 (cornice del display, sopra la ciotola), pitch −10°. Camera posteriore: x=−0.155, z=0.110, yaw π, pitch −10°.
- Encoder: parametro `ticks_per_rev` (default 1320 tick/giro ruota, x4).
- Limiti: v_max auto 0.40 m/s, teleop 0.50 m/s, ω_max 2.0 rad/s, a_max 0.6 m/s², α_max 3.0 rad/s².

Frame ROS: `map` → `odom` → `base_footprint` → `base_link` (z = 0.036) → `lidar_link`, `imu_link`, `camera_front_link`, `camera_rear_link`.

---

## 2. Protocollo seriale Host ↔ MCU (USB CDC)

- Trasporto: USB CDC ACM (`/dev/petmate_mcu` via regola udev, VID 0x2E8A). Baud ignorato.
- Framing: **COBS**, delimitatore 0x00. Frame decodificato: `[type:u8][seq:u8][payload…][crc:u16 LE]`.
- CRC: CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, xorout 0) su `type|seq|payload`.
- Tutti i campi little-endian. `float` = IEEE754 32 bit.
- Watchdog: se non arriva `CMD_VEL` o `PING` per **250 ms**, l'MCU porta le velocità a zero con rampa di decelerazione.

### Host → MCU
| type | Nome | Payload |
|---|---|---|
| 0x01 | CMD_VEL | `i16 v_mm_s`, `i16 w_mrad_s` |
| 0x02 | FEED | `u16 grams_x10` (es. 250 = 25.0 g) |
| 0x03 | LAUNCH | `u8 power_pct` (clamp 20–70), `u8 count` (1–4) |
| 0x04 | INTAKE | `u8 mode` (0 off, 1 in, 2 out) |
| 0x05 | TEASE | `u8 pattern` (0 stop, 1 wiggle, 2 sweep, 3 random), `u16 duration_ms` |
| 0x06 | LED | `u8 mode` (0 off, 1 solid, 2 breathe, 3 rainbow, 4 chase), `u8 r`, `u8 g`, `u8 b` |
| 0x07 | ESTOP | `u8 engage` (1 attiva, 0 rilascia) |
| 0x08 | SET_PARAM | `u8 param_id`, `f32 value` |
| 0x09 | PING | — |
| 0x0A | RESET_ODOM | — |
| 0x0B | FEED_ABORT | — |
| 0x0C | TARE_SCALE | — |

`param_id`: 1 kp, 2 ki, 3 kd (PID velocità ruota), 4 ticks_per_rev, 5 wheel_radius_m, 6 track_m, 7 v_max, 8 w_max, 9 a_max, 10 scale_factor (tick HX711 per grammo), 11 cliff_threshold_mm.

### MCU → Host
| type | Nome | Payload | Freq. |
|---|---|---|---|
| 0x81 | STATE | `u32 t_ms`, `f32 x`, `f32 y`, `f32 th`, `f32 v`, `f32 w`, `i32 enc_l`, `i32 enc_r`, `i16 ax_mg`, `i16 ay_mg`, `i16 az_mg`, `i16 gx_cdps`, `i16 gy_cdps`, `i16 gz_cdps`, `u8 cliff_mask`, `u8 bump_mask`, `u8 flags`, `u16 batt_mv`, `i16 batt_ma`, `u8 soc_pct` | 50 Hz |
| 0x82 | FEED_STATUS | `u8 state`, `f32 dispensed_g`, `f32 hopper_g` | su evento + 2 Hz durante erogazione |
| 0x83 | LAUNCH_STATUS | `u8 state`, `u8 balls` | su evento |
| 0x84 | EVENT | `u8 code`, `u16 data` | su evento |
| 0x85 | PONG | `u8 fw_major`, `u8 fw_minor`, `u8 fw_patch`, `u32 uptime_ms` | risposta a PING |
| 0x86 | LOG | testo ASCII (≤ 120 B) | |

- `cliff_mask` bit0 FL, bit1 FR, bit2 RL, bit3 RR. `bump_mask` bit0 front, bit1 rear.
- `flags`: bit0 estop, bit1 wheel_drop, bit2 motor_fault, bit3 charging, bit4 docked, bit5 button_pressed, bit6 watchdog_tripped.
- gyro in centi-gradi/s (cdps). acc in mg.
- FEED state: 0 idle, 1 dispensing, 2 done, 3 jam, 4 hopper_empty, 5 aborted.
- LAUNCH state: 0 idle, 1 spinning_up, 2 launching, 3 done, 4 empty.
- EVENT code: 1 cliff, 2 bump, 3 wheel_drop, 4 motor_fault, 5 button_short, 6 button_long, 7 button_provision, 8 dock_contact, 9 undock, 10 battery_low (data=soc), 11 battery_critical, 12 feeder_jam, 13 hopper_low (data=grams), 14 watchdog.

Sicurezza lato MCU (indipendente dall'host):
- cliff anteriore attivo ⇒ blocca v>0; cliff posteriore ⇒ blocca v<0; bumper idem; wheel_drop ⇒ stop totale.
- ESTOP o nFAULT ⇒ motori in coast, flywheel e coclea fermi.
- Flywheel abilitato solo se v=0 e nessun bumper attivo. Coclea solo se robot fermo da > 500 ms (misura peso stabile).

---

## 3. IPC pm-core ↔ ROS 2 (ZeroMQ, localhost)

- `pm-core` **bind** PUB su `tcp://127.0.0.1:5560` (core→ros). Il bridge ROS fa SUB.
- `petmate_bridge` **bind** PUB su `tcp://127.0.0.1:5561` (ros→core). pm-core fa SUB.
- Messaggi multipart: `[topic (utf-8), payload JSON (utf-8)]`.

### core→ros
| topic | payload |
|---|---|
| `cmd` | Comando (vedi §4.2) con campo obbligatorio `id` (uuid) |
| `detections` | `{"stamp": float, "camera": "front"\|"rear", "objects": [{"cls": str, "conf": float, "bbox": [x1,y1,x2,y2] (normalizzati 0..1), "bearing_rad": float, "track_id": int}]}` — `bearing_rad` nel frame `base_link` (positivo a sinistra) |
| `config` | Configurazione completa (vedi §5) — inviata all'avvio e a ogni modifica |

Classi rilevate (`cls`): `cat`, `dog`, `person`, `sports ball`, `bowl` (nomi COCO).

### ros→core
| topic | payload |
|---|---|
| `state` | Vedi §4.3, pubblicato a 2 Hz |
| `event` | `{"t": str, "ts": float, ...}` (vedi §4.4) |
| `ack` | `{"id": str, "ok": bool, "error": str\|null}` |

---

## 4. DataChannel WebRTC "ctl" (App ↔ pm-core) e comandi

DataChannel con label `ctl`, ordered, JSON UTF-8, un oggetto per messaggio, campo `t` = tipo.

### 4.1 Media
- Robot → App: 1 traccia video H.264 (Constrained Baseline, 1280×720@25 fps, libx264 ultrafast/zerolatency, 1.5 Mb/s target) dalla camera selezionata; 1 traccia audio Opus (mic array con AEC).
- App → Robot: 1 traccia audio Opus (voce proprietario → altoparlante); 1 traccia video opzionale (camera frontale telefono → display tondo del robot).
- L'app crea l'offerta (`recvonly`/`sendrecv` come sopra) **dopo** il completamento dell'ICE gathering (no trickle, compatibile aiortc). Messaggi `ice` restano opzionali.

### 4.2 Comandi App → Robot (stessi oggetti su ZMQ `cmd`, con `id` aggiunto se assente)
```json
{"t":"teleop","v":0.2,"w":0.5}                     // 10 Hz; deadman 300 ms
{"t":"feed","grams":20}                             // 1..60
{"t":"feed_abort"}
{"t":"launch","power":40,"count":1}
{"t":"intake","mode":"in"|"out"|"off"}
{"t":"tease","pattern":"wiggle"|"sweep"|"random"|"stop","duration_s":20}
{"t":"led","mode":"solid"|"breathe"|"rainbow"|"chase"|"off","color":"#F0A830"}
{"t":"goto","target":"dock"|"pet"|"poi","poi":"cucina"}
{"t":"mode","mode":"auto"|"manual"|"sleep"}
{"t":"estop","engage":true}
{"t":"play","game":"chase_ball"|"teaser"|"screen_birds"|"laser_dots","duration_s":300}
{"t":"save_poi","name":"cucina"}                    // salva posa corrente come POI
{"t":"map","action":"start_mapping"|"stop_mapping"|"save"|"clear"}
```
Solo su DataChannel (gestiti da pm-core, non inoltrati a ROS):
```json
{"t":"camera","id":"front"|"rear"}
{"t":"display","mode":"owner"|"face"|"game","game":"birds"|"fish"|"mouse"}
{"t":"volume","speaker":0..100}
{"t":"snapshot"}                                     // risposta: {"t":"snapshot","jpeg_b64":...}
{"t":"record_clip","seconds":15}
{"t":"config_get"} / {"t":"config_set","config":{...}}
```

### 4.3 Stato Robot → App (2 Hz, `{"t":"state", ...}`; identico al topic ZMQ `state`)
```json
{"t":"state","ts":1760000000.0,
 "battery":{"soc":82,"voltage":15.6,"current":-1.2,"charging":false,"docked":true},
 "pose":{"x":1.2,"y":-0.4,"th":0.3,"localized":true},
 "mode":"auto","activity":"idle",
 "feeder":{"state":"idle","hopper_g":210.0,"today_g":50.0,"last_meal":{"ts":1760000000.0,"grams":25.0}},
 "launcher":{"state":"idle","balls":3},
 "safety":{"estop":false,"cliff_mask":0,"bump_mask":0,"wheel_drop":false,"motor_fault":false},
 "nav":{"status":"idle","goal":null,"mapping":false},
 "pet":{"last_seen_ts":1760000000.0,"camera":"front","bearing_rad":0.1},
 "pois":["cucina","salotto"],
 "fw":"0.1.0"}
```
`activity` ∈ `idle|patrolling|feeding|playing|docking|undocking|teleop|seeking_pet|mapping|sleeping|error`.
`nav.status` ∈ `idle|active|succeeded|failed|canceled`.

### 4.4 Eventi (ZMQ `event` → pm-core → DataChannel `{"t":"event",...}` e cloud `POST /v1/devices/{id}/events`)
`meal_dispensed{grams}`, `meal_failed{reason}`, `feeder_jam`, `hopper_low{grams}`, `pet_detected{camera,cls}`, `play_started{game}`, `play_ended{game,duration_s,active_s}`, `battery_low{soc}`, `docked`, `undocked`, `stuck`, `cliff`, `estop`, `nav_failed{target}`.

---

## 5. Configurazione dispositivo (JSON, persistita da pm-core in `/var/lib/petmate/config.json`)
```json
{
  "version": 1,
  "pet": {"name": "Micio", "species": "cat", "weight_kg": 4.5},
  "feeding": {
    "schedule": [{"time": "08:00", "grams": 25, "days": [1,2,3,4,5,6,7]}],
    "portion_max_g": 60, "daily_max_g": 120, "feeding_poi": "cucina"
  },
  "play": {
    "auto_play": true,
    "sessions": [{"time": "14:00", "duration_min": 10, "days": [1,2,3,4,5,6,7]}],
    "launch_power": 40, "games": ["chase_ball", "teaser", "screen_birds"],
    "quiet_hours": {"start": "22:00", "end": "07:00"}
  },
  "patrol": {"enabled": false, "interval_min": 120},
  "notifications": {"meals": true, "activity": true, "battery": true, "pet_detected": false},
  "privacy": {"stream_led": true, "rear_camera_streaming": true},
  "audio": {"speaker_volume": 70},
  "timezone": "Europe/Rome"
}
```
`days`: 1 = lunedì … 7 = domenica (ISO 8601).

---

## 6. Cloud API (FastAPI) — base `https://api.petmate.example/v1`

Auth utente: `Authorization: Bearer <JWT user>` (HS256, claim `sub`=user_id, `typ`="user", 24 h).
Auth device: `Authorization: Bearer <JWT device>` (claim `sub`=device_id, `typ`="device", 24 h).

| Metodo | Path | Chi | Body / Risposta |
|---|---|---|---|
| POST | `/auth/register` | — | `{email,password}` → `{access_token}` |
| POST | `/auth/login` | — | `{email,password}` → `{access_token}` |
| POST | `/claim-tokens` | user | → `{claim_token, expires_at}` (validità 10 min, monouso) |
| POST | `/devices/register` | — | `{device_id, device_secret, claim_token}` → `{device_token, owner_user_id}` |
| POST | `/devices/token` | — | `{device_id, device_secret}` → `{device_token}` |
| GET | `/devices` | user | → `[{device_id, name, online, last_seen, fw}]` |
| PATCH | `/devices/{id}` | user | `{name}` |
| DELETE | `/devices/{id}` | user | rimuove associazione |
| GET | `/ice-servers` | user/device | → `{"ice_servers":[{"urls":[...]},{"urls":[...],"username":...,"credential":...}]}` (coturn REST, TTL 1 h) |
| GET/PUT | `/devices/{id}/config` | user (PUT, GET) / device (GET) | config desiderata §5; PUT inoltra `config_set` via WS se online |
| POST | `/devices/{id}/config/reported` | device | config effettiva |
| POST | `/devices/{id}/commands` | user | comando §4.2 → inoltrato al device via WS `{"type":"command","cmd":{...}}`; 409 se offline |
| POST | `/devices/{id}/events` | device | evento §4.4 → salvato + push FCM al proprietario |
| GET | `/devices/{id}/events?limit=50` | user | lista eventi |
| POST | `/devices/{id}/clips` | device | multipart `file` (mp4) + `meta` JSON → `{clip_id}` |
| GET | `/devices/{id}/clips` | user | `[{clip_id, ts, duration_s, thumb_url, url}]` |
| GET | `/clips/{clip_id}` | user | file mp4 |
| POST | `/push-tokens` | user | `{token, platform}` |
| GET | `/health` | — | `{"ok":true}` |

### Signaling WebSocket: `wss://…/v1/signal?token=<JWT>`
Il server assegna `peer` = `device:<id>` oppure `user:<uid>:<session_uuid>` e invia subito `{"type":"hello","peer":"..."}`.
Messaggi (il server aggiunge `from`, verifica l'autorizzazione: un utente può parlare solo con i device che possiede; un device solo con sessioni di utenti proprietari):
```json
{"type":"offer","to":"device:PM-0001","sdp":"..."}
{"type":"answer","to":"user:42:5f2c...","sdp":"..."}
{"type":"ice","to":"...","candidate":{"candidate":"...","sdpMid":"0","sdpMLineIndex":0}}
{"type":"bye","to":"..."}
{"type":"command","cmd":{...}}          // server → device (da REST /commands)
{"type":"config_set","config":{...}}    // server → device (da REST PUT /config)
{"type":"presence","device_id":"...","online":true}   // server → user
{"type":"error","error":"..."}
```
Ping/pong applicativo: `{"type":"ping"}` ogni 20 s → `{"type":"pong"}`.

---

## 7. BLE provisioning (pm-core GATT server, app = central)

Attivo se il device non è provisionato, oppure per 10 min dopo pressione 5 s del pulsante zampa.
Nome advertising: `Petmate-XXXX` (ultime 4 cifre del device_id).

Service UUID: `8f1d0000-5e7a-4c3a-9d8e-7e7f1a2b3c4d`
| Char UUID | Proprietà | Contenuto |
|---|---|---|
| `8f1d0001-…` | read | `{"device_id":"PM-000123","model":"PM-EVT1","fw":"0.1.0","pubkey":"<X25519 base64>"}` |
| `8f1d0002-…` | read | scansione Wi-Fi `[{"ssid":"Casa","rssi":-52,"secure":true}]` (max 15) |
| `8f1d0003-…` | write | payload cifrato a chunk (vedi sotto) |
| `8f1d0004-…` | read, notify | `{"state":"idle"\|"receiving"\|"connecting"\|"wifi_ok"\|"registering"\|"done"\|"error","error":null,"ip":null}` |

(I suffissi completano l'UUID come il service: `8f1d000N-5e7a-4c3a-9d8e-7e7f1a2b3c4d`.)

Cifratura payload provisioning:
1. App genera coppia X25519 effimera; `shared = X25519(app_priv, device_pub)`.
2. `key = HKDF-SHA256(shared, salt=utf8(device_id), info=b"petmate-prov-v1", L=32)`.
3. Plaintext JSON: `{"ssid":"…","password":"…","claim_token":"…","cloud_url":"https://api.petmate.example/v1","timezone":"Europe/Rome"}`.
4. `ct = ChaCha20-Poly1305(key, nonce12 random, plaintext, aad=utf8(device_id))`.
5. Messaggio: `{"epk":b64(app_pub),"nonce":b64(nonce),"ct":b64(ct)}` serializzato UTF-8.
6. Chunking: frammenti da ≤ 180 byte; ogni write = `[flags:u8][dati]`, flags bit0 = primo, bit1 = ultimo.

Flusso: device decifra → `nmcli` connette → `POST /devices/register` con claim_token → stato `done` → BLE si spegne.

---

## 8. Porte e percorsi sul robot
- Servizi systemd host: `petmate-core.service`, `petmate-ros.service` (docker compose).
- Config: `/var/lib/petmate/config.json`; identità: `/etc/petmate/identity.json` `{"device_id","device_secret"}` (fabbrica); stato cloud `/var/lib/petmate/cloud.json`; clip `/var/lib/petmate/clips/`; mappe `/var/lib/petmate/maps/` (montato nel container ROS); POI `/var/lib/petmate/pois.yaml`.
- Device MCU: `/dev/petmate_mcu`; LiDAR L2 via Ethernet UDP (IP LiDAR 192.168.1.62, host 192.168.1.2 su eth0 dedicata) — in alternativa UART TTL.

---

## 9. Chiarimenti v0.2 (allineamento dopo la prima implementazione)

1. **Inviluppo eventi sul DataChannel**: `{"t":"event","event":{"t":"meal_dispensed","ts":…,…}}`. Su ZMQ e cloud l'evento resta piatto (`t` = tipo).
2. **Risposte ai comandi**: `{"t":"ack","id":"…","ok":true,"error":null}` per ogni comando con `id` (ROS ack anche il teleop). `config_get`/`config_set` rispondono `{"t":"config","config":{…}}`.
3. **Nuovi eventi** (ros→core): `display_game{game: "birds"|"fish"|"mouse"|null, duration_s?}` (pm-core cambia modalità display), `button_provision` (MCU EVENT 7 → apre finestra BLE), `fault{what}`.
4. **BLE chunk**: 180 byte = intera write **incluso** il byte flags. Android: richiedere MTU ≥ 185 (`requestMtu(247)`).
5. **Timestamp**: robot/ZMQ/DataChannel = epoch secondi float; REST cloud = ISO 8601 UTC (`last_seen`, `expires_at`, `ts` di eventi e clip).
6. **Codici HTTP**: 201 su creazione (register, claim-tokens, events, clips), 202 `{id, delivered}` su `POST /commands` (409 se offline); errori `{"detail": "…"}`. WS close: 4401 token non valido/device rimosso, 4403 device non associato, 4409 connessione sostituita.
7. **Download clip**: `url`/`thumb_url` richiedono l'header Bearer dell'utente (nessun URL firmato nel prototipo).
8. **ESTOP/nFAULT**: con il pinout PH/EN del DRV8874 l'arresto è in **frenatura** (brake), non coast; il coast richiede nSLEEP su GPIO dedicato (da prevedere sulla PCB EVT2).
9. **Limiti velocità**: l'MCU applica 0.50 m/s (teleop); il limite 0.40 m/s in autonomia è applicato da Nav2. α_max costante di firmware.
10. **Pasti e quiet hours**: i pasti NON sono bloccati dalle quiet hours (solo gioco e pattuglia). Slot mancati > 15 min ⇒ `meal_failed{reason:"missed"}`.
11. **Valori null**: `feeder.hopper_g` e i campi `pet` sono `null` finché non disponibili. `nav.goal`: `{"target":"dock"|"pet"|"poi","poi":str|null}` o `null`.
12. **Montaggio LiDAR/IMU**: diritti, assi allineati a `base_link`; quote z misurate dal pavimento.
