// Signaling WebSocket — INTERFACES.md §6 `wss://…/v1/signal?token=<JWT>`.
//
// Responsibilities: connect, receive `hello` (our peer id), app-level ping
// every 20 s, exponential-backoff reconnect, fan out messages by type, and
// track device presence for the device list.
import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../models/json_utils.dart';

enum SignalingStatus { disconnected, connecting, connected }

typedef ChannelFactory = WebSocketChannel Function(Uri uri);

/// Builds the signaling URL for a JWT (see `signalUriFor` in env.dart).
typedef SignalUriBuilder = Uri Function(String token);

class SignalingClient extends ChangeNotifier {
  static const pingInterval = Duration(seconds: 20);

  /// No pong within this window → assume the socket is dead and reconnect.
  static const pongTimeout = Duration(seconds: 10);

  final ChannelFactory _factory;
  final SignalUriBuilder _uriFor;
  final Random _rng = Random();

  /// Called when the server rejects the token (`error: unauthorized` or
  /// close code 4401) — the app signs out instead of retrying forever.
  void Function()? onUnauthorized;

  String? _token;
  WebSocketChannel? _ch;
  StreamSubscription<dynamic>? _sub;
  Timer? _pingTimer, _pongTimer, _reconnectTimer;
  int _attempt = 0;
  bool _wanted = false;

  SignalingStatus _status = SignalingStatus.disconnected;
  String? _peer;
  final Map<String, bool> _presence = {};
  final _messages = StreamController<Json>.broadcast();

  SignalingClient({required SignalUriBuilder uriFor, ChannelFactory? factory})
      : _uriFor = uriFor,
        _factory = factory ?? ((uri) => WebSocketChannel.connect(uri));

  SignalingStatus get status => _status;

  /// Our peer id as assigned by the server, e.g. `user:42:5f2c…`.
  String? get peer => _peer;

  /// All incoming messages (already JSON-decoded); filter by `type`.
  Stream<Json> get messages => _messages.stream;

  /// Live presence from `{"type":"presence",...}`; null if unknown.
  bool? isOnline(String deviceId) => _presence[deviceId];

  /// Starts (or restarts with a new token) the connection. Idempotent.
  void connect(String token) {
    if (_wanted && token == _token && _status != SignalingStatus.disconnected) return;
    _token = token;
    _wanted = true;
    _attempt = 0;
    _open();
  }

  /// Drops the current socket and connects again right away (e.g. after the
  /// server URL changed). No-op when not wanted.
  void reconnect() {
    if (!_wanted || _token == null) return;
    _attempt = 0;
    _reconnectTimer?.cancel();
    _open();
  }

  /// Closes and stops reconnecting (e.g. on sign-out).
  void disconnect() {
    _wanted = false;
    _reconnectTimer?.cancel();
    _teardown();
    _setStatus(SignalingStatus.disconnected);
  }

  /// Waits until the connection has received `hello` (or times out).
  Future<void> ready({Duration timeout = const Duration(seconds: 10)}) async {
    if (_status == SignalingStatus.connected) return;
    final c = Completer<void>();
    void l() {
      if (_status == SignalingStatus.connected && !c.isCompleted) c.complete();
    }

    addListener(l);
    try {
      await c.future.timeout(timeout);
    } finally {
      removeListener(l);
    }
  }

  /// Sends a message; returns false if not connected.
  bool send(Json msg) {
    final ch = _ch;
    if (ch == null || _status != SignalingStatus.connected) return false;
    try {
      ch.sink.add(jsonEncode(msg));
      return true;
    } catch (_) {
      return false; // sink closed under us; the stream handlers reconnect
    }
  }

  bool sendOffer(String deviceId, String sdp) =>
      send({'type': 'offer', 'to': 'device:$deviceId', 'sdp': sdp});

  bool sendIce(String deviceId, Json candidate) =>
      send({'type': 'ice', 'to': 'device:$deviceId', 'candidate': candidate});

  bool sendBye(String deviceId) => send({'type': 'bye', 'to': 'device:$deviceId'});

  void _open() {
    _teardown();
    final token = _token;
    if (token == null) return;
    _setStatus(SignalingStatus.connecting);
    try {
      final ch = _factory(_uriFor(token));
      _ch = ch;
      // A failed handshake completes `ready` with an error AND closes the
      // stream; the stream handlers below do the reconnect, here we only
      // mark the error as handled (otherwise it is reported as uncaught).
      unawaited(ch.ready.catchError((Object _) {}));
      _sub = ch.stream.listen(_onData, onError: (Object _) => _onClosed(), onDone: _onClosed);
    } catch (_) {
      _onClosed();
    }
  }

  void _onData(dynamic raw) {
    Json msg;
    try {
      final d = jsonDecode(raw is String ? raw : utf8.decode(raw as List<int>));
      if (d is! Map) return;
      msg = asMap(d);
    } catch (_) {
      return;
    }
    switch (msg['type']) {
      case 'hello':
        _peer = asStringOrNull(msg['peer']);
        _attempt = 0;
        _startPing();
        _setStatus(SignalingStatus.connected);
      case 'pong':
        _pongTimer?.cancel();
        return; // internal, not forwarded
      case 'presence':
        final id = asString(msg['device_id']);
        if (id.isNotEmpty) {
          _presence[id] = asBool(msg['online']);
          notifyListeners();
        }
      case 'error':
        if (msg['error'] == 'unauthorized') _authRejected = true;
    }
    _messages.add(msg);
  }

  void _startPing() {
    _pingTimer?.cancel();
    _pingTimer = Timer.periodic(pingInterval, (_) {
      if (send({'type': 'ping'})) {
        _pongTimer?.cancel();
        _pongTimer = Timer(pongTimeout, () => _onClosed());
      }
    });
  }

  bool _authRejected = false;

  void _onClosed() {
    final code = _ch?.closeCode;
    _teardown();
    _peer = null;
    _setStatus(SignalingStatus.disconnected);
    if (!_wanted) return;
    if (code == 4401 || _authRejected) {
      // Token refused (expired, cloud restarted with a new secret, device
      // list changed): stop and let the app sign out.
      _authRejected = false;
      _wanted = false;
      onUnauthorized?.call();
      return;
    }
    // Exponential backoff with jitter: 1, 2, 4 … 30 s.
    final base = min(30, 1 << min(_attempt, 5));
    final delay = Duration(milliseconds: (base * 1000 * (0.5 + _rng.nextDouble() * 0.5)).round());
    _attempt++;
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(delay, _open);
  }

  void _teardown() {
    _pingTimer?.cancel();
    _pongTimer?.cancel();
    final sub = _sub;
    _sub = null;
    unawaited(sub?.cancel());
    final ch = _ch;
    _ch = null;
    if (ch != null) unawaited(ch.sink.close().catchError((_) {}));
  }

  void _setStatus(SignalingStatus s) {
    if (_status == s) return;
    _status = s;
    notifyListeners();
  }

  @override
  void dispose() {
    disconnect();
    unawaited(_messages.close());
    super.dispose();
  }
}
