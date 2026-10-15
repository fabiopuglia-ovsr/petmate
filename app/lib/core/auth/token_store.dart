import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Platform keystore used for the JWT and the server URL
/// (Keychain on iOS, Keystore-backed ciphers on Android — flutter_secure_storage 10).
const FlutterSecureStorage kSecureStorage = FlutterSecureStorage(
  aOptions: AndroidOptions(),
  iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock),
);

/// Persists the user JWT.
class TokenStore {
  static const _kToken = 'petmate.user_jwt';
  static const _kEmail = 'petmate.email';

  final FlutterSecureStorage _storage;

  TokenStore([FlutterSecureStorage? storage]) : _storage = storage ?? kSecureStorage;

  /// Never throws: a broken keystore (e.g. after a backup restore on another
  /// device) must not prevent the app from starting — the user logs in again.
  Future<String?> readToken() => _safeRead(_kToken);
  Future<String?> readEmail() => _safeRead(_kEmail);

  Future<String?> _safeRead(String key) async {
    try {
      return await _storage.read(key: key);
    } catch (_) {
      return null;
    }
  }

  Future<void> save(String token, String email) async {
    try {
      await _storage.write(key: _kToken, value: token);
      await _storage.write(key: _kEmail, value: email);
    } catch (_) {
      // Not persisted: the session still works until the app is closed.
    }
  }

  Future<void> clear() async {
    try {
      await _storage.delete(key: _kToken);
    } catch (_) {}
  }
}

/// Reads the `exp` claim of a JWT without verifying it (server verifies).
DateTime? jwtExpiry(String token) {
  final parts = token.split('.');
  if (parts.length != 3) return null;
  try {
    final payload = jsonDecode(utf8.decode(base64Url.decode(base64Url.normalize(parts[1]))));
    final exp = payload is Map ? payload['exp'] : null;
    return exp is num ? DateTime.fromMillisecondsSinceEpoch((exp * 1000).round(), isUtc: true) : null;
  } catch (_) {
    return null;
  }
}
