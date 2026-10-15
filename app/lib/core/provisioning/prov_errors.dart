// Mapping of provisioning error codes (BLE status characteristic §7
// `{"state":"error","error":<code>}` and app-side failures) to Italian
// messages and the retry action offered to the user. Pure Dart.

/// What "Riprova" does after an error.
enum ProvRetry {
  /// Back to the Wi-Fi form on the still-open BLE link (a fresh claim token
  /// is requested on every send).
  wifi,

  /// Start over from the BLE scan.
  rescan,

  /// Open the system settings (permission permanently denied).
  settings,
}

class ProvErrorInfo {
  final String message;
  final ProvRetry retry;
  const ProvErrorInfo(this.message, this.retry);
}

/// Device-side codes reported in the status characteristic.
///
/// Canonical (MVP spec): `wifi_auth_failed`, `wifi_not_found`, `wifi_timeout`,
/// `cloud_unreachable`, `claim_invalid`. Aliases emitted by the current
/// pm-core build are accepted too (`wifi_connect_failed`, `register_failed`,
/// `claim_token_invalid`, `decrypt: …`, `chunk: …`).
ProvErrorInfo deviceProvError(String? raw) {
  final code = _codeOf(raw);
  switch (code) {
    case 'wifi_auth_failed' || 'wrong_password' || 'auth':
      return const ProvErrorInfo(
          'Password Wi-Fi errata. Controlla maiuscole e minuscole e riprova.', ProvRetry.wifi);
    case 'wifi_not_found' || 'ssid_not_found':
      return const ProvErrorInfo(
          'Il robot non trova questa rete Wi-Fi. Verifica il nome (SSID) esatto e che il router sia '
          'nel raggio del robot.',
          ProvRetry.wifi);
    case 'wifi_timeout' || 'wifi_connect_failed':
      return const ProvErrorInfo(
          'Il robot non è riuscito a collegarsi al Wi-Fi. Controlla password e segnale, poi riprova '
          '(la Raspberry Pi 5 supporta sia 2,4 GHz sia 5 GHz).',
          ProvRetry.wifi);
    case 'cloud_unreachable' || 'register_failed':
      return const ProvErrorInfo(
          'Il robot è connesso al Wi-Fi ma non raggiunge il server. Verifica l\'indirizzo del server, '
          'che il PC sia sulla stessa rete e che il firewall consenta la porta 8000.',
          ProvRetry.wifi);
    case 'claim_invalid' || 'claim_token_invalid' || 'claim_expired':
      return const ProvErrorInfo(
          'Codice di associazione non valido o scaduto. Riprova: ne verrà generato uno nuovo.',
          ProvRetry.wifi);
    case 'decrypt' || 'chunk':
      return const ProvErrorInfo(
          'Errore di sicurezza nella trasmissione Bluetooth. Riprova la connessione al robot.',
          ProvRetry.rescan);
    case '':
      return const ProvErrorInfo('Configurazione non riuscita.', ProvRetry.wifi);
    default:
      return ProvErrorInfo('Configurazione non riuscita ($code).', ProvRetry.wifi);
  }
}

/// App-side failure codes (BLE stack, permissions, cloud, timeouts).
ProvErrorInfo appProvError(String code, {String? detail}) => switch (code) {
      'permission' => const ProvErrorInfo(
          'Serve il permesso "Dispositivi nelle vicinanze" (Bluetooth) per trovare il robot.', ProvRetry.rescan),
      'permission_permanent' => const ProvErrorInfo(
          'Il permesso Bluetooth è stato negato. Abilitalo dalle impostazioni dell\'app.', ProvRetry.settings),
      'location_permission' => const ProvErrorInfo(
          'Su questa versione di Android la ricerca Bluetooth richiede il permesso di posizione.',
          ProvRetry.rescan),
      'location_off' => const ProvErrorInfo(
          'Su questa versione di Android la ricerca Bluetooth richiede la localizzazione attiva. '
          'Attivala dalle impostazioni rapide e riprova.',
          ProvRetry.rescan),
      'bluetooth_off' => const ProvErrorInfo('Attiva il Bluetooth per continuare.', ProvRetry.rescan),
      'bluetooth_unauthorized' => const ProvErrorInfo(
          'Petmate non ha accesso al Bluetooth. Consentilo in Impostazioni > Petmate > Bluetooth.',
          ProvRetry.settings),
      'unsupported' => const ProvErrorInfo(
          'Questo telefono non supporta il Bluetooth Low Energy.', ProvRetry.rescan),
      'connect' => const ProvErrorInfo(
          'Impossibile connettersi al robot. Avvicinati (entro 2-3 metri) e riprova.', ProvRetry.rescan),
      'disconnected' => const ProvErrorInfo(
          'Connessione Bluetooth persa. Avvicinati al robot e riprova.', ProvRetry.rescan),
      'gatt' => const ProvErrorInfo(
          'Il robot non risponde come previsto. Riavvialo (o tieni premuto il pulsante zampa 5 s) e riprova.',
          ProvRetry.rescan),
      'write' => const ProvErrorInfo(
          'Invio delle credenziali al robot non riuscito. Avvicinati e riprova.', ProvRetry.rescan),
      'crypto' => const ProvErrorInfo(
          'Chiave del robot non valida. Riavvia il robot e riprova.', ProvRetry.rescan),
      'claim_api' => ProvErrorInfo(
          'Impossibile ottenere il codice di associazione dal server${detail == null ? '' : ' ($detail)'}. '
          'Controlla la connessione del telefono.',
          ProvRetry.wifi),
      'timeout' => const ProvErrorInfo(
          'Il robot non ha completato la configurazione entro il tempo previsto. Controlla che la rete '
          'Wi-Fi sia raggiungibile dal robot e riprova.',
          ProvRetry.rescan),
      _ => ProvErrorInfo('Qualcosa è andato storto${detail == null ? '' : ' ($detail)'}. Riprova.',
          ProvRetry.rescan),
    };

/// `"decrypt: InvalidTag"` → `decrypt`; trims and lower-cases.
String _codeOf(String? raw) {
  final s = (raw ?? '').trim().toLowerCase();
  final i = s.indexOf(':');
  return i >= 0 ? s.substring(0, i).trim() : s;
}
