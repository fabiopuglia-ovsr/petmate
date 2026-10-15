import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import 'core/auth/auth_state.dart';
import 'core/env.dart';
import 'features/activity/activity_screen.dart';
import 'features/auth/login_screen.dart';
import 'features/device_home/device_home_screen.dart';
import 'features/devices/devices_screen.dart';
import 'features/live/live_screen.dart';
import 'features/onboarding/onboarding_screen.dart';
import 'features/schedule/schedule_screen.dart';
import 'features/settings/settings_screen.dart';

/// Location of the live view for [deviceId].
/// MVP: `/devices/live/<id>` (back → robot list); full app: `/d/<id>/live`.
String liveLocation(String deviceId) {
  final id = Uri.encodeComponent(deviceId);
  return Env.mvp ? '/devices/live/$id' : '/d/$id/live';
}

/// Location of a robot's main page: the live view in MVP, the device hub otherwise.
String deviceLocation(String deviceId) =>
    Env.mvp ? liveLocation(deviceId) : '/d/${Uri.encodeComponent(deviceId)}';

Page<void> _livePage(GoRouterState s) => MaterialPage<void>(
      key: s.pageKey,
      fullscreenDialog: true,
      child: LiveScreen(deviceId: s.pathParameters['id'] ?? ''),
    );

GoRouter buildRouter(AuthState auth) => GoRouter(
      initialLocation: '/devices',
      refreshListenable: auth,
      redirect: (context, state) {
        final loc = state.matchedLocation;
        final atAuth = loc == '/login' || loc == '/register';
        switch (auth.status) {
          case AuthStatus.unknown:
            return null;
          case AuthStatus.signedOut:
            return atAuth ? null : '/login';
          case AuthStatus.signedIn:
            return atAuth ? '/devices' : null;
        }
      },
      routes: [
        GoRoute(path: '/login', builder: (_, _) => const LoginScreen(registerMode: false)),
        GoRoute(path: '/register', builder: (_, _) => const LoginScreen(registerMode: true)),
        GoRoute(path: '/devices', builder: (_, _) => const DevicesScreen(), routes: [
          GoRoute(path: 'add', builder: (_, _) => const OnboardingScreen()),
          // MVP: Login → I miei robot → Wizard → Live. Nothing else is routable.
          if (Env.mvp) GoRoute(path: 'live/:id', pageBuilder: (_, s) => _livePage(s)),
        ]),
        if (!Env.mvp)
          GoRoute(
            path: '/d/:id',
            builder: (_, s) => DeviceHomeScreen(deviceId: s.pathParameters['id'] ?? ''),
            routes: [
              GoRoute(path: 'live', pageBuilder: (_, s) => _livePage(s)),
              GoRoute(path: 'schedule', builder: (_, s) => ScheduleScreen(deviceId: s.pathParameters['id'] ?? '')),
              GoRoute(path: 'settings', builder: (_, s) => SettingsScreen(deviceId: s.pathParameters['id'] ?? '')),
              GoRoute(path: 'activity', builder: (_, s) => ActivityScreen(deviceId: s.pathParameters['id'] ?? '')),
            ],
          ),
      ],
    );
