// Interop test: Dart provisioning crypto must match tools/prov_vector.py
// (Python `cryptography`) byte-for-byte. Vector values are copied from
// tools/prov_vector.json; regenerate with `python3 tools/prov_vector.py`.
import 'dart:convert';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:petmate/core/provisioning/prov_crypto.dart';
import 'package:flutter_test/flutter_test.dart';

Uint8List hex(String h) =>
    Uint8List.fromList([for (var i = 0; i < h.length; i += 2) int.parse(h.substring(i, i + 2), radix: 16)]);

String toHex(List<int> b) => b.map((x) => x.toRadixString(16).padLeft(2, '0')).join();

// ---- vector (tools/prov_vector.json) ----
const deviceId = 'PM-000123';
const appPrivHex = '0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20';
const appPubHex = '07a37cbc142093c8b755dc1b10e86cb426374ad16aa853ed0bdfc0b2b86d1c7c';
const devicePrivHex = '4142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f60';
const devicePubB64 = 'ZLEBsdC+WocEvQePmJUAH8A+jp+VIvGI3RKNmEbUhGY=';
const sharedHex = '26c2c17fdb82161cb21ad16e721315355b64d1763119b10bfc962530dc7cc163';
const keyHex = 'e13af1c0b7ead442bb75678eb09d51bd92f67aa2afe146d313dde19f337d3ce9';
const nonceHex = 'a0a1a2a3a4a5a6a7a8a9aaab';
const plaintextUtf8 =
    '{"ssid":"Casa Rossi","password":"p@ssw0rd-è-lunga!","claim_token":"ct_9f8e7d6c5b4a","cloud_url":"https://api.petmate.example/v1","timezone":"Europe/Rome"}';
const ctHex =
    'c4c33e6c25317a3e81d7176ea906b1399e1ec46d48772b7b3c20c1b0995d1864bf4c70af467bbe0cc0fd04f0f904f94fcea9957cff27784e8e6f18b350c09fd179b3d81b7aac7c9f7f0d86ed2684d1f0f82f30e210d4393e1835558b917428e1c0b1f2b5407113f3a2785a9a6d9fa4cce5a42b85da06b49d63fa15357f078e22fe886854d9d096f2ad3658c39a2620dae2f2dc3e4037e656ddb476cb2539056f7ce7d07b2b0e7889ec26c9';
const messageUtf8 =
    '{"epk":"B6N8vBQgk8i3VdwbEOhstCY3StFqqFPtC9/AsrhtHHw=","nonce":"oKGio6Slpqeoqaqr","ct":"xMM+bCUxej6B1xduqQaxOZ4exG1Idyt7PCDBsJldGGS/THCvRnu+DMD9BPD5BPlPzqmVfP8neE6ObxizUMCf0Xmz2Bt6rHyffw2G7SaE0fD4LzDiENQ5Phg1VYuRdCjhwLHytUBxE/OieFqabZ+kzOWkK4XaBrSdY/oVNX8HjiL+iGhU2dCW8q02WMOaJiDa4vLcPkA35lbdtHbLJTkFb3zn0HsrDniJ7CbJ"}';
const writesHex = [
  '017b2265706b223a2242364e38764251676b38693356647762454f687374435933537446717146507443392f41737268744848773d222c226e6f6e6365223a226f4b47696f36536c7071656f71617172222c226374223a22784d4d2b62435578656a364231786475715161784f5a3465784731496479743750434442734a6c644747532f54484376526e752b444d44394250443542506c507a716d566650386e6545364f6278697a554d436630586d7a32427436',
  '02724879666677324737536145306644344c7a4469454e5135506867315659755264436a68774c487974554278452f4f6965467161625a2b6b7a4f576b4b34586142725364592f6f564e5838486a694c2b694768553264435738713032574d4f614a69446134764c63506b4133356c62647448624c4a546b4662337a6e30487372446e694a3743624a227d',
];

const payload = ProvisioningPayload(
  ssid: 'Casa Rossi',
  password: 'p@ssw0rd-è-lunga!',
  claimToken: 'ct_9f8e7d6c5b4a',
  cloudUrl: 'https://api.petmate.example/v1',
  timezone: 'Europe/Rome',
);

void main() {
  group('provisioning crypto vs Python vector', () {
    test('X25519 public key + shared secret', () async {
      final x = X25519();
      final app = await x.newKeyPairFromSeed(hex(appPrivHex));
      expect(toHex((await app.extractPublicKey()).bytes), appPubHex);
      final shared = await x.sharedSecretKey(
        keyPair: app,
        remotePublicKey: SimplePublicKey(base64Decode(devicePubB64), type: KeyPairType.x25519),
      );
      expect(toHex(await shared.extractBytes()), sharedHex);
    });

    test('HKDF-SHA256 key derivation', () async {
      final key = await ProvisioningCrypto().deriveKey(hex(sharedHex), deviceId);
      expect(toHex(key), keyHex);
    });

    test('plaintext JSON serialization matches byte-for-byte', () {
      expect(jsonEncode(payload.toJson()), plaintextUtf8);
    });

    test('seal() reproduces ciphertext, message and BLE writes exactly', () async {
      final sealed = await ProvisioningCrypto().seal(
        deviceId: deviceId,
        devicePublicKey: base64Decode(devicePubB64),
        payload: payload,
        appPrivateKey: hex(appPrivHex),
        nonce: hex(nonceHex),
      );
      expect(toHex(sealed.appPublicKey), appPubHex);
      expect(toHex(sealed.nonce), nonceHex);
      expect(toHex(sealed.ciphertextWithTag), ctHex);
      expect(utf8.decode(sealed.message), messageUtf8);
      final writes = chunkForBle(sealed.message);
      expect(writes.map(toHex).toList(), writesHex);
    });

    test('device side (open) decrypts reassembled chunks', () async {
      final msg = reassembleBleChunks(writesHex.map(hex).toList());
      final pt = await ProvisioningCrypto().open(
        deviceId: deviceId,
        devicePrivateKey: hex(devicePrivHex),
        message: msg,
      );
      expect(pt, payload.toJson());
    });

    test('wrong device_id (AAD/salt) fails authentication', () async {
      final msg = reassembleBleChunks(writesHex.map(hex).toList());
      await expectLater(
        ProvisioningCrypto().open(deviceId: 'PM-000124', devicePrivateKey: hex(devicePrivHex), message: msg),
        throwsA(isA<SecretBoxAuthenticationError>()),
      );
    });

    test('random keys/nonce round-trip', () async {
      final c = ProvisioningCrypto();
      final dev = await X25519().newKeyPairFromSeed(hex(devicePrivHex));
      final devPub = (await dev.extractPublicKey()).bytes;
      final a = await c.seal(deviceId: deviceId, devicePublicKey: devPub, payload: payload);
      final b = await c.seal(deviceId: deviceId, devicePublicKey: devPub, payload: payload);
      expect(a.nonce, isNot(equals(b.nonce)));
      expect(a.appPublicKey, isNot(equals(b.appPublicKey)));
      expect(await c.open(deviceId: deviceId, devicePrivateKey: hex(devicePrivHex), message: a.message),
          payload.toJson());
    });
  });

  group('BLE chunking', () {
    test('flags: single chunk has first|last', () {
      final w = chunkForBle([1, 2, 3]);
      expect(w, hasLength(1));
      expect(w.single, [0x03, 1, 2, 3]);
    });

    test('every write ≤ 180 bytes; first/middle/last flags', () {
      final msg = List<int>.generate(500, (i) => i & 0xff);
      final w = chunkForBle(msg);
      expect(w.length, 3); // 179 + 179 + 142
      expect(w.every((c) => c.length <= kProvMaxWrite), isTrue);
      expect(w.map((c) => c[0]).toList(), [0x01, 0x00, 0x02]);
      expect(reassembleBleChunks(w), msg);
    });

    test('exact multiple of payload size', () {
      final msg = List<int>.filled(179 * 2, 7);
      final w = chunkForBle(msg);
      expect(w.map((c) => c.length).toList(), [180, 180]);
      expect(w.map((c) => c[0]).toList(), [0x01, 0x02]);
    });

    test('smaller MTU-bounded writes', () {
      final msg = List<int>.generate(100, (i) => i);
      final w = chunkForBle(msg, maxWrite: 20);
      expect(w.every((c) => c.length <= 20), isTrue);
      expect(reassembleBleChunks(w), msg);
    });

    test('reassembly rejects bad framing', () {
      expect(() => reassembleBleChunks([
            [0x00, 1]
          ]), throwsFormatException);
      expect(() => reassembleBleChunks([
            [0x01, 1],
            [0x01, 2]
          ]), throwsFormatException);
    });
  });
}
