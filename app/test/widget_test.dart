// Smoke test. This file also prevents `flutter create .` from generating its
// template widget_test.dart (which references a non-existent `MyApp`).
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:petmate/core/api/api_client.dart';
import 'package:petmate/core/auth/auth_state.dart';
import 'package:petmate/core/auth/token_store.dart';
import 'package:petmate/core/config/server_config.dart';
import 'package:petmate/core/widgets/theme.dart';
import 'package:petmate/features/auth/login_screen.dart';
import 'package:petmate/l10n/strings.dart';
import 'package:provider/provider.dart';

void main() {
  testWidgets('login screen builds and toggles the server field', (tester) async {
    final server = ServerConfig(); // no platform call until restore()/set()
    final api = ApiClient(baseUrl: server.base);
    final auth = AuthState(TokenStore(), api);

    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<ServerConfig>.value(value: server),
        ChangeNotifierProvider<AuthState>.value(value: auth),
      ],
      child: MaterialApp(theme: buildTheme(), home: const LoginScreen(registerMode: false)),
    ));

    expect(find.text(S.login), findsOneWidget);
    expect(find.text(S.serverLabel), findsNothing);

    await tester.ensureVisible(find.text(S.advancedSettings));
    await tester.tap(find.text(S.advancedSettings));
    await tester.pump();
    expect(find.text(S.serverLabel), findsOneWidget);
    // The URL appears in the field (and in the hidden hint, which equals the default).
    expect(find.text(server.base), findsWidgets);
  });
}
