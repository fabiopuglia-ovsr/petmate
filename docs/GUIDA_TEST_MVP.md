# Guida al test MVP: login, messa in rete, streaming

Servono tre cose nella stessa rete Wi-Fi di casa: un **PC** (fa da cloud), la **Raspberry Pi 5** con la
webcam Logitech, un **telefono Android**. Tempo stimato: 1 ora la prima volta.

```
Telefono (app) ──HTTP──► PC: cloud :8000 ◄──HTTP/WS── Raspberry Pi 5 + Logitech
      ▲                                                     │
      └───────────── video WebRTC diretto in LAN ───────────┘
      └──── Bluetooth (solo durante il wizard) ─────────────┘
```

## 1. PC: avviare il cloud

Serve Python 3.11 o più recente (oppure Docker).

```bash
git clone https://github.com/fabiopuglia-ovsr/petmate.git
cd petmate/cloud
./run_dev.sh                # macOS / Linux. Windows: vedi cloud/README.md (PowerShell)
```

All'avvio stampa l'indirizzo da usare, per esempio `http://192.168.1.50:8000/v1`. Annotalo: è
l'**indirizzo del server** per app e robot.

Controlli:
- dal browser del **telefono** apri `http://192.168.1.50:8000/v1/health` → deve rispondere `{"ok":true}`.
  Se non risponde, il firewall del PC blocca la porta 8000: consentila per le reti private.
- Il PC non deve andare in standby durante il test.

Alternativa con Docker: `LAN_HOSTS=192.168.1.50 docker compose -f deploy/docker-compose.dev.yml up --build`.

## 2. Raspberry Pi 5: installare il software

1. Con **Raspberry Pi Imager** scrivi *Raspberry Pi OS (64-bit) Bookworm* sulla microSD. Nelle impostazioni
   dell'Imager abilita **SSH** e imposta utente e password. **Non** configurare il Wi-Fi: lo farà il wizard.
2. Collega la Pi al router con un **cavo Ethernet** (serve internet per l'installazione), collega la
   **webcam Logitech** a una porta USB blu e accendi.
3. Dal PC: `ssh utente@raspberrypi.local`, poi:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/fabiopuglia-ovsr/petmate.git
cd petmate
sudo robot/scripts/install_mvp.sh          # 10–15 minuti la prima volta
sudo petmate-doctor                        # deve dare PASS su camera, encoder, Bluetooth, identità
```

`petmate-doctor` mostra anche l'ID del robot (es. `PM-A1B2C3`) e quanti fotogrammi al secondo la Pi
riesce a codificare: sopra 25 fps a 1280×720 lo streaming sarà fluido.

4. **Stacca il cavo Ethernet** (così il test del wizard è reale) e lascia la Pi accesa: non essendo ancora
   associata a nessun account, si annuncia via Bluetooth come `Petmate-XXXX`.

Log in tempo reale: `journalctl -u petmate-core -f`.

## 3. Telefono Android: installare l'app

1. Apri <https://github.com/fabiopuglia-ovsr/petmate/actions>, entra nell'ultima esecuzione **verde** di "CI",
   in fondo alla pagina scarica l'artefatto **petmate-android-apk** (serve essere loggati su GitHub).
2. È uno .zip: estrai il file `petmate-xxxxxxx.apk` e trasferiscilo sul telefono (cavo, Drive, email).
3. Sul telefono apri l'APK e consenti "Installa app sconosciute" per l'app da cui lo apri.
   Se Play Protect avvisa, scegli "Installa comunque" (l'app è firmata con chiave di sviluppo).

## 4. Test delle tre funzioni

**Login**
1. Apri Petmate → *Impostazioni avanzate* → **Server**: `http://192.168.1.50:8000/v1` → *Verifica*.
2. *Registrati* con email e password, poi esci e rientra con *Accedi*.

**Wizard di messa in rete** (telefono vicino alla Pi, Bluetooth attivo)
1. *I miei robot* → **Aggiungi robot**. Concedi il permesso "Dispositivi nelle vicinanze".
2. Seleziona `Petmate-XXXX`, scegli la rete Wi-Fi di casa e inserisci la password.
3. Attendi "Robot online! Apro la diretta…": la Pi si collega al Wi-Fi, si registra sul cloud e l'app apre
   direttamente il Live. Nella lista il robot ha il pallino verde.
4. Prova anche l'errore: ripeti con una password sbagliata → messaggio "password errata" e possibilità di
   riprovare. (Per rifare il wizard da capo: `sudo rm /var/lib/petmate/cloud.json && sudo systemctl restart petmate-core`.)

**Streaming**
1. Tocca il robot → **Live**: il video della Logitech deve comparire in 2–4 secondi.
2. Controlla la barra in alto (qualità del collegamento) e prova *Riconnetti*.

In alternativa al telefono, dal PC che ospita il cloud: Chrome o Edge su `http://localhost:8000/console/`
(console di test con login, wizard via Web Bluetooth e live).

## Se qualcosa non va

| Sintomo | Causa probabile e rimedio |
|---|---|
| L'app non raggiunge il server | Telefono su rete diversa (rete ospiti, dati mobili), firewall del PC sulla porta 8000, indirizzo IP del PC cambiato |
| Il wizard non trova `Petmate-XXXX` | Robot già associato (vedi comando per rifare il wizard); Bluetooth spento sulla Pi: `bluetoothctl show`; permesso "Dispositivi nelle vicinanze" negato; su Android ≤ 11 attiva la Localizzazione |
| "Password errata" con password giusta | Rete solo WPA3 o con portale: prova una rete WPA2; controlla il paese Wi-Fi (`install_mvp.sh --country IT`) |
| Robot collegato ma offline | La Pi non raggiunge il PC: `ping 192.168.1.50` dalla Pi; log `journalctl -u petmate-core -f` |
| Live resta nero | `sudo petmate-doctor` (camera vista?); webcam su porta USB 2 lenta → spostala sulla porta blu; riduci la risoluzione in `/etc/petmate/core.json` |
| Video a scatti | La Pi non ce la fa a codificare: guarda gli fps di `petmate-doctor`, imposta 960×540 o 20 fps in `core.json` |

## Prossimo passo: streaming da fuori casa

Con il telefono su rete mobile il cloud deve essere raggiungibile da internet (server cloud o porta inoltrata
sul router) e serve un server TURN (`docker compose -f deploy/docker-compose.dev.yml --profile turn up`).
Poiché il repository è pubblico, prima di esporre il cloud cambia `JWT_SECRET` e `TURN_SECRET` rispetto ai
valori di sviluppo in `cloud/deploy/dev.env`.
