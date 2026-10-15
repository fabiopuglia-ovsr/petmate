// Cloud REST client — INTERFACES.md §6.
import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../env.dart';
import '../models/config.dart';
import '../models/device.dart';
import '../models/json_utils.dart';
import '../models/robot_event.dart';

class ApiException implements Exception {
  final int statusCode;
  final String message;
  const ApiException(this.statusCode, this.message);

  bool get isOffline => statusCode == 409; // POST /commands when device offline
  bool get isUnauthorized => statusCode == 401;
  bool get isNetwork => statusCode == 0;

  @override
  String toString() => 'ApiException($statusCode): $message';
}

class ApiClient {
  final http.Client _http;
  final Duration timeout;

  /// REST base (normalized, no trailing slash), e.g. `http://192.168.1.50:8000/v1`.
  /// Kept in sync with ServerConfig by main.dart.
  String baseUrl;

  /// User JWT, set by [AuthState].
  String? token;

  /// Called on any 401 so the app can sign out.
  void Function()? onUnauthorized;

  ApiClient({required this.baseUrl, http.Client? client, this.timeout = const Duration(seconds: 15)})
      : _http = client ?? http.Client();

  Uri get _base => Uri.parse(baseUrl);

  Uri uri(String path, [Map<String, String>? query]) => apiUri(baseUrl, path, query);

  Map<String, String> get authHeaders => {if (token != null) 'Authorization': 'Bearer $token'};

  Future<Object?> _send(String method, String path,
      {Object? body, Map<String, String>? query, bool auth = true}) async {
    final req = http.Request(method, uri(path, query));
    req.headers['Accept'] = 'application/json';
    if (auth) req.headers.addAll(authHeaders);
    if (body != null) {
      req.headers['Content-Type'] = 'application/json';
      req.body = jsonEncode(body);
    }
    http.Response res;
    try {
      res = await http.Response.fromStream(await _http.send(req).timeout(timeout));
    } on TimeoutException {
      throw const ApiException(0, 'timeout');
    } on Exception catch (e) {
      // SocketException / ClientException / HandshakeException: server down,
      // wrong address, phone not on the same LAN, or https on an http port.
      throw ApiException(0, e.toString());
    }
    if (res.statusCode == 401 && auth) onUnauthorized?.call();
    if (res.statusCode < 200 || res.statusCode >= 300) {
      throw ApiException(res.statusCode, _errorMessage(res));
    }
    if (res.bodyBytes.isEmpty) return null;
    try {
      return jsonDecode(utf8.decode(res.bodyBytes));
    } on FormatException {
      // Not JSON: most likely the address points to something that is not
      // the Petmate cloud.
      throw ApiException(res.statusCode, 'invalid_response');
    }
  }

  static String _errorMessage(http.Response r) {
    try {
      final j = jsonDecode(r.body);
      if (j is Map && j['detail'] != null) return j['detail'].toString();
      if (j is Map && j['error'] != null) return j['error'].toString();
    } catch (_) {}
    return r.reasonPhrase ?? 'HTTP ${r.statusCode}';
  }

  // ---- auth ----
  Future<String> register(String email, String password) async =>
      asString(asMap(await _send('POST', '/auth/register',
          body: {'email': email, 'password': password}, auth: false))['access_token']);

  Future<String> login(String email, String password) async =>
      asString(asMap(await _send('POST', '/auth/login',
          body: {'email': email, 'password': password}, auth: false))['access_token']);

  // ---- devices ----
  Future<ClaimToken> createClaimToken() async =>
      ClaimToken.fromJson(asMap(await _send('POST', '/claim-tokens')));

  Future<List<DeviceSummary>> listDevices() async =>
      asList(await _send('GET', '/devices')).map((e) => DeviceSummary.fromJson(asMap(e))).toList();

  Future<void> renameDevice(String id, String name) =>
      _send('PATCH', '/devices/${Uri.encodeComponent(id)}', body: {'name': name});

  Future<void> removeDevice(String id) => _send('DELETE', '/devices/${Uri.encodeComponent(id)}');

  Future<List<IceServer>> iceServers() async =>
      asList(asMap(await _send('GET', '/ice-servers'))['ice_servers'])
          .map((e) => IceServer.fromJson(asMap(e)))
          .toList();

  // ---- config ----
  Future<DeviceConfig> getConfig(String id) async =>
      DeviceConfig.fromJson(asMap(await _send('GET', '/devices/${Uri.encodeComponent(id)}/config')));

  Future<void> putConfig(String id, DeviceConfig config) =>
      _send('PUT', '/devices/${Uri.encodeComponent(id)}/config', body: config.toJson());

  // ---- commands (fallback when no live session) ----
  Future<void> sendCommand(String id, Json cmd) =>
      _send('POST', '/devices/${Uri.encodeComponent(id)}/commands', body: cmd);

  // ---- events / clips ----
  Future<List<RobotEvent>> events(String id, {int limit = 50}) async =>
      asList(await _send('GET', '/devices/${Uri.encodeComponent(id)}/events',
              query: {'limit': '$limit'}))
          .map((e) => RobotEvent.fromJson(asMap(e)))
          .toList();

  Future<List<RobotClip>> clips(String id) async =>
      asList(await _send('GET', '/devices/${Uri.encodeComponent(id)}/clips'))
          .map((e) => RobotClip.fromJson(asMap(e)))
          .toList();

  /// Absolute URL for a clip file (§6 `GET /clips/{clip_id}`); falls back to
  /// the server-provided `url` when that is already absolute.
  Uri clipUri(RobotClip c) {
    final u = Uri.tryParse(c.url);
    if (u != null && u.hasScheme) return u;
    if (u != null && c.url.startsWith('/')) return _base.resolve(c.url);
    return uri('/clips/${Uri.encodeComponent(c.clipId)}');
  }

  Uri? thumbUri(RobotClip c) {
    final t = c.thumbUrl;
    if (t == null || t.isEmpty) return null;
    final u = Uri.tryParse(t);
    if (u == null) return null;
    return u.hasScheme ? u : _base.resolve(t);
  }

  // ---- push ----
  Future<void> registerPushToken(String pushToken, String platform) =>
      _send('POST', '/push-tokens', body: {'token': pushToken, 'platform': platform});

  void close() => _http.close();
}
