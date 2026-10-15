import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:permission_handler/permission_handler.dart' show openAppSettings;

import '../../core/api/api_client.dart';
import '../../core/ble/provisioning_service.dart';
import '../../core/env.dart';
import '../../core/models/device.dart';
import '../../core/provisioning/prov_crypto.dart';
import '../../core/provisioning/prov_errors.dart';

enum OnbStep {
  intro,
  preparing, // permissions + adapter
  scanning,
  connecting,
  wifi,
  sending, // BLE status notifications: receiving → connecting → wifi_ok → registering → done
  waitingOnline, // polling GET /devices / presence
  online, // → the screen navigates to Live
  onlineTimeout,
  failed,
}

/// Drives the §7 provisioning flow:
/// permissions → adapter on → scan → connect (+MTU) → read device_info and
/// wifi_scan → choose SSID/password → POST /claim-tokens → encrypt + chunk +
/// write (status notifications subscribed before) → on `done` poll the cloud
/// until the robot is online → Live.
class OnboardingController extends ChangeNotifier {
  /// pm-core gives nmcli up to 45 s, then registers on the cloud.
  static const bleCompletionTimeout = Duration(seconds: 90);
  static const onlineTimeout = Duration(seconds: 60);
  static const onlinePollInterval = Duration(seconds: 2);

  final ApiClient _api;
  final String Function() _cloudUrl;
  final bool? Function(String deviceId) _presence;
  final BleScanner _scanner;

  OnboardingController({
    required ApiClient api,
    required String Function() cloudUrl,
    required bool? Function(String deviceId) presence,
    BleScanner? scanner,
  })  : _api = api,
        _cloudUrl = cloudUrl,
        _presence = presence,
        _scanner = scanner ?? BleScanner();

  OnbStep _step = OnbStep.intro;
  List<FoundRobot> _found = [];
  ProvisioningSession? _session;
  bool _linkLost = false;
  BleDeviceInfo? _info;
  List<WifiNetwork> _networks = [];
  ProvisioningStatus? _status;
  String _lastState = 'idle';
  ProvErrorInfo? _errorInfo;
  StreamSubscription<List<FoundRobot>>? _scanSub;
  StreamSubscription<bool>? _scanStateSub;
  StreamSubscription<ProvisioningStatus>? _statusSub;
  Timer? _bleTimer;
  Timer? _pollTimer;
  DateTime? _onlineDeadline;
  bool _polling = false;
  bool _scanDone = false;
  bool _disposed = false;

  OnbStep get step => _step;
  List<FoundRobot> get found => _found;
  BleDeviceInfo? get info => _info;
  List<WifiNetwork> get networks => _networks;
  ProvisioningStatus? get status => _status;
  ProvErrorInfo? get errorInfo => _errorInfo;
  bool get scanDone => _scanDone;
  String? get deviceId => _info?.deviceId;

  // ------------------------------------------------------------------ scan
  Future<void> startScan() async {
    await _resetSession();
    _found = [];
    _scanDone = false;
    _errorInfo = null;
    _info = null;
    _go(OnbStep.preparing);
    try {
      await ensureBlePermissions();
      await ensureAdapterOn();
      if (_disposed) return;
      final stream = await _scanner.start();
      if (_disposed) {
        await _scanner.stop();
        return;
      }
      _go(OnbStep.scanning);
      _scanSub = stream.listen(
        (list) {
          _found = list;
          _notify();
        },
        onError: (Object e) => _fail(e),
      );
      _scanStateSub = _scanner.isScanning.listen((on) {
        if (!on && _step == OnbStep.scanning && !_scanDone) {
          _scanDone = true;
          _notify();
        }
      });
    } catch (e) {
      _fail(e);
    }
  }

  Future<void> _stopScan() async {
    await _scanSub?.cancel();
    _scanSub = null;
    await _scanStateSub?.cancel();
    _scanStateSub = null;
    await _scanner.stop();
  }

  // --------------------------------------------------------------- connect
  Future<void> select(FoundRobot r) async {
    if (_step != OnbStep.scanning) return;
    await _stopScan();
    _go(OnbStep.connecting);
    final s = ProvisioningSession(r.device);
    _session = s;
    _linkLost = false;
    try {
      await s.connect();
      _statusSub = s.status.listen(_onStatus, onError: _onLinkError);
      _info = await s.readInfo();
      _networks = await s.readWifiScan();
      if (_disposed) return;
      _go(OnbStep.wifi);
    } catch (e) {
      _fail(e);
    }
  }

  Future<void> rescanWifi() async {
    final s = _session;
    if (s == null || _linkLost) return;
    try {
      _networks = await s.readWifiScan();
      _notify();
    } catch (e) {
      _fail(e);
    }
  }

  // ------------------------------------------------------------------ send
  Future<void> sendCredentials({required String ssid, required String password}) async {
    final s = _session;
    if (s == null || _step != OnbStep.wifi) return;
    if (_linkLost) {
      _fail(const BleProvisioningException('disconnected'));
      return;
    }
    _errorInfo = null;
    _lastState = 'receiving';
    _status = const ProvisioningStatus(state: 'receiving');
    _go(OnbStep.sending);

    final ClaimToken claim;
    try {
      claim = await _api.createClaimToken(); // fresh, single-use, 10 min
    } on ApiException catch (e) {
      _setError(appProvError('claim_api', detail: e.isNetwork ? 'server non raggiungibile' : e.message));
      return;
    } catch (e) {
      _setError(appProvError('claim_api', detail: e.toString()));
      return;
    }
    if (_disposed || _step != OnbStep.sending) return;
    try {
      await s.sendCredentials(ProvisioningPayload(
        ssid: ssid,
        password: password,
        claimToken: claim.claimToken,
        cloudUrl: _cloudUrl(),
        timezone: _deviceTimezone(),
      ));
    } catch (e) {
      _fail(e);
      return;
    }
    if (_step == OnbStep.sending) {
      _bleTimer?.cancel();
      _bleTimer = Timer(bleCompletionTimeout, _onBleTimeout);
    }
  }

  void _onStatus(ProvisioningStatus st) {
    _status = st;
    if (_step != OnbStep.sending) {
      _notify();
      return;
    }
    _lastState = st.state;
    switch (st.state) {
      case 'done':
        _bleTimer?.cancel();
        unawaited(_resetSession()); // the robot switches BLE off anyway
        _beginOnlineWait();
      case 'error':
        _bleTimer?.cancel();
        _setError(deviceProvError(st.error));
      default:
        _notify();
    }
  }

  void _onLinkError(Object e) {
    _linkLost = true;
    switch (_step) {
      case OnbStep.sending:
        // The robot turns BLE off once registered; if the link drops after
        // Wi-Fi came up, the last notification was probably lost: check the
        // cloud instead of failing.
        if (_lastState == 'wifi_ok' || _lastState == 'registering' || _lastState == 'done') {
          _bleTimer?.cancel();
          _beginOnlineWait();
        } else {
          _fail(e);
        }
      case OnbStep.connecting || OnbStep.wifi:
        _fail(e);
      default:
        break; // already done / failed
    }
  }

  void _onBleTimeout() {
    if (_step != OnbStep.sending) return;
    if (_lastState == 'wifi_ok' || _lastState == 'registering') {
      _beginOnlineWait();
    } else {
      _fail(const BleProvisioningException('timeout'));
    }
  }

  // ---------------------------------------------------------- online wait
  void _beginOnlineWait() {
    if (_info == null) {
      _fail(const BleProvisioningException('gatt', 'device id unknown'));
      return;
    }
    _go(OnbStep.waitingOnline);
    _onlineDeadline = DateTime.now().add(onlineTimeout);
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(onlinePollInterval, (_) => unawaited(_pollOnce()));
    unawaited(_pollOnce());
  }

  /// "Riprova" on the online-timeout page.
  void retryOnlineWait() {
    if (_step == OnbStep.onlineTimeout) _beginOnlineWait();
  }

  Future<void> _pollOnce() async {
    final id = _info?.deviceId;
    if (_polling || id == null || _step != OnbStep.waitingOnline) return;
    _polling = true;
    try {
      var online = _presence(id) == true;
      if (!online) {
        try {
          final list = await _api.listDevices();
          online = list.any((d) => d.deviceId == id && d.online);
        } catch (e) {
          debugPrint('[onb] GET /devices failed: $e');
        }
      }
      if (_disposed || _step != OnbStep.waitingOnline) return;
      if (online) {
        _pollTimer?.cancel();
        _go(OnbStep.online);
      } else if (DateTime.now().isAfter(_onlineDeadline ?? DateTime.now())) {
        _pollTimer?.cancel();
        _go(OnbStep.onlineTimeout);
      }
    } finally {
      _polling = false;
    }
  }

  // ----------------------------------------------------------------- retry
  /// "Riprova" on the error page, according to [ProvErrorInfo.retry].
  Future<void> retry() async {
    final r = _errorInfo?.retry ?? ProvRetry.rescan;
    switch (r) {
      case ProvRetry.wifi:
        if (_session != null && !_linkLost && _info != null) {
          _errorInfo = null;
          _go(OnbStep.wifi);
        } else {
          await startScan();
        }
      case ProvRetry.rescan:
        await startScan();
      case ProvRetry.settings:
        // Back to the intro: the user taps "Cerca robot" after returning
        // from the system settings.
        _errorInfo = null;
        _go(OnbStep.intro);
        await openAppSettings();
    }
  }

  /// IANA zone for the robot. Dart has no IANA zone API; some platforms do
  /// report it as the zone name, otherwise use the spec default.
  String _deviceTimezone() {
    final name = DateTime.now().timeZoneName;
    if (name.contains('/')) return name;
    return Env.defaultTimezone;
  }

  void _fail(Object e) {
    if (e is BleProvisioningException) {
      _setError(appProvError(e.code, detail: e.detail));
    } else if (e is ApiException) {
      _setError(appProvError('claim_api', detail: e.message));
    } else {
      _setError(appProvError('unknown', detail: e.toString()));
    }
  }

  void _setError(ProvErrorInfo info) {
    _bleTimer?.cancel();
    _pollTimer?.cancel();
    if (_disposed) return;
    debugPrint('[onb] error: ${info.message}');
    _errorInfo = info;
    _go(OnbStep.failed);
    if (info.retry == ProvRetry.rescan) unawaited(_resetSession());
  }

  void _go(OnbStep s) {
    _step = s;
    _notify();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  Future<void> _resetSession() async {
    await _stopScan();
    await _statusSub?.cancel();
    _statusSub = null;
    final s = _session;
    _session = null;
    await s?.close();
  }

  @override
  void dispose() {
    _disposed = true;
    _bleTimer?.cancel();
    _pollTimer?.cancel();
    unawaited(_resetSession());
    super.dispose();
  }
}
