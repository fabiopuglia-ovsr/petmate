import 'package:flutter/foundation.dart';

import '../api/api_client.dart';
import 'token_store.dart';

enum AuthStatus { unknown, signedOut, signedIn }

/// App-wide authentication state; go_router listens to it for redirects.
class AuthState extends ChangeNotifier {
  final TokenStore _store;
  final ApiClient _api;

  AuthStatus _status = AuthStatus.unknown;
  String? _token;
  String? _email;

  AuthState(this._store, this._api) {
    _api.onUnauthorized = signOut;
  }

  AuthStatus get status => _status;
  String? get token => _token;
  String? get email => _email;

  Future<void> restore() async {
    final t = await _store.readToken();
    _email = await _store.readEmail();
    final exp = t == null ? null : jwtExpiry(t);
    if (t != null && (exp == null || exp.isAfter(DateTime.now().toUtc().add(const Duration(minutes: 1))))) {
      _setToken(t);
    } else {
      _status = AuthStatus.signedOut;
      notifyListeners();
    }
  }

  Future<void> login(String email, String password) async {
    final t = await _api.login(email, password);
    await _store.save(t, email);
    _email = email;
    _setToken(t);
  }

  Future<void> register(String email, String password) async {
    final t = await _api.register(email, password);
    await _store.save(t, email);
    _email = email;
    _setToken(t);
  }

  Future<void> signOut() async {
    await _store.clear();
    _token = null;
    _api.token = null;
    _status = AuthStatus.signedOut;
    notifyListeners();
  }

  void _setToken(String t) {
    _token = t;
    _api.token = t;
    _status = AuthStatus.signedIn;
    notifyListeners();
  }
}
