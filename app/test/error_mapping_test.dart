import 'package:flutter_test/flutter_test.dart';
import 'package:petmate/core/env.dart';
import 'package:petmate/core/provisioning/prov_crypto.dart';
import 'package:petmate/core/provisioning/prov_errors.dart';
import 'package:petmate/l10n/strings.dart';

void main() {
  group('device provisioning errors (status characteristic)', () {
    test('canonical MVP codes → Italian message + retry on Wi-Fi form', () {
      final cases = {
        'wifi_auth_failed': 'Password Wi-Fi errata',
        'wifi_not_found': 'non trova questa rete',
        'wifi_timeout': 'non è riuscito a collegarsi al Wi-Fi',
        'cloud_unreachable': 'non raggiunge il server',
        'claim_invalid': 'Codice di associazione',
      };
      cases.forEach((code, fragment) {
        final i = deviceProvError(code);
        expect(i.message, contains(fragment), reason: code);
        expect(i.retry, ProvRetry.wifi, reason: code);
      });
    });

    test('aliases emitted by the current pm-core build', () {
      expect(deviceProvError('wifi_connect_failed').message, deviceProvError('wifi_timeout').message);
      expect(deviceProvError('register_failed').message, deviceProvError('cloud_unreachable').message);
      expect(deviceProvError('claim_token_invalid').message, deviceProvError('claim_invalid').message);
    });

    test('prefixed crypto errors restart from the scan', () {
      expect(deviceProvError('decrypt: InvalidTag').retry, ProvRetry.rescan);
      expect(deviceProvError('chunk: bad first flag').retry, ProvRetry.rescan);
    });

    test('case/space tolerant, unknown and empty codes', () {
      expect(deviceProvError(' WIFI_AUTH_FAILED ').message, contains('Password'));
      expect(deviceProvError('boom').message, contains('(boom)'));
      expect(deviceProvError(null).message, 'Configurazione non riuscita.');
      expect(deviceProvError('').message, 'Configurazione non riuscita.');
    });
  });

  group('app-side provisioning errors', () {
    test('permissions and adapter', () {
      expect(appProvError('permission').message, contains('Bluetooth'));
      expect(appProvError('permission_permanent').retry, ProvRetry.settings);
      expect(appProvError('bluetooth_unauthorized').retry, ProvRetry.settings);
      expect(appProvError('location_off').message, contains('localizzazione'));
      expect(appProvError('bluetooth_off').retry, ProvRetry.rescan);
    });

    test('claim token failure keeps the BLE link (retry on Wi-Fi form)', () {
      final i = appProvError('claim_api', detail: 'server non raggiungibile');
      expect(i.retry, ProvRetry.wifi);
      expect(i.message, contains('server non raggiungibile'));
    });

    test('unknown code', () {
      expect(appProvError('xyz').retry, ProvRetry.rescan);
    });
  });

  group('live errors', () {
    test('signaling error and timeout codes are mapped', () {
      expect(S.liveError('peer_offline'), contains('offline'));
      expect(S.liveError('answer_timeout'), contains('15 secondi'));
      expect(S.liveError('ice_failed'), contains('stessa rete'));
      expect(S.liveError('forbidden'), contains('non è associato'));
      expect(S.liveError(null), S.liveFailed);
      expect(S.liveError('whatever'), S.liveFailed);
    });
  });

  group('server health messages', () {
    test('every status has a message', () {
      for (final s in HealthStatus.values) {
        expect(S.healthMessage(s, null), isNotEmpty);
      }
      expect(S.healthMessage(HealthStatus.notPetmate, 'HTTP 404'), contains('HTTP 404'));
    });
  });

  group('BLE write size', () {
    test('≤ 180 B incl. flags, ≤ MTU − 3', () {
      expect(bleMaxWrite(23), 20);
      expect(bleMaxWrite(0), 20);
      expect(bleMaxWrite(185), 180);
      expect(bleMaxWrite(247), 180);
      expect(bleMaxWrite(517), 180);
      expect(bleMaxWrite(100), 97);
    });
  });
}
