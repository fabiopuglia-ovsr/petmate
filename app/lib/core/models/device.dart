// Cloud device list / clips / ICE servers / BLE info — INTERFACES.md §6, §7.
import 'json_utils.dart';

class DeviceSummary {
  final String deviceId;
  final String name;
  final bool online;
  final DateTime? lastSeen;
  final String? fw;

  const DeviceSummary({
    required this.deviceId,
    required this.name,
    required this.online,
    this.lastSeen,
    this.fw,
  });

  factory DeviceSummary.fromJson(Json j) => DeviceSummary(
        deviceId: asString(j['device_id']),
        name: asString(j['name']),
        online: asBool(j['online']),
        lastSeen: tsToDate(j['last_seen']),
        fw: asStringOrNull(j['fw']),
      );

  Json toJson() => {
        'device_id': deviceId,
        'name': name,
        'online': online,
        'last_seen': lastSeen?.toUtc().toIso8601String(),
        'fw': fw,
      };

  DeviceSummary copyWith({String? name, bool? online, DateTime? lastSeen}) => DeviceSummary(
        deviceId: deviceId,
        name: name ?? this.name,
        online: online ?? this.online,
        lastSeen: lastSeen ?? this.lastSeen,
        fw: fw,
      );

  String get displayName => name.isNotEmpty ? name : deviceId;
}

/// A recorded clip (§6 `GET /devices/{id}/clips`). Not named `Clip`: that
/// would clash with `Clip` from package:flutter (dart:ui) in widget files.
class RobotClip {
  final String clipId;
  final double ts;
  final double durationS;
  final String? thumbUrl;
  final String url;

  const RobotClip({
    required this.clipId,
    required this.ts,
    required this.durationS,
    this.thumbUrl,
    required this.url,
  });

  factory RobotClip.fromJson(Json j) => RobotClip(
        clipId: asString(j['clip_id']),
        ts: asTs(j['ts']),
        durationS: asDouble(j['duration_s']),
        thumbUrl: asStringOrNull(j['thumb_url']),
        url: asString(j['url']),
      );

  Json toJson() => {
        'clip_id': clipId,
        'ts': ts,
        'duration_s': durationS,
        'thumb_url': thumbUrl,
        'url': url,
      };

  DateTime? get time => tsToDate(ts);
}

class IceServer {
  final List<String> urls;
  final String? username;
  final String? credential;

  const IceServer({required this.urls, this.username, this.credential});

  factory IceServer.fromJson(Json j) => IceServer(
        urls: j['urls'] is String ? [j['urls'] as String] : asStringList(j['urls']),
        username: asStringOrNull(j['username']),
        credential: asStringOrNull(j['credential']),
      );

  /// Shape expected by flutter_webrtc's `createPeerConnection`.
  Json toJson() => {
        'urls': List<String>.of(urls),
        if (username != null) 'username': username,
        if (credential != null) 'credential': credential,
      };
}

class ClaimToken {
  final String claimToken;
  final DateTime? expiresAt;

  const ClaimToken({required this.claimToken, this.expiresAt});

  factory ClaimToken.fromJson(Json j) =>
      ClaimToken(claimToken: asString(j['claim_token']), expiresAt: tsToDate(j['expires_at']));
}

/// BLE characteristic 8f1d0001 (device_info).
class BleDeviceInfo {
  final String deviceId;
  final String model;
  final String fw;
  final String pubkeyB64;

  const BleDeviceInfo({
    required this.deviceId,
    required this.model,
    required this.fw,
    required this.pubkeyB64,
  });

  factory BleDeviceInfo.fromJson(Json j) => BleDeviceInfo(
        deviceId: asString(j['device_id']),
        model: asString(j['model']),
        fw: asString(j['fw']),
        pubkeyB64: asString(j['pubkey']),
      );

  Json toJson() => {'device_id': deviceId, 'model': model, 'fw': fw, 'pubkey': pubkeyB64};
}

/// BLE characteristic 8f1d0002 entry (wifi_scan).
class WifiNetwork {
  final String ssid;
  final int rssi;
  final bool secure;

  const WifiNetwork({required this.ssid, required this.rssi, required this.secure});

  factory WifiNetwork.fromJson(Json j) =>
      WifiNetwork(ssid: asString(j['ssid']), rssi: asInt(j['rssi'], -100), secure: asBool(j['secure'], true));

  Json toJson() => {'ssid': ssid, 'rssi': rssi, 'secure': secure};

  /// 0..4 signal bars.
  int get bars => rssi >= -55 ? 4 : rssi >= -65 ? 3 : rssi >= -75 ? 2 : rssi >= -85 ? 1 : 0;
}

/// BLE characteristic 8f1d0004 (status).
class ProvisioningStatus {
  final String state; // idle|receiving|connecting|wifi_ok|registering|done|error
  final String? error;
  final String? ip;

  const ProvisioningStatus({required this.state, this.error, this.ip});

  factory ProvisioningStatus.fromJson(Json j) => ProvisioningStatus(
        state: asString(j['state'], 'idle'),
        error: asStringOrNull(j['error']),
        ip: asStringOrNull(j['ip']),
      );

  Json toJson() => {'state': state, 'error': error, 'ip': ip};

  bool get isTerminal => state == 'done' || state == 'error';
}
