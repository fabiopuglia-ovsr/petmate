import 'package:flutter_test/flutter_test.dart';
import 'package:petmate/core/env.dart';

void main() {
  group('normalizeApiBase', () {
    test('keeps a well-formed LAN base', () {
      expect(normalizeApiBase('http://192.168.1.50:8000/v1'), 'http://192.168.1.50:8000/v1');
    });

    test('trims spaces and trailing slashes', () {
      expect(normalizeApiBase('  http://192.168.1.50:8000/v1/ '), 'http://192.168.1.50:8000/v1');
      expect(normalizeApiBase('http://192.168.1.50:8000/v1//'), 'http://192.168.1.50:8000/v1');
    });

    test('adds http:// and /v1 when missing', () {
      expect(normalizeApiBase('192.168.1.50:8000'), 'http://192.168.1.50:8000/v1');
      expect(normalizeApiBase('http://pc.local:8000/'), 'http://pc.local:8000/v1');
    });

    test('keeps https and custom paths', () {
      expect(normalizeApiBase('https://api.petmate.example/v1'), 'https://api.petmate.example/v1');
      expect(normalizeApiBase('https://h/x/v1'), 'https://h/x/v1');
    });

    test('drops query and fragment', () {
      expect(normalizeApiBase('http://h:8000/v1?x=1#y'), 'http://h:8000/v1');
    });

    test('rejects garbage', () {
      expect(() => normalizeApiBase(''), throwsA(isA<InvalidApiBase>()));
      expect(() => normalizeApiBase('ftp://h/v1'), throwsA(isA<InvalidApiBase>()));
      expect(() => normalizeApiBase('http://'), throwsA(isA<InvalidApiBase>()));
    });
  });

  group('derived endpoints', () {
    test('http base → ws://…/v1/signal?token=', () {
      final u = signalUriFor('http://192.168.1.50:8000/v1', 'abc.def.ghi');
      expect(u.toString(), 'ws://192.168.1.50:8000/v1/signal?token=abc.def.ghi');
    });

    test('https base → wss://…/v1/signal', () {
      final u = signalUriFor('https://api.petmate.example/v1', 't');
      expect(u.scheme, 'wss');
      expect(u.host, 'api.petmate.example');
      expect(u.path, '/v1/signal');
      expect(u.queryParameters, {'token': 't'});
    });

    test('trailing slash in base does not double the slash', () {
      expect(signalUriFor('http://h:8000/v1/', 't').path, '/v1/signal');
      expect(apiUri('http://h:8000/v1/', '/devices').toString(), 'http://h:8000/v1/devices');
    });

    test('health is served under the API prefix', () {
      expect(healthUri('http://192.168.1.50:8000/v1').toString(), 'http://192.168.1.50:8000/v1/health');
    });

    test('REST paths and query', () {
      expect(apiUri('http://h:8000/v1', '/devices/PM-1/events', {'limit': '50'}).toString(),
          'http://h:8000/v1/devices/PM-1/events?limit=50');
    });
  });
}
