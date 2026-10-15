import 'dart:async';
import 'dart:io' show Platform;

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';

import '../api/api_client.dart';
import '../env.dart';

/// FCM registration — only active when built with `--dart-define=PETMATE_FCM=true`
/// and the Firebase platform config files are present.
class PushService {
  final ApiClient _api;
  bool _initialized = false;
  StreamSubscription<String>? _refreshSub;

  PushService(this._api);

  Future<void> registerIfEnabled() async {
    if (!Env.fcmEnabled) return;
    try {
      if (!_initialized) {
        await Firebase.initializeApp();
        _initialized = true;
      }
      final fm = FirebaseMessaging.instance;
      final perm = await fm.requestPermission();
      if (perm.authorizationStatus == AuthorizationStatus.denied) return;
      final token = await fm.getToken();
      if (token != null) await _api.registerPushToken(token, _platform);
      await _refreshSub?.cancel();
      _refreshSub = fm.onTokenRefresh.listen((t) => unawaited(_api.registerPushToken(t, _platform)));
    } catch (e) {
      debugPrint('Push registration skipped: $e');
    }
  }

  static String get _platform => Platform.isIOS ? 'ios' : 'android';
}
