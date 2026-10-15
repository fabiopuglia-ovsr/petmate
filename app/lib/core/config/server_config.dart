// Cloud base URL chosen at runtime (MVP: the developer's laptop on the LAN).
//
// Default from `--dart-define=PETMATE_API=...`, editable on the login screen,
// persisted in secure storage, validated with `GET {base}/health`.
import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;

import '../auth/token_store.dart';
import '../env.dart';

export '../env.dart' show HealthStatus;

class HealthResult {
  final HealthStatus status;

  /// Normalized base URL (null when [HealthStatus.invalidUrl]).
  final String? base;
  final String? detail;
  const HealthResult(this.status, {this.base, this.detail});
  bool get ok => status == HealthStatus.ok;
}

class ServerConfig extends ChangeNotifier {
  static const _kBase = 'petmate.api_base';
  static const healthTimeout = Duration(seconds: 5);

  final FlutterSecureStorage _storage;
  final http.Client _http;
  String _base;

  ServerConfig({FlutterSecureStorage? storage, http.Client? client})
      : _storage = storage ?? kSecureStorage,
        _http = client ?? http.Client(),
        _base = _normalizeOr(Env.apiBaseDefault, Env.apiBaseDefault);

  static String _normalizeOr(String s, String fallback) {
    try {
      return normalizeApiBase(s);
    } on InvalidApiBase {
      return fallback;
    }
  }

  /// Current REST base, e.g. `http://192.168.1.50:8000/v1` (no trailing slash).
  String get base => _base;

  /// The build-time default (shown as hint / "reset").
  String get defaultBase => _normalizeOr(Env.apiBaseDefault, Env.apiBaseDefault);

  /// Loads the persisted value, if any. Never throws.
  Future<void> restore() async {
    try {
      final v = await _storage.read(key: _kBase);
      if (v != null && v.isNotEmpty) _base = normalizeApiBase(v);
    } catch (_) {
      // keep default
    }
    notifyListeners();
  }

  /// `GET {base}/health` → `{"ok": true}`. Does not change the stored value.
  Future<HealthResult> check(String input) async {
    final String base;
    try {
      base = normalizeApiBase(input);
    } on InvalidApiBase {
      return const HealthResult(HealthStatus.invalidUrl);
    }
    try {
      final res = await _http
          .get(healthUri(base), headers: const {'Accept': 'application/json'})
          .timeout(healthTimeout);
      if (res.statusCode != 200) {
        return HealthResult(HealthStatus.notPetmate, base: base, detail: 'HTTP ${res.statusCode}');
      }
      final body = jsonDecode(utf8.decode(res.bodyBytes));
      if (body is Map && body['ok'] == true) return HealthResult(HealthStatus.ok, base: base);
      return HealthResult(HealthStatus.notPetmate, base: base, detail: 'unexpected body');
    } on TimeoutException {
      return HealthResult(HealthStatus.timeout, base: base);
    } on FormatException catch (e) {
      return HealthResult(HealthStatus.notPetmate, base: base, detail: e.message);
    } catch (e) {
      // SocketException, ClientException, HandshakeException (https on an http port)…
      return HealthResult(HealthStatus.unreachable, base: base, detail: e.toString());
    }
  }

  /// Sets and persists [input] (normalized). Throws [InvalidApiBase].
  Future<void> set(String input) async {
    final b = normalizeApiBase(input);
    final changed = b != _base;
    _base = b;
    try {
      await _storage.write(key: _kBase, value: b);
    } catch (_) {
      // Not persisted; still used for this run.
    }
    if (changed) notifyListeners();
  }

  @override
  void dispose() {
    _http.close();
    super.dispose();
  }
}
