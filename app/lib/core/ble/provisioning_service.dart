// BLE central side of provisioning — INTERFACES.md §7 (+ §9.4: 180 B writes
// including the flags byte, Android MTU request 247).
import 'dart:async';
import 'dart:convert';
import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:permission_handler/permission_handler.dart';

import '../models/device.dart';
import '../models/json_utils.dart';
import '../provisioning/prov_crypto.dart';
import 'ble_uuids.dart';

class BleProvisioningException implements Exception {
  /// permission | permission_permanent | location_permission | location_off |
  /// bluetooth_off | bluetooth_unauthorized | unsupported | connect | gatt |
  /// crypto | write | disconnected | timeout
  final String code;
  final String? detail;
  const BleProvisioningException(this.code, [this.detail]);
  @override
  String toString() => 'BleProvisioningException($code${detail == null ? '' : ': $detail'})';
}

/// Discovered robot in provisioning mode.
class FoundRobot {
  final BluetoothDevice device;
  final String name;
  final int rssi;
  const FoundRobot(this.device, this.name, this.rssi);
}

/// Android 12+ (API 31+): runtime BLUETOOTH_SCAN + BLUETOOTH_CONNECT (the
/// manifest declares SCAN with `neverForLocation`). On Android ≤ 11 these are
/// install-time and permission_handler reports them granted without a dialog;
/// the location permission those versions need is requested by
/// flutter_blue_plus itself when the scan starts (it knows the API level),
/// failures are mapped to `location_permission` / `location_off`.
/// iOS: the system prompt appears on first CoreBluetooth use; the outcome is
/// read from the adapter state (`unauthorized`).
Future<void> ensureBlePermissions() async {
  if (!Platform.isAndroid) return;
  final res = await [Permission.bluetoothScan, Permission.bluetoothConnect].request();
  final statuses = [
    res[Permission.bluetoothScan] ?? PermissionStatus.denied,
    res[Permission.bluetoothConnect] ?? PermissionStatus.denied,
  ];
  if (statuses.every((s) => s.isGranted || s.isLimited)) return;
  if (statuses.any((s) => s.isPermanentlyDenied || s.isRestricted)) {
    throw const BleProvisioningException('permission_permanent');
  }
  throw const BleProvisioningException('permission');
}

/// Makes sure the adapter is on. Android: shows the system "turn on
/// Bluetooth" dialog. iOS: cannot be turned on by apps → `bluetooth_off`.
Future<void> ensureAdapterOn({Duration wait = const Duration(seconds: 15)}) async {
  if (await FlutterBluePlus.isSupported == false) {
    throw const BleProvisioningException('unsupported');
  }
  // iOS reports `unknown` until CoreBluetooth initialises (and while the
  // permission prompt is on screen): wait for a definite state.
  BluetoothAdapterState state;
  try {
    state = await FlutterBluePlus.adapterState
        .where((s) => s != BluetoothAdapterState.unknown && s != BluetoothAdapterState.turningOn)
        .first
        .timeout(wait);
  } on TimeoutException {
    state = FlutterBluePlus.adapterStateNow;
  }
  if (state == BluetoothAdapterState.on) return;
  if (state == BluetoothAdapterState.unauthorized) {
    throw const BleProvisioningException('bluetooth_unauthorized');
  }
  if (state == BluetoothAdapterState.unavailable) {
    throw const BleProvisioningException('unsupported');
  }
  if (Platform.isAndroid) {
    try {
      await FlutterBluePlus.turnOn(); // returns when on, throws if the user declines
    } catch (_) {
      throw const BleProvisioningException('bluetooth_off');
    }
    try {
      await FlutterBluePlus.adapterState
          .where((s) => s == BluetoothAdapterState.on)
          .first
          .timeout(const Duration(seconds: 10));
      return;
    } on TimeoutException {
      throw const BleProvisioningException('bluetooth_off');
    }
  }
  throw const BleProvisioningException('bluetooth_off');
}

class ProvisioningSession {
  final BluetoothDevice device;
  BleDeviceInfo? info;
  final _statusCtl = StreamController<ProvisioningStatus>.broadcast();
  StreamSubscription<List<int>>? _notifySub;
  StreamSubscription<BluetoothConnectionState>? _connSub;
  BluetoothCharacteristic? _cInfo, _cScan, _cPayload, _cStatus;
  int _mtu = 23;
  bool _closed = false;

  ProvisioningSession(this.device);

  /// Status notifications (8f1d0004). Emits a `disconnected` error when the
  /// BLE link drops.
  Stream<ProvisioningStatus> get status => _statusCtl.stream;

  int get mtu => _mtu;

  Future<void> connect() async {
    try {
      // mtu: null → no automatic MTU request inside connect(); done below
      // explicitly (Android only) as required by §9.4.
      await device.connect(timeout: const Duration(seconds: 15), mtu: null);
    } catch (e) {
      throw BleProvisioningException('connect', e.toString());
    }
    _connSub = device.connectionState.listen((s) {
      if (s == BluetoothConnectionState.disconnected && !_statusCtl.isClosed) {
        _statusCtl.addError(const BleProvisioningException('disconnected'));
      }
    });
    if (Platform.isAndroid) {
      try {
        _mtu = await device.requestMtu(247);
      } catch (e) {
        debugPrint('[ble] requestMtu failed, keeping $_mtu: $e');
      }
    } else {
      _mtu = device.mtuNow; // iOS negotiates automatically (usually 185+)
    }
    final List<BluetoothService> services;
    try {
      services = await device.discoverServices();
    } catch (e) {
      throw BleProvisioningException('gatt', 'discoverServices: $e');
    }
    final svcGuid = Guid(PetmateBle.service);
    final svc = services.where((s) => s.uuid == svcGuid).firstOrNull;
    if (svc == null) throw const BleProvisioningException('gatt', 'service not found');
    BluetoothCharacteristic? find(String u) {
      final g = Guid(u);
      return svc.characteristics.where((c) => c.uuid == g).firstOrNull;
    }

    _cInfo = find(PetmateBle.deviceInfo);
    _cScan = find(PetmateBle.wifiScan);
    _cPayload = find(PetmateBle.payload);
    _cStatus = find(PetmateBle.status);
    final cStatus = _cStatus;
    if (_cInfo == null || _cScan == null || _cPayload == null || cStatus == null) {
      throw const BleProvisioningException('gatt', 'characteristic missing');
    }
    // Subscribe BEFORE any write so no status transition is missed.
    _notifySub = cStatus.onValueReceived.listen(_onStatusBytes);
    try {
      await cStatus.setNotifyValue(true);
    } catch (e) {
      throw BleProvisioningException('gatt', 'notify: $e');
    }
  }

  void _onStatusBytes(List<int> v) {
    if (_statusCtl.isClosed) return;
    try {
      _statusCtl.add(ProvisioningStatus.fromJson(asMap(jsonDecode(utf8.decode(v)))));
    } catch (_) {
      // partial/invalid JSON: ignore
    }
  }

  Future<BleDeviceInfo> readInfo() async {
    final BleDeviceInfo i;
    try {
      final raw = await _cInfo!.read();
      i = BleDeviceInfo.fromJson(asMap(jsonDecode(utf8.decode(raw))));
    } on BleProvisioningException {
      rethrow;
    } catch (e) {
      throw BleProvisioningException('gatt', 'device_info: $e');
    }
    if (i.deviceId.isEmpty || i.pubkeyB64.isEmpty) {
      throw const BleProvisioningException('gatt', 'invalid device_info');
    }
    info = i;
    return i;
  }

  Future<List<WifiNetwork>> readWifiScan() async {
    final List<Object?> raw;
    try {
      raw = asList(jsonDecode(utf8.decode(await _cScan!.read())));
    } catch (e) {
      throw BleProvisioningException('gatt', 'wifi_scan: $e');
    }
    final list = raw.map((e) => WifiNetwork.fromJson(asMap(e))).where((n) => n.ssid.isNotEmpty).toList();
    // De-duplicate SSIDs (keep strongest) and sort by signal.
    final best = <String, WifiNetwork>{};
    for (final n in list) {
      final cur = best[n.ssid];
      if (cur == null || cur.rssi < n.rssi) best[n.ssid] = n;
    }
    return best.values.toList()..sort((a, b) => b.rssi.compareTo(a.rssi));
  }

  /// Encrypts, chunks (≤ 180 B per write incl. flags, also ≤ ATT MTU − 3) and
  /// writes the provisioning payload with write-with-response.
  Future<void> sendCredentials(ProvisioningPayload payload) async {
    final i = info ?? await readInfo();
    final SealedProvisioning sealed;
    try {
      sealed = await ProvisioningCrypto().seal(
        deviceId: i.deviceId,
        devicePublicKey: base64Decode(i.pubkeyB64),
        payload: payload,
      );
    } catch (e) {
      throw BleProvisioningException('crypto', e.toString());
    }
    final maxWrite = bleMaxWrite(_mtu);
    final writes = chunkForBle(sealed.message, maxWrite: maxWrite);
    debugPrint('[ble] sending ${sealed.message.length} B in ${writes.length} writes (mtu $_mtu, max $maxWrite)');
    try {
      for (final w in writes) {
        await _cPayload!.write(w, withoutResponse: false, timeout: 10);
      }
    } catch (e) {
      throw BleProvisioningException('write', e.toString());
    }
  }

  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    await _notifySub?.cancel();
    await _connSub?.cancel();
    await _statusCtl.close();
    try {
      await device.disconnect();
    } catch (_) {}
  }
}

class BleScanner {
  static const scanTimeout = Duration(seconds: 15);

  /// Scans for `Petmate-XXXX` advertisers / the provisioning service UUID.
  /// Call [ensureBlePermissions] and [ensureAdapterOn] first.
  Future<Stream<List<FoundRobot>>> start() async {
    // No OS-level filter: some stacks put the 128-bit service UUID in the
    // scan response only, so match in Dart on name prefix OR service UUID.
    try {
      await FlutterBluePlus.startScan(timeout: scanTimeout, androidScanMode: AndroidScanMode.lowLatency);
    } catch (e) {
      final s = e.toString();
      if (s.contains('Location services')) throw const BleProvisioningException('location_off');
      if (s.contains('ACCESS_FINE_LOCATION')) throw const BleProvisioningException('location_permission');
      if (s.contains('BLUETOOTH_SCAN') || s.contains('BLUETOOTH_CONNECT')) {
        throw const BleProvisioningException('permission');
      }
      throw BleProvisioningException('scan', s);
    }
    final svc = Guid(PetmateBle.service);
    return FlutterBluePlus.scanResults.map((results) {
      final out = <FoundRobot>[];
      for (final r in results) {
        final adv = r.advertisementData.advName;
        final name = adv.isNotEmpty ? adv : r.device.platformName;
        final hasSvc = r.advertisementData.serviceUuids.contains(svc);
        if (name.startsWith(PetmateBle.namePrefix) || hasSvc) {
          out.add(FoundRobot(r.device, name.isEmpty ? r.device.remoteId.str : name, r.rssi));
        }
      }
      out.sort((a, b) => b.rssi.compareTo(a.rssi));
      return out;
    });
  }

  /// Emits false when the scan ends (timeout or stop).
  Stream<bool> get isScanning => FlutterBluePlus.isScanning;

  Future<void> stop() async {
    try {
      await FlutterBluePlus.stopScan();
    } catch (_) {}
  }
}
