# pm-core (`pmcore`)

Servizio host del robot (Raspberry Pi 5, Python asyncio). Contratti: `docs/INTERFACES.md`.

```bash
cd robot/core
pip install -r requirements.txt pytest      # su un PC di sviluppo (oppure --break-system-packages)
python3 -m pytest -q                          # suite completa, nessun hardware richiesto
python3 -m pmcore --root /tmp/pm --log-format text   # avvio in locale (PETMATE_ROOT=/tmp/pm)
```

---

## Configurazione MVP (docs/MVP.md)

Hardware: Raspberry Pi 5 + Raspberry Pi OS Bookworm 64 bit + webcam USB Logitech, BlueZ,
NetworkManager. Niente MCU, LiDAR, Hailo, display, ROS.

### Installazione sul Pi

```bash
git clone … petmate && cd petmate
sudo robot/scripts/install_mvp.sh --country IT      # idempotente; --force riscrive core.json
sudo petmate-doctor                                  # checklist PASS/FAIL in italiano
journalctl -u petmate-core -f
```

`install_mvp.sh`: pacchetti apt (python3-venv, ffmpeg, v4l-utils, bluez, network-manager, rfkill, iw),
venv `/opt/petmate/venv` (`--system-site-packages`) con le wheel aarch64 di `av`/`aiortc`/`bless`
(`requirements-mvp.txt`; la wheel PyAV include FFmpeg con libx264), codice in `/opt/petmate/core`,
`/etc/petmate/core.json` col profilo `mvp` (non sovrascritto senza `--force`), `make_identity.sh`,
paese Wi-Fi (`raspi-config nonint do_wifi_country`: senza paese il Wi-Fi del Pi resta bloccato),
`bluetooth` + `NetworkManager` abilitati, `petmate-core.service` + drop-in
`petmate-core.service.d/mvp.conf`.

**Privilegi**: nel prototipo pm-core gira come **root** (drop-in `mvp.conf`), perché deve usare
`nmcli` per creare/attivare connessioni Wi-Fi e registrare su BlueZ (D-Bus) il servizio GATT e
l'advertising. Il BlueZ di Bookworm (5.66) va bene così com'è: `bless` usa le API stabili
`GattManager1`/`LEAdvertisingManager1`, nessun `--experimental`, nessuna modifica a
`Privacy`/`ControllerMode` (lo script imposta solo `AutoEnable=true`). Per tornare all'utente
`petmate` (unit di prodotto + regola polkit di `install_host.sh`) basta rimuovere il drop-in.

Identità: `sudo robot/scripts/make_identity.sh` → `/etc/petmate/identity.json` (0600 root),
`device_id = PM-` + ultime 6 cifre hex del seriale (`/proc/device-tree/serial-number`, poi
`/proc/cpuinfo`, altrimenti casuale con warning), `device_secret` 32 byte base64url. Idempotente:
non rigenera mai un'identità valida senza `--force` (il cloud con `ALLOW_TOFU=true` fissa il
secret alla prima registrazione). Nome BLE: `Petmate-` + ultimi 4 caratteri (`PM-A1B2C3` → `Petmate-B2C3`).

### `/etc/petmate/core.json`

```json
{
  "profile": "mvp",
  "camera": {"backend": "uvc", "device": "auto", "main_size": [1280, 720], "fps": 30},
  "video": {"bitrate": 1500000, "x264_threads": 3},
  "log": {"level": "INFO", "format": "text"}
}
```

Ordine di risoluzione: default → profilo → `core.json` → variabili d'ambiente
`PMCORE_<SEZIONE>_<CHIAVE>` (es. `PMCORE_CAMERA_FPS=25`). `PMCORE_PROFILE` sceglie il profilo.
Il profilo `mvp` imposta: `zmq.enabled=false` (nessun bind ZMQ, nessuna attesa dello stato ROS),
`detector/display/audio/clips.enabled=false`, `camera.backend=uvc`, `camera.device=auto`, 30 fps,
`cloud.allow_http=true` (il `cloud_url` del wizard può essere `http://192.168.1.50:8000/v1`).

| Chiave | Default | Note |
|---|---|---|
| `camera.backend` | `picamera2` | `uvc` (MVP), `lavfi` (test, stesso codice di `uvc`), `synthetic` |
| `camera.device` | `auto` | `auto` = primo `/dev/v4l/by-id/*Logitech*-video-index0` (anche `usb-046d_*`), poi primo `/dev/videoN` con driver `uvcvideo` e capability VIDEO_CAPTURE; oppure un path esplicito |
| `camera.input_format` | `mjpeg` | primo tentativo; poi MJPEG 640×480, YUYV 640×480, default del driver |
| `camera.main_size`, `camera.fps` | 1280×720, 25 (`mvp`: 30) | richiesti alla camera; l'encoder segue la risoluzione reale |
| `camera.reconnect_s` | 2 | retry se la camera manca o viene scollegata |
| `camera.idle_close_s` | 5 | la webcam si apre solo con viewer/snapshot attivi e si chiude dopo 5 s (il LED della Logitech = "qualcuno guarda") |
| `video.bitrate`, `video.x264_threads` | 1.5 Mb/s, 3 | libx264 ultrafast/zerolatency, Constrained Baseline |
| `cloud.url` | `""` | override del cloud (vince su `cloud.json` e sul payload BLE) — `PMCORE_CLOUD_URL` |
| `cloud.allow_http` | `false` (`mvp`: `true`) | accetta `http://` nel payload BLE |
| `ble.transport` | `bless` | `loopback` = BLE in-process per test/E2E |
| `ble.wifi` | `nmcli` | `fake` = nessuna modifica di rete (test/E2E) |
| `state.local_rate_hz` | 2 | stato §4.3 pubblicato da pm-core quando ROS è disattivato |

### Pipeline video sul Pi 5

Il Pi 5 non ha encoder H.264 hardware: MJPEG dalla webcam → decoder FFmpeg (`yuvj422p`) →
**un solo** passaggio swscale verso `yuv420p` nel thread encoder → libx264 → pacchetti H.264
condivisi da tutti i viewer (un solo encode). Nessun ndarray intermedio: il thread camera passa
direttamente l'`av.VideoFrame`. Se nessuno guarda, i pacchetti MJPEG non vengono nemmeno decodificati.
Se la camera ripiega su 640×480 l'encoder si riapre alla nuova risoluzione (nuovo SPS + IDR).
`petmate-doctor` misura gli fps reali di x264 alla risoluzione configurata.

### Comportamento verso l'app nel profilo MVP

* Media: una traccia video H.264. Se l'offerta contiene audio (app Flutter: audio `sendrecv`)
  la m-line audio viene risposta `inactive`; con offerta solo video + DataChannel (console browser)
  non c'è m-line audio. Il video del telefono (owner mode) non viene accettato (nessun display).
* Stato (2 Hz): oggetto §4.3 completo con valori segnaposto; `battery.soc/voltage/current`,
  `feeder.hopper_g`, `feeder.last_meal`, `pet.last_seen_ts/bearing_rad` = `null`;
  `mode:"manual"`, `activity:"idle"`, `pose.localized:false`, `pet.camera:"front"`.
* Ack:
  * `{"t":"camera","id":"rear"}` → `{"t":"ack","id":"rear","ok":false,"error":"camera_not_available","camera":"rear"}`
    (nota: per `camera` il campo `id` del comando è il nome della camera, quindi l'ack lo riporta);
    `front` → `{"t":"ack","id":"front","ok":true,"error":null,"camera":"front"}`.
  * Comandi ROS (`feed`, `teleop`, `led`, …) e `record_clip` → `{"t":"ack","id":…,"ok":false,"error":"not_supported"}` subito.
  * `snapshot` funziona (`{"t":"snapshot","jpeg_b64":…}`); se la webcam è scollegata → ack `camera_not_available`.
  * `volume`, `config_get/config_set` invariati (il volume è solo salvato nella config).
* ICE: STUN/TURN da `GET /v1/ice-servers` del cloud. I candidati host mDNS di Chrome
  (`<uuid>.local`) sono risolti da aioice (≥ 0.7, client mDNS proprio, senza avahi): testato in
  `tests/test_mvp_app.py::test_mdns_host_candidates_are_resolved`.

### Provisioning BLE (§7) — codici di errore

Caratteristica stato: `{"state":"error","error":<codice>,"ip":…}`. Dopo un errore la finestra resta
aperta con la stessa chiave: l'app può rimandare il payload (lo stato torna a `receiving`).
Dopo `done` l'advertising BLE si spegne (≈ 2 s, per far arrivare la notifica finale).

| Codice | Causa |
|---|---|
| `wifi_auth_failed` | password errata / handshake / SAE |
| `wifi_not_found` | SSID non visibile (reti nascoste: tentate con `802-11-wireless.hidden yes`) |
| `wifi_timeout` | associazione o DHCP oltre `ble.wifi_timeout_s` (45 s) |
| `wifi_unavailable` | radio Wi-Fi spenta/rfkill/paese Wi-Fi non impostato |
| `wifi_failed` | altro errore nmcli (vedi log) |
| `cloud_unreachable` | DNS/TCP/timeout/5xx verso `cloud_url` (fino a 4 tentativi con backoff) |
| `claim_invalid` | 400/404/409/410/422 da `/devices/register` (token errato, usato o scaduto) |
| `device_rejected` | 401/403: device_id/secret rifiutati (cloud senza `ALLOW_TOFU` e device non registrato) |
| `register_failed` | altro errore di registrazione |
| `decrypt` | decifratura fallita (chiave pubblica vecchia, device_id diverso) |
| `payload_invalid` | chunk/JSON/campi non validi (es. `http://` con `cloud.allow_http=false`, password < 8) |

Wi-Fi: viene creato un profilo temporaneo `petmate-wifi-new`; solo se si connette sostituisce
`petmate-wifi` (una password sbagliata non distrugge una configurazione funzionante). WPA3-only →
`key-mgmt sae`. Il `cloud_url` del payload (http o https) è salvato in `/var/lib/petmate/cloud.json`
e usato dal signaling (`http→ws`, `https→wss`). Per ripetere il wizard:
`sudo rm /var/lib/petmate/cloud.json && sudo systemctl restart petmate-core`.

### Strumenti

* `robot/tools/provision_cli.py` (PC/Mac/Windows, `pip install bleak cryptography`): fa da
  central BLE come l'app — login (o `--signup`) sul cloud, claim token, scansione `Petmate-XXXX`,
  lista reti viste dal robot, invio credenziali cifrate, stati in tempo reale.
  ```bash
  python3 robot/tools/provision_cli.py --cloud http://192.168.1.50:8000/v1 \
      --email dev@example.com --password secret --signup --ssid Casa
  ```
  Se il login avviene su `localhost`, passare `--robot-cloud-url http://<IP-LAN-del-PC>:8000/v1`:
  il robot non può raggiungere il localhost del PC (vale anche per la console web).
* `petmate-doctor` (`robot/scripts/petmate_doctor.sh`, modulo `pmcore.doctor`): identità,
  core.json, dipendenze, camera + cattura 3 s, velocità x264, Bluetooth, NetworkManager,
  paese Wi-Fi, servizio, cloud. La cattura richiede la webcam libera (nessuno streaming attivo).

### Test end-to-end senza radio né hardware

pm-core può girare in-process con camera sintetica FFmpeg, BLE "loopback" e Wi-Fi finto:

```python
from pmcore.app import App
from pmcore.ble import loopback
from pmcore.paths import Paths
from pmcore.settings import Settings
import provision_cli as cli   # robot/tools

st = Settings.load(None, env={}, overrides={
    "profile": "mvp",
    "camera": {"backend": "lavfi", "main_size": [640, 360]},
    "ble": {"transport": "loopback", "wifi": "fake"},
})
app = App(Paths(tmp_root), st, enable_hw=True)    # tmp_root/etc/petmate/identity.json richiesto
await app.start()
link = loopback.get()                               # = app.ble.server (stessa API del link bleak)
await cli.provision(link, "PetmateTest", "password1", claim_token, "http://127.0.0.1:8000/v1")
# poi: un peer utente si collega a ws://127.0.0.1:8000/v1/signal?token=<user JWT> e manda l'offer
```

`FakeWifi` simula gli errori: password `wrongpassword` → `wifi_auth_failed`, SSID
`PetmateTest-NotFound` → `wifi_not_found`, `PetmateTest-Timeout` → `wifi_timeout`.
Da riga di comando: `PETMATE_ROOT=/tmp/pm PMCORE_PROFILE=mvp PMCORE_CAMERA_BACKEND=lavfi
PMCORE_BLE_TRANSPORT=loopback PMCORE_BLE_WIFI=fake PMCORE_CLOUD_URL=http://127.0.0.1:8000/v1
python3 -m pmcore --log-format text`.
Esempi completi: `tests/test_provisioning_mvp.py` (cloud finto aiohttp) e `tests/test_mvp_app.py`.
