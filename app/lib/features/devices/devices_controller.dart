import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../core/api/api_client.dart';
import '../../core/models/device.dart';
import '../../core/signaling/signaling_client.dart';

/// Device list with live presence: REST snapshot merged with signaling
/// `presence` messages.
class DevicesController extends ChangeNotifier {
  final ApiClient _api;
  final SignalingClient _signaling;
  late final StreamSubscription<Map<String, dynamic>> _sub;

  List<DeviceSummary> _devices = [];
  bool _loading = false;
  Object? _error;
  bool _loadedOnce = false;

  DevicesController(this._api, this._signaling) {
    _sub = _signaling.messages.listen((m) {
      if (m['type'] != 'presence') return;
      final id = m['device_id'];
      final online = m['online'] == true;
      final i = _devices.indexWhere((d) => d.deviceId == id);
      if (i < 0 || _devices[i].online == online) return;
      _devices = List.of(_devices)
        ..[i] = _devices[i].copyWith(online: online, lastSeen: online ? null : DateTime.now());
      notifyListeners();
    });
  }

  List<DeviceSummary> get devices => _devices;
  bool get loading => _loading;
  bool get loadedOnce => _loadedOnce;
  Object? get error => _error;

  DeviceSummary? byId(String id) => _devices.where((d) => d.deviceId == id).firstOrNull;

  Future<void> refresh() async {
    _loading = true;
    _error = null;
    notifyListeners();
    try {
      _devices = await _api.listDevices();
      _loadedOnce = true;
    } catch (e) {
      _error = e;
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  Future<void> rename(String id, String name) async {
    await _api.renameDevice(id, name);
    _devices = [for (final d in _devices) d.deviceId == id ? d.copyWith(name: name) : d];
    notifyListeners();
  }

  Future<void> remove(String id) async {
    await _api.removeDevice(id);
    _devices = _devices.where((d) => d.deviceId != id).toList();
    notifyListeners();
  }

  void clear() {
    _devices = [];
    _loadedOnce = false;
    notifyListeners();
  }

  @override
  void dispose() {
    unawaited(_sub.cancel());
    super.dispose();
  }
}
