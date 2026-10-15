/// Build-time configuration (override with --dart-define) and pure helpers
/// to derive cloud endpoints from the REST base URL.
///
/// Pure Dart (no Flutter imports) so it is unit-testable.
abstract final class Env {
  /// Default REST base, §6. In the MVP the cloud runs on the developer's
  /// laptop over plain HTTP, e.g. `--dart-define=PETMATE_API=http://192.168.1.50:8000/v1`.
  /// The value can be overridden at runtime from the login screen
  /// ("Impostazioni avanzate" → Server), see ServerConfig.
  static const apiBaseDefault =
      String.fromEnvironment('PETMATE_API', defaultValue: 'http://192.168.1.50:8000/v1');

  /// MVP 1 (docs/MVP.md): login → robot list → BLE wizard → live video only.
  /// Every other feature stays in the code base but is unreachable.
  /// Build the full app with `--dart-define=PETMATE_MVP=false`.
  static const mvp = bool.fromEnvironment('PETMATE_MVP', defaultValue: true);

  /// Push notifications via FCM are opt-in: requires Firebase config files
  /// (google-services.json / GoogleService-Info.plist) in the platform dirs.
  static const fcmEnabled = bool.fromEnvironment('PETMATE_FCM', defaultValue: false);

  static const defaultTimezone = 'Europe/Rome';
}

/// Outcome of `GET {base}/health` (see ServerConfig.check).
enum HealthStatus { ok, invalidUrl, unreachable, timeout, notPetmate }

/// Thrown by [normalizeApiBase] for input that cannot be a REST base URL.
class InvalidApiBase implements Exception {
  final String input;
  const InvalidApiBase(this.input);
  @override
  String toString() => 'InvalidApiBase($input)';
}

/// Normalizes a user-typed REST base URL.
///
/// * trims spaces, adds `http://` when no scheme was typed (LAN dev setup);
/// * only `http`/`https` with a host are accepted;
/// * drops query/fragment and trailing slashes;
/// * appends `/v1` when the path is empty (`http://pc:8000` → `http://pc:8000/v1`).
String normalizeApiBase(String input) {
  var s = input.trim();
  if (s.isEmpty) throw InvalidApiBase(input);
  if (!s.contains('://')) s = 'http://$s';
  final u = Uri.tryParse(s);
  if (u == null || (u.scheme != 'http' && u.scheme != 'https') || u.host.isEmpty) {
    throw InvalidApiBase(input);
  }
  var path = u.path;
  while (path.endsWith('/')) {
    path = path.substring(0, path.length - 1);
  }
  if (path.isEmpty) path = '/v1';
  return Uri(
    scheme: u.scheme,
    userInfo: u.userInfo.isEmpty ? null : u.userInfo,
    host: u.host,
    port: u.hasPort ? u.port : null,
    path: path,
  ).toString();
}

/// Joins [base] (normalized, no trailing slash) and an absolute API [path].
Uri apiUri(String base, String path, [Map<String, String>? query]) {
  final b = Uri.parse(base);
  final basePath = b.path.endsWith('/') ? b.path.substring(0, b.path.length - 1) : b.path;
  return b.replace(path: '$basePath$path', queryParameters: query);
}

/// `GET {base}/health` — the cloud serves it under the API prefix
/// (`/v1/health`, cloud/app/main.py) and also at the root `/health`.
Uri healthUri(String base) => apiUri(base, '/health');

/// Signaling WebSocket derived from the REST base: http→ws, https→wss,
/// path `<base path>/signal` (= `/v1/signal`), `?token=<JWT>`.
Uri signalUriFor(String base, String token) {
  final b = Uri.parse(base);
  final scheme = b.scheme == 'https' ? 'wss' : 'ws';
  final u = apiUri(base, '/signal', {'token': token});
  return u.replace(scheme: scheme);
}
