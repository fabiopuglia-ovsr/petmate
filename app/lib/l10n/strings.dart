// Centralized UI strings (Italian). l10n-ready: every user-visible string
// lives here behind a getter/method so it can be moved to ARB files
// (flutter gen-l10n) without touching widgets. Add `StringsEn` implementing
// the same members to ship English.
//
// Pure Dart (no Flutter imports): unit-testable.
import '../core/env.dart' show HealthStatus;

abstract final class S {
  // ---- generic ----
  static const appName = 'Petmate';
  static const ok = 'OK';
  static const cancel = 'Annulla';
  static const save = 'Salva';
  static const saved = 'Salvato';
  static const delete = 'Elimina';
  static const retry = 'Riprova';
  static const close = 'Chiudi';
  static const next = 'Avanti';
  static const back = 'Indietro';
  static const add = 'Aggiungi';
  static const edit = 'Modifica';
  static const loading = 'Caricamento…';
  static const discard = 'Scarta modifiche';
  static const genericError = 'Qualcosa è andato storto. Riprova.';
  static const networkError = 'Connessione assente o server non raggiungibile.';
  static const invalidServerResponse = 'Risposta non valida: l\'indirizzo del server non sembra il cloud Petmate.';
  static const sessionExpired = 'Sessione scaduta, accedi di nuovo.';
  static const deviceOffline = 'Il robot è offline.';
  static const requiresLive = 'Apri la diretta per usare questa funzione.';
  static String sentVia(bool live) => live ? 'Inviato al robot' : 'Inviato tramite cloud';

  // ---- auth ----
  static const welcome = 'Ciao! Prenditi cura del tuo amico, anche da lontano.';
  static const login = 'Accedi';
  static const register = 'Registrati';
  static const logout = 'Esci';
  static const email = 'Email';
  static const password = 'Password';
  static const passwordConfirm = 'Conferma password';
  static const noAccount = 'Non hai un account? Registrati';
  static const haveAccount = 'Hai già un account? Accedi';
  static const invalidEmail = 'Inserisci un indirizzo email valido';
  static const passwordTooShort = 'Almeno 8 caratteri';
  static const passwordMismatch = 'Le password non coincidono';
  static const wrongCredentials = 'Email o password non corretti';
  static const emailTaken = 'Esiste già un account con questa email';
  static const invalidData = 'Dati non validi. Controlla email e password.';
  static const showPassword = 'Mostra password';
  static const hidePassword = 'Nascondi password';
  static const advancedSettings = 'Impostazioni avanzate';
  static const serverLabel = 'Server';
  static const serverHelp =
      'Indirizzo del cloud Petmate, es. http://192.168.1.50:8000/v1 (IP del PC nella stessa rete Wi-Fi).';
  static const serverCheck = 'Verifica';
  static const serverReset = 'Predefinito';
  static const serverProblem = 'Il server non è raggiungibile: controlla "Impostazioni avanzate".';
  static String healthMessage(HealthStatus s, String? detail) => switch (s) {
        HealthStatus.ok => 'Server raggiungibile.',
        HealthStatus.invalidUrl => 'Indirizzo non valido. Esempio: http://192.168.1.50:8000/v1',
        HealthStatus.unreachable =>
          'Server non raggiungibile. Verifica IP e porta, che il telefono sia sulla stessa rete Wi-Fi del PC '
              'e che il firewall del PC consenta la porta 8000.',
        HealthStatus.timeout => 'Il server non risponde (timeout). Verifica IP, porta e firewall del PC.',
        HealthStatus.notPetmate =>
          'A questo indirizzo risponde qualcosa che non è il cloud Petmate${detail == null ? '' : ' ($detail)'}. '
              'Controlla porta e percorso /v1.',
      };

  // ---- devices ----
  static const myRobots = 'I miei robot';
  static const noRobots = 'Nessun robot associato.\nAggiungine uno per iniziare!';
  static const addRobot = 'Aggiungi robot';
  static const online = 'Online';
  static const offline = 'Offline';
  static String lastSeen(String when) => 'Visto l\'ultima volta $when';
  static const justNow = 'ora';
  static String minutesAgo(int m) => '$m min fa';
  static String todayAt(String hm) => 'oggi alle $hm';
  static const rename = 'Rinomina';
  static const robotName = 'Nome del robot';
  static const removeRobot = 'Rimuovi robot';
  static const robotOfflineTap = 'Il robot è offline: accendilo o rifai la configurazione Wi-Fi.';
  static const serverDisconnected = 'Non connesso al server: lo stato online potrebbe non essere aggiornato.';
  static String removeConfirm(String name) =>
      'Vuoi davvero rimuovere "$name" dal tuo account? Dovrai rifare la configurazione per usarlo di nuovo.';
  static String firmware(String fw) => 'Firmware $fw';

  // ---- device home ----
  static const live = 'Diretta';
  static const schedule = 'Programma';
  static const activity = 'Attività';
  static const settings = 'Impostazioni';
  static const quickFeed = 'Dai da mangiare';
  static const quickPlay = 'Gioca';
  static const quickDock = 'Torna alla base';
  static const quickActions = 'Azioni rapide';

  // ---- onboarding ----
  static const onbTitle = 'Configura Petmate';
  static const onbIntro =
      'Accendi il robot. Se è già stato configurato, tieni premuto il pulsante a forma di zampa sul dorso per 5 secondi finché l\'anello LED non lampeggia.';
  static const onbStartScan = 'Cerca robot';
  static const onbPreparing = 'Controllo Bluetooth e permessi…';
  static const onbScanning = 'Ricerca dei robot "Petmate-…" in corso…';
  static const onbNoneFound =
      'Nessun robot trovato. Avvicinati, verifica che il robot sia acceso e in modalità configurazione '
      '(il servizio pm-core deve essere attivo), poi riprova.';
  static const onbScanAgain = 'Cerca di nuovo';
  static const onbConnecting = 'Connessione al robot…';
  static const onbNoNetworks = 'Il robot non vede reti Wi-Fi. Aggiorna l\'elenco o inserisci la rete a mano.';
  static const onbHiddenHint = 'Rete nascosta o non in elenco';
  static const onbPasswordRule = 'Da 8 a 63 caratteri';
  static const onbKeepClose = 'Tieni il telefono vicino al robot fino al termine.';
  static const onbWaitingOnline = 'Il robot è sul Wi-Fi. Attendo che si colleghi al server…';
  static const onbOnline = 'Robot online! Apro la diretta…';
  static const onbOnlineTimeoutTitle = 'Il robot non risulta ancora online';
  static String onbOnlineTimeoutBody(String server) =>
      'La configurazione Wi-Fi è stata inviata, ma dopo 60 secondi il robot non è collegato al server.\n\n'
      '• Il robot deve raggiungere $server: il PC con il cloud deve essere acceso, sulla stessa rete, '
      'con la porta 8000 aperta nel firewall (non usare "localhost" o 127.0.0.1 come indirizzo del server).\n'
      '• Controlla che il robot sia ancora acceso e vicino al router.\n'
      '• Sul robot: "journalctl -u petmate-core -f" mostra l\'esito della registrazione.';
  static const onbCheckAgain = 'Controlla di nuovo';
  static const onbRetryWifi = 'Riprova con un\'altra rete o password';
  static const onbStartOver = 'Ricomincia dalla ricerca';
  static const openSettings = 'Apri impostazioni';
  static const onbChooseWifi = 'Scegli la rete Wi-Fi di casa';
  static const onbRescanWifi = 'Aggiorna elenco';
  static const onbOtherNetwork = 'Altra rete…';
  static const onbSsid = 'Nome rete (SSID)';
  static const onbWifiPassword = 'Password Wi-Fi';
  static const onbOpenNetwork = 'Rete aperta (senza password)';
  static const onbBands = 'Il robot (Raspberry Pi 5) si collega a reti Wi-Fi a 2,4 GHz e 5 GHz.';
  static const onbSend = 'Collega';
  static const onbSuccess = 'Fatto! Petmate è online.';
  static const onbSuccessBody = 'Ora puoi dargli un nome e impostare pasti e giochi.';
  static const onbGoToRobot = 'Vai al robot';
  static const permMic = 'Serve il permesso microfono per parlare con il tuo pet.';
  static const micGrantedReconnect = 'Microfono consentito: chiudi e riapri la diretta per attivarlo.';
  static const permBle = 'Serve il permesso Bluetooth per trovare il robot.';
  static const bluetoothOff = 'Attiva il Bluetooth per continuare.';
  static const bleUnsupported = 'Questo dispositivo non supporta il Bluetooth Low Energy.';
  static String provState(String s) => switch (s) {
        'idle' => 'In attesa…',
        'receiving' => 'Ricezione credenziali…',
        'connecting' => 'Connessione al Wi-Fi…',
        'wifi_ok' => 'Wi-Fi connesso',
        'registering' => 'Registrazione nel cloud…',
        'done' => 'Completato',
        'error' => 'Errore',
        _ => s,
      };

  // ---- live ----
  static const liveConnecting = 'Connessione alla diretta…';
  static const liveFailed = 'Diretta non disponibile';
  static const liveReconnect = 'Riconnetti';
  static const camFront = 'Anteriore';
  static const camRear = 'Posteriore';
  static const switchCamera = 'Cambia camera';
  static const pushToTalk = 'Tieni premuto per parlare';
  static const talking = 'Stai parlando…';
  static const duplex = 'Conversazione continua';
  static const mute = 'Silenzia audio robot';
  static const unmute = 'Attiva audio robot';
  static const showFace = 'Mostra il mio viso sul robot';
  static const feed = 'Cibo';
  static String feedGrams(int g) => 'Eroga $g g';
  static const feedAbort = 'Interrompi erogazione';
  static const launchBall = 'Lancia pallina';
  static const teaser = 'Gioco piuma';
  static const led = 'Luci';
  static const goDock = 'Alla base';
  static const goPet = 'Cerca il pet';
  static const goPoi = 'Vai a…';
  static const snapshot = 'Foto';
  static const recordClip = 'Registra clip';
  static String clipRecording(int s) => 'Registrazione clip di $s s avviata';
  static const snapshotSaved = 'Foto scattata';
  static const estop = 'STOP';
  static const estopRelease = 'Sblocca';
  static const estopActive = 'Arresto di emergenza attivo';
  static String battery(int soc) => '$soc%';
  static const charging = 'In carica';
  static const docked = 'Sulla base';
  static const quality = 'Qualità connessione';
  static const qualityGood = 'Ottima';
  static const qualityFair = 'Discreta';
  static const qualityPoor = 'Scarsa';
  static const qualityUnknown = '—';
  static const relayed = 'via relay';
  static const speed = 'Velocità';
  static const noVideo = 'In attesa del video…';
  static const liveConnected = 'In diretta';
  static const liveWaitingVideo = 'Connesso, in attesa del video…';
  static const snapshotFailed = 'Foto non disponibile: apri la diretta e riprova.';
  static String liveError(String? e) => switch (e) {
        'answer_timeout' || 'timeout' => 'Il robot non ha risposto entro 15 secondi. Verifica che sia acceso e online.',
        'peer_offline' || 'device_offline' => 'Il robot non è collegato al server (offline).',
        'ice_failed' =>
          'Connessione video non riuscita. Telefono e robot devono essere sulla stessa rete, '
              'oppure serve un server TURN per la connessione da remoto.',
        'remote_bye' => 'La diretta è stata chiusa dal robot.',
        'signaling_down' || 'signaling_error' => 'Connessione al server di segnalazione non disponibile.',
        'forbidden' => 'Questo robot non è associato al tuo account.',
        'datachannel_closed' => 'Connessione con il robot interrotta.',
        _ => liveFailed,
      };
  static String activityLabel(String a) => switch (a) {
        'idle' => 'A riposo',
        'patrolling' => 'In pattuglia',
        'feeding' => 'Sta servendo il pasto',
        'playing' => 'Sta giocando',
        'docking' => 'Rientro alla base',
        'undocking' => 'Esce dalla base',
        'teleop' => 'Guida manuale',
        'seeking_pet' => 'Cerca il pet',
        'mapping' => 'Mappatura',
        'sleeping' => 'Dorme',
        'error' => 'Errore',
        _ => a,
      };
  static String ledModeLabel(String m) => switch (m) {
        'solid' => 'Fisso',
        'breathe' => 'Respiro',
        'rainbow' => 'Arcobaleno',
        'chase' => 'Rincorsa',
        'off' => 'Spente',
        _ => m,
      };
  static String teaseLabel(String p) => switch (p) {
        'wiggle' => 'Vibrazione',
        'sweep' => 'Spazzata',
        'random' => 'Casuale',
        'stop' => 'Ferma',
        _ => p,
      };
  static String feederStateLabel(String s) => switch (s) {
        'idle' => 'Pronto',
        'dispensing' => 'Erogazione…',
        'done' => 'Erogato',
        'jam' => 'Inceppato',
        'hopper_empty' => 'Serbatoio vuoto',
        'aborted' => 'Interrotto',
        _ => s,
      };
  static String hopper(double g) => 'Serbatoio: ${g.round()} g';
  static String balls(int n) => 'Palline: $n';

  // ---- schedule ----
  static const meals = 'Pasti';
  static const addMeal = 'Aggiungi pasto';
  static const time = 'Ora';
  static const grams = 'Grammi';
  static const days = 'Giorni';
  static const dailyMax = 'Massimo giornaliero';
  static const portionMax = 'Porzione massima';
  static const feedingPoi = 'Punto di erogazione';
  static const none = 'Nessuno';
  static String perDayTotal(int g, int max) => 'Totale programmato: fino a $g g/giorno (max $max g)';
  static const playSessions = 'Sessioni di gioco';
  static const addSession = 'Aggiungi sessione';
  static const durationMin = 'Durata (min)';
  static const autoPlay = 'Gioco automatico';
  static const autoPlayHint = 'Petmate propone il gioco quando vede il tuo pet annoiato';
  static const launchPower = 'Potenza lancio';
  static const games = 'Giochi abilitati';
  static const quietHours = 'Ore di silenzio';
  static const from = 'Dalle';
  static const to = 'Alle';
  static const patrol = 'Pattuglia';
  static const patrolEnabled = 'Pattuglia automatica';
  static String patrolEvery(int m) => 'Ogni $m minuti';
  static String gameLabel(String g) => switch (g) {
        'chase_ball' => 'Rincorri la pallina',
        'teaser' => 'Piuma',
        'screen_birds' => 'Uccellini sullo schermo',
        'laser_dots' => 'Puntini luminosi',
        _ => g,
      };
  static const weekdaysShort = ['L', 'M', 'M', 'G', 'V', 'S', 'D'];
  static const weekdaysLong = ['Lunedì', 'Martedì', 'Mercoledì', 'Giovedì', 'Venerdì', 'Sabato', 'Domenica'];
  static String problem(String code) => switch (code) {
        'portion_max_range' => 'La porzione massima deve essere tra 1 e 60 g.',
        'meal_time' => 'Orario pasto non valido.',
        'meal_grams' => 'Ogni pasto deve essere tra 1 g e la porzione massima.',
        'meal_days' => 'Ogni pasto deve avere almeno un giorno.',
        'daily_max_exceeded' => 'I pasti programmati superano il massimo giornaliero.',
        'play_time' => 'Orario di gioco non valido.',
        'play_duration' => 'La durata del gioco deve essere almeno 1 minuto.',
        'play_days' => 'Ogni sessione deve avere almeno un giorno.',
        'launch_power_range' => 'La potenza di lancio deve essere tra 20 e 70.',
        'quiet_hours' => 'Ore di silenzio non valide.',
        'volume_range' => 'Volume non valido.',
        _ => code,
      };

  // ---- settings ----
  static const petProfile = 'Profilo del pet';
  static const petName = 'Nome';
  static const species = 'Specie';
  static const cat = 'Gatto';
  static const dog = 'Cane';
  static const weightKg = 'Peso (kg)';
  static const notifications = 'Notifiche';
  static const notifMeals = 'Pasti';
  static const notifActivity = 'Attività e giochi';
  static const notifBattery = 'Batteria';
  static const notifPet = 'Pet avvistato';
  static const privacy = 'Privacy';
  static const streamLed = 'LED acceso durante la diretta';
  static const rearCamera = 'Consenti diretta dalla camera posteriore';
  static const audio = 'Audio';
  static const volume = 'Volume altoparlante';
  static const mapPoi = 'Mappa e luoghi';
  static const mapHint =
      'Per creare la mappa avvia la mappatura e guida Petmate per tutte le stanze dalla diretta, poi salva.';
  static const startMapping = 'Avvia mappatura';
  static const stopMapping = 'Ferma mappatura';
  static const saveMap = 'Salva mappa';
  static const clearMap = 'Cancella mappa';
  static const clearMapConfirm = 'Cancellare la mappa e tutti i luoghi salvati?';
  static const mappingActive = 'Mappatura in corso';
  static const pois = 'Luoghi salvati';
  static const noPois = 'Nessun luogo salvato';
  static const savePoiHere = 'Salva posizione attuale';
  static const poiName = 'Nome del luogo (es. cucina)';
  static const goThere = 'Vai qui';
  static const timezone = 'Fuso orario';
  static const robotMode = 'Modalità';
  static String modeLabel(String m) => switch (m) {
        'auto' => 'Automatica',
        'manual' => 'Manuale',
        'sleep' => 'Riposo',
        _ => m,
      };
  static const account = 'Account';
  static const unsaved = 'Hai modifiche non salvate';

  // ---- activity ----
  static const events = 'Eventi';
  static const clips = 'Clip';
  static const noEvents = 'Ancora nessun evento.';
  static const noClips = 'Ancora nessuna clip.';
  static String playDuration(int min, int? activeMin) =>
      activeMin == null ? '$min min' : '$min min (attivo $activeMin min)';
  static const today = 'Oggi';
  static const yesterday = 'Ieri';
  static String eventTitle(String type) => switch (type) {
        'meal_dispensed' => 'Pasto servito',
        'meal_failed' => 'Pasto non riuscito',
        'feeder_jam' => 'Erogatore inceppato',
        'hopper_low' => 'Serbatoio quasi vuoto',
        'pet_detected' => 'Pet avvistato',
        'play_started' => 'Gioco iniziato',
        'play_ended' => 'Gioco terminato',
        'battery_low' => 'Batteria scarica',
        'docked' => 'Tornato alla base',
        'undocked' => 'Uscito dalla base',
        'stuck' => 'Robot bloccato',
        'cliff' => 'Rilevato un gradino',
        'estop' => 'Arresto di emergenza',
        'nav_failed' => 'Destinazione non raggiunta',
        _ => type,
      };
}
