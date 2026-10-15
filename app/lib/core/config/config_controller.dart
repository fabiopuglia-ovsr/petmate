import 'package:flutter/foundation.dart';

import '../api/api_client.dart';
import '../models/config.dart';
import '../webrtc/session_hub.dart';

/// Per-device editable config used by the schedule and settings screens.
class ConfigController extends ChangeNotifier {
  final String deviceId;
  final SessionHub _hub;

  ConfigController(this.deviceId, this._hub);

  DeviceConfig? _saved;
  DeviceConfig? _draft;
  bool _loading = false;
  bool _saving = false;
  Object? _error;
  SendRoute? _lastRoute;

  DeviceConfig? get config => _draft;
  bool get loading => _loading;
  bool get saving => _saving;
  Object? get error => _error;
  SendRoute? get lastRoute => _lastRoute;
  bool get dirty => _draft != null && !identical(_draft, _saved);
  List<String> get problems => _draft?.validate() ?? const [];

  Future<void> load() async {
    _loading = true;
    _error = null;
    notifyListeners();
    try {
      _saved = await _hub.loadConfig(deviceId);
      _draft = _saved;
    } catch (e) {
      _error = e;
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  void edit(DeviceConfig Function(DeviceConfig c) change) {
    final d = _draft;
    if (d == null) return;
    _draft = change(d);
    notifyListeners();
  }

  void discard() {
    _draft = _saved;
    notifyListeners();
  }

  /// Returns true on success. Validation failures are not sent.
  Future<bool> save() async {
    final d = _draft;
    if (d == null || problems.isNotEmpty) return false;
    _saving = true;
    _error = null;
    notifyListeners();
    try {
      _lastRoute = await _hub.saveConfig(deviceId, d);
      _saved = d;
      return true;
    } on ApiException catch (e) {
      _error = e;
      return false;
    } catch (e) {
      _error = e;
      return false;
    } finally {
      _saving = false;
      notifyListeners();
    }
  }
}
