# Petmate — repository EVT1

Robot domestico per animali (cura, gioco, comunicazione a distanza).
Il progetto tecnico completo è nel documento "Petmate — Progetto tecnico e scheda tecnica (EVT1)";
il contratto tra i sottosistemi è in [`docs/INTERFACES.md`](docs/INTERFACES.md) (v0.2).

| Cartella | Contenuto | Come si prova |
|---|---|---|
| `firmware/` | Firmware RP2350 (Pico 2), C++17 / Pico SDK 2.1 | `cmake -B build -DPICO_BOARD=pico2 && cmake --build build` · test host in `firmware/test/` |
| `robot/ros2_ws/` | ROS 2 Jazzy: base, description, bridge ZeroMQ, behavior, bringup, interfaces | `pytest` nei pacchetti · `robot/docker/compose.sh build` |
| `robot/core/` | pm-core (camere, Hailo, WebRTC, audio, display, BLE, cloud) | `pip install -e . && pytest` |
| `robot/scripts/install_host.sh` | Installazione su Raspberry Pi OS Bookworm 64 bit | `sudo ./install_host.sh` poi reboot |
| `robot/systemd/`, `robot/docker/` | Servizi e container | — |
| `cloud/` | Backend FastAPI + coturn + Caddy | `pytest` · `cloud/deploy/docker-compose.yml` |
| `app/` | App Flutter iOS/Android | `flutter create . && flutter test` |

## Avvio rapido sul robot
1. Flash `firmware/prebuilt/petmate_fw.uf2 (o ricompilato in firmware/build/)` sul Pico 2 (BOOTSEL).
2. Copiare `/etc/petmate/identity.json` (`device_id`, `device_secret`) e registrarlo sul cloud:
   `python -m app.cli add-device PM-000123 <secret>`.
3. `sudo robot/scripts/install_host.sh` e reboot.
4. Aprire l'app, "Aggiungi robot": il robot è visibile in BLE come `Petmate-XXXX`.

## Stato
Codice scritto e testato su host; **mai eseguito sull'hardware reale**. App Flutter non compilata
nell'ambiente di sviluppo (Flutter non disponibile). Vedi "Rischi aperti" nel documento di progetto.
