# Petmate — MVP 1: login, messa in rete, streaming

Obiettivo: tre funzioni provate end-to-end sull'hardware reale, prima di tutto il resto.

1. **Login** — registrazione/accesso utente sul cloud (app Flutter; console web di test).
2. **Wizard di messa in rete** — l'app trova la Raspberry Pi 5 via BLE, le passa Wi-Fi + codice di
   associazione cifrati (INTERFACES.md §7); la Pi si collega e si registra sul cloud.
3. **Streaming** — video dalla **webcam USB Logitech** collegata alla Pi verso l'app (WebRTC, §4.1),
   in LAN e da remoto.

## Configurazione di prova (decisioni condivise da tutti i componenti)

| Elemento | Scelta MVP |
|---|---|
| Cloud | sul PC/portatile dello sviluppatore nella stessa LAN, HTTP porta 8000, `ALLOW_TOFU=true` (nessun registro di fabbrica). Base URL tipo `http://192.168.1.50:8000/v1` |
| Console web di test | servita dal cloud su `GET /console/` (stessa origine dell'API). Web Bluetooth funziona solo in contesto sicuro: aprirla come `http://localhost:8000/console/` sul PC che ospita il cloud, con Chrome/Edge |
| Robot | Raspberry Pi 5, Raspberry Pi OS Bookworm 64 bit, NetworkManager, BlueZ. Nessun MCU, LiDAR, Hailo, display o ROS |
| Camera | webcam USB UVC Logitech (C270/C920/C922/Brio…), MJPEG 1280×720@30 se disponibile, altrimenti YUYV 640×480; dispositivo `auto` = primo `/dev/v4l/by-id/*Logitech*-video-index0`, poi primo `/dev/video*` che sia una capture UVC |
| pm-core | `/etc/petmate/core.json` con `"profile": "mvp"`: detector, display, audio, clip, ZMQ/ROS disattivati; camera `backend: "uvc"`; solo camera `front` |
| Identità | `sudo robot/scripts/make_identity.sh` → `/etc/petmate/identity.json` con `device_id = PM-` + ultime 6 cifre hex del seriale Pi, `device_secret` casuale 32 byte base64url |
| Audio | fuori dall'MVP (il microfono della Logitech potrà essere aggiunto dopo) |
| Remoto | STUN pubblico; TURN (coturn) opzionale, consigliato per il test fuori casa |

Backend camera di pm-core (`camera.backend`): `picamera2` (prodotto), `uvc` (MVP, PyAV + V4L2), `lavfi` (test:
sorgente sintetica FFmpeg, stesso percorso di codice di `uvc`), `synthetic` (numpy, già esistente).
