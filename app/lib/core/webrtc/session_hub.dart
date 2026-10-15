// Owns the (at most one) live WebRTC session and routes commands/config:
// DataChannel when a live session to that device is open, otherwise REST.
import 'dart:async';

import 'package:flutter/foundation.dart';

import '../api/api_client.dart';
import '../env.dart';
import '../models/config.dart';
import '../models/json_utils.dart';
import '../signaling/signaling_client.dart';
import 'live_session.dart';

enum SendRoute { dataChannel, rest }

class SessionHub extends ChangeNotifier {
  final ApiClient api;
  final SignalingClient signaling;

  LiveSession? _session;

  SessionHub({required this.api, required this.signaling});

  LiveSession? get session => _session;

  LiveSession? openSessionFor(String deviceId) {
    final s = _session;
    return (s != null && s.deviceId == deviceId && s.isOpen) ? s : null;
  }

  /// Returns the session for [deviceId], creating and starting one if needed.
  /// A failed/closed session for the same device is restarted in place (the
  /// renderers are kept, so the video view does not need to be rebuilt).
  LiveSession obtain(String deviceId) {
    final cur = _session;
    if (cur != null && cur.deviceId == deviceId) {
      if (cur.status == LiveStatus.failed || cur.status == LiveStatus.idle) unawaited(cur.start());
      if (cur.status != LiveStatus.closed) return cur;
    }
    if (cur != null) unawaited(_drop(cur));
    final s = LiveSession(deviceId: deviceId, api: api, signaling: signaling, mvp: Env.mvp);
    _session = s;
    s.addListener(notifyListeners);
    unawaited(s.start());
    notifyListeners();
    return s;
  }

  /// Forces a new offer for [deviceId] (reconnect button).
  LiveSession reconnect(String deviceId) {
    final cur = _session;
    if (cur != null && cur.deviceId == deviceId && cur.status != LiveStatus.closed) {
      unawaited(cur.restart());
      return cur;
    }
    return obtain(deviceId);
  }

  Future<void> closeSession() async {
    final s = _session;
    _session = null;
    if (s != null) await _drop(s);
    notifyListeners();
  }

  Future<void> _drop(LiveSession s) async {
    s.removeListener(notifyListeners);
    await s.close();
    s.dispose();
  }

  /// Sends a §4.2 command: DataChannel if open, else `POST /devices/{id}/commands`.
  /// DataChannel-only commands (camera, snapshot, …) throw if no session.
  Future<SendRoute> sendCommand(String deviceId, Json cmd) async {
    final s = openSessionFor(deviceId);
    if (s != null && s.send(cmd)) return SendRoute.dataChannel;
    if (_dcOnly.contains(cmd['t'])) throw StateError('requires_live_session');
    await api.sendCommand(deviceId, cmd);
    return SendRoute.rest;
  }

  static const _dcOnly = {'camera', 'display', 'volume', 'snapshot', 'record_clip', 'config_get', 'config_set', 'teleop'};

  /// Loads config: prefers `config_get` over the DataChannel when live (that is
  /// the robot's *effective* config), falling back to REST desired config.
  Future<DeviceConfig> loadConfig(String deviceId) async {
    final s = openSessionFor(deviceId);
    if (s != null) {
      try {
        return await s.fetchConfig();
      } catch (_) {/* fall back */}
    }
    return api.getConfig(deviceId);
  }

  /// Saves config: `config_set` over DataChannel when live, otherwise
  /// `PUT /devices/{id}/config` (cloud forwards it when the robot is online).
  Future<SendRoute> saveConfig(String deviceId, DeviceConfig cfg) async {
    final s = openSessionFor(deviceId);
    if (s != null && s.pushConfig(cfg)) return SendRoute.dataChannel;
    await api.putConfig(deviceId, cfg);
    return SendRoute.rest;
  }

  @override
  void dispose() {
    final s = _session;
    _session = null;
    if (s != null) unawaited(_drop(s));
    super.dispose();
  }
}
