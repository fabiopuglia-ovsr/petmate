// BLE provisioning payload encryption + chunking — INTERFACES.md §7.
//
// Pure Dart (package:cryptography) so it is unit-testable without Flutter and
// verified byte-for-byte against tools/prov_vector.py (Python `cryptography`).
import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';

const String kProvHkdfInfo = 'petmate-prov-v1';

/// Max bytes per BLE write including the 1-byte flags header (§7.6).
const int kProvMaxWrite = 180;

const int kFlagFirst = 0x01;
const int kFlagLast = 0x02;

class ProvisioningPayload {
  final String ssid;
  final String password;
  final String claimToken;
  final String cloudUrl;
  final String timezone;

  const ProvisioningPayload({
    required this.ssid,
    required this.password,
    required this.claimToken,
    required this.cloudUrl,
    required this.timezone,
  });

  /// Key order matches §7.3 exactly.
  Map<String, dynamic> toJson() => {
        'ssid': ssid,
        'password': password,
        'claim_token': claimToken,
        'cloud_url': cloudUrl,
        'timezone': timezone,
      };
}

/// Result of [ProvisioningCrypto.seal] — exposed fields allow test vectors.
class SealedProvisioning {
  final Uint8List appPublicKey;
  final Uint8List nonce;
  final Uint8List ciphertextWithTag;

  /// UTF-8 JSON `{"epk":..,"nonce":..,"ct":..}`.
  final Uint8List message;

  const SealedProvisioning({
    required this.appPublicKey,
    required this.nonce,
    required this.ciphertextWithTag,
    required this.message,
  });
}

class ProvisioningCrypto {
  final X25519 _x25519;
  final Hkdf _hkdf;
  final Chacha20 _aead;
  final Random _random;

  ProvisioningCrypto({Random? random})
      : _x25519 = X25519(),
        _hkdf = Hkdf(hmac: Hmac.sha256(), outputLength: 32),
        _aead = Chacha20.poly1305Aead(),
        _random = random ?? Random.secure();

  /// HKDF-SHA256(shared, salt=utf8(deviceId), info="petmate-prov-v1", L=32).
  Future<Uint8List> deriveKey(List<int> shared, String deviceId) async {
    final k = await _hkdf.deriveKey(
      secretKey: SecretKey(shared),
      nonce: utf8.encode(deviceId), // package:cryptography calls the salt "nonce"
      info: utf8.encode(kProvHkdfInfo),
    );
    return Uint8List.fromList(await k.extractBytes());
  }

  /// Encrypts [payload] for the device.
  ///
  /// [appPrivateKey] (32 raw bytes) and [nonce] (12 bytes) are injectable for
  /// deterministic tests; in production both are fresh random values.
  Future<SealedProvisioning> seal({
    required String deviceId,
    required List<int> devicePublicKey,
    required ProvisioningPayload payload,
    List<int>? appPrivateKey,
    List<int>? nonce,
  }) async {
    if (devicePublicKey.length != 32) {
      throw ArgumentError('device pubkey must be 32 bytes (X25519 raw)');
    }
    final seed = appPrivateKey ?? _randomBytes(32);
    final keyPair = await _x25519.newKeyPairFromSeed(seed);
    final appPub = await keyPair.extractPublicKey();
    final shared = await _x25519.sharedSecretKey(
      keyPair: keyPair,
      remotePublicKey: SimplePublicKey(devicePublicKey, type: KeyPairType.x25519),
    );
    final key = await deriveKey(await shared.extractBytes(), deviceId);

    final n = nonce ?? _randomBytes(12);
    if (n.length != 12) throw ArgumentError('nonce must be 12 bytes');
    final box = await _aead.encrypt(
      utf8.encode(jsonEncode(payload.toJson())),
      secretKey: SecretKey(key),
      nonce: n,
      aad: utf8.encode(deviceId),
    );
    final ct = Uint8List.fromList([...box.cipherText, ...box.mac.bytes]);
    final pub = Uint8List.fromList(appPub.bytes);
    final msg = utf8.encode(jsonEncode({
      'epk': base64Encode(pub),
      'nonce': base64Encode(n),
      'ct': base64Encode(ct),
    }));
    return SealedProvisioning(
      appPublicKey: pub,
      nonce: Uint8List.fromList(n),
      ciphertextWithTag: ct,
      message: Uint8List.fromList(msg),
    );
  }

  /// Device-side inverse (used by tests and for self-checks).
  Future<Map<String, dynamic>> open({
    required String deviceId,
    required List<int> devicePrivateKey,
    required List<int> message,
  }) async {
    final m = jsonDecode(utf8.decode(message)) as Map<String, dynamic>;
    final epk = base64Decode(m['epk'] as String);
    final nonce = base64Decode(m['nonce'] as String);
    final ct = base64Decode(m['ct'] as String);
    final kp = await _x25519.newKeyPairFromSeed(devicePrivateKey);
    final shared = await _x25519.sharedSecretKey(
      keyPair: kp,
      remotePublicKey: SimplePublicKey(epk, type: KeyPairType.x25519),
    );
    final key = await deriveKey(await shared.extractBytes(), deviceId);
    final clear = await _aead.decrypt(
      SecretBox(ct.sublist(0, ct.length - 16),
          nonce: nonce, mac: Mac(ct.sublist(ct.length - 16))),
      secretKey: SecretKey(key),
      aad: utf8.encode(deviceId),
    );
    return jsonDecode(utf8.decode(clear)) as Map<String, dynamic>;
  }

  Uint8List _randomBytes(int n) =>
      Uint8List.fromList(List<int>.generate(n, (_) => _random.nextInt(256)));
}

/// Largest BLE write (flags byte included) for a negotiated ATT [mtu]:
/// ≤ 180 B (§9.4) and ≤ MTU − 3 (ATT write header). Default MTU 23 → 20 B.
int bleMaxWrite(int mtu) => mtu > 23 ? min(max(mtu - 3, 20), kProvMaxWrite) : 20;

/// Splits [message] into BLE writes `[flags:u8][data]`, each ≤ [maxWrite]
/// bytes in total. flags bit0 = first, bit1 = last (a single chunk has both).
List<Uint8List> chunkForBle(List<int> message, {int maxWrite = kProvMaxWrite}) {
  if (maxWrite < 2) throw ArgumentError('maxWrite must be ≥ 2');
  final size = maxWrite - 1;
  final count = message.isEmpty ? 1 : (message.length + size - 1) ~/ size;
  return List<Uint8List>.generate(count, (i) {
    final start = i * size;
    final end = min(start + size, message.length);
    final flags = (i == 0 ? kFlagFirst : 0) | (i == count - 1 ? kFlagLast : 0);
    return Uint8List.fromList([flags, ...message.sublist(start, end)]);
  });
}

/// Reassembles writes produced by [chunkForBle]; throws on bad framing.
Uint8List reassembleBleChunks(List<List<int>> writes) {
  if (writes.isEmpty) throw const FormatException('no chunks');
  final out = <int>[];
  for (var i = 0; i < writes.length; i++) {
    final w = writes[i];
    if (w.isEmpty) throw const FormatException('empty write');
    final first = w[0] & kFlagFirst != 0;
    final last = w[0] & kFlagLast != 0;
    if (first != (i == 0)) throw const FormatException('bad first flag');
    if (last != (i == writes.length - 1)) throw const FormatException('bad last flag');
    out.addAll(w.sublist(1));
  }
  return Uint8List.fromList(out);
}
