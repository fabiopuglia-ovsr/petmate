import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:intl/date_symbol_data_local.dart';
import 'package:provider/provider.dart';

import 'app_router.dart';
import 'core/api/api_client.dart';
import 'core/auth/auth_state.dart';
import 'core/auth/token_store.dart';
import 'core/config/server_config.dart';
import 'core/env.dart';
import 'core/push/push_service.dart';
import 'core/signaling/signaling_client.dart';
import 'core/webrtc/session_hub.dart';
import 'core/widgets/theme.dart';
import 'features/devices/devices_controller.dart';
import 'l10n/strings.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initializeDateFormatting('it');

  // Server URL first: every client below derives its endpoints from it.
  final server = ServerConfig();
  await server.restore();
  debugPrint('[petmate] mvp=${Env.mvp} api=${server.base}');

  final api = ApiClient(baseUrl: server.base);
  final auth = AuthState(TokenStore(), api);
  final signaling = SignalingClient(uriFor: (token) => signalUriFor(api.baseUrl, token));
  final hub = SessionHub(api: api, signaling: signaling);
  final devices = DevicesController(api, signaling);
  final push = PushService(api);

  signaling.onUnauthorized = () => unawaited(auth.signOut());

  // Server changed (login screen): re-point REST and signaling.
  server.addListener(() {
    if (api.baseUrl == server.base) return;
    api.baseUrl = server.base;
    signaling.reconnect();
  });

  // Signaling + push follow the auth state.
  auth.addListener(() {
    final t = auth.token;
    if (auth.status == AuthStatus.signedIn && t != null) {
      signaling.connect(t);
      unawaited(devices.refresh());
      if (!Env.mvp) unawaited(push.registerIfEnabled());
    } else if (auth.status == AuthStatus.signedOut) {
      unawaited(hub.closeSession());
      signaling.disconnect();
      devices.clear();
    }
  });
  await auth.restore();

  runApp(MultiProvider(
    providers: [
      Provider<ApiClient>.value(value: api),
      ChangeNotifierProvider<ServerConfig>.value(value: server),
      ChangeNotifierProvider<AuthState>.value(value: auth),
      ChangeNotifierProvider<SignalingClient>.value(value: signaling),
      ChangeNotifierProvider<SessionHub>.value(value: hub),
      ChangeNotifierProvider<DevicesController>.value(value: devices),
    ],
    child: PetmateApp(auth: auth),
  ));
}

class PetmateApp extends StatefulWidget {
  final AuthState auth;
  const PetmateApp({super.key, required this.auth});

  @override
  State<PetmateApp> createState() => _PetmateAppState();
}

class _PetmateAppState extends State<PetmateApp> {
  late final router = buildRouter(widget.auth);

  @override
  void dispose() {
    router.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => MaterialApp.router(
        title: S.appName,
        debugShowCheckedModeBanner: false,
        theme: buildTheme(),
        routerConfig: router,
        locale: const Locale('it'),
        supportedLocales: const [Locale('it'), Locale('en')],
        localizationsDelegates: const [
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
      );
}
