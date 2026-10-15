# Static review notes — MVP 1 (first build on real phones)

No Flutter/Dart toolchain was available in the review sandbox (storage.googleapis.com and pub.dev are
blocked), so nothing here was compiled. Instead every third-party API used by `lib/` was checked against
the **source of the pinned release** (shallow clones from GitHub), and the Flutter framework API against
the Flutter **3.44.0** sources (plus 3.32/3.35/3.38/3.41 for the `ThemeData` signatures and the app
templates). Date of review: 2026-10-15.

## 1. Pinned versions and why

| Package | Pin | Checked |
|---|---|---|
| Flutter / Dart | ≥ 3.35 / ≥ 3.9 | go_router 17.2.3 pubspec requires it; ThemeData/AppBarTheme/InputDecorationTheme/Switch/RadioListTile signatures compared 3.32 → 3.44 |
| flutter_webrtc | 1.5.2 (2026-06-20) | tag `v1.5.2`: `createPeerConnection`, `addTransceiver({track, kind, init})`, `RTCRtpTransceiverInit`, `TransceiverDirection.*`, `RTCDataChannelInit` (`id` default 0 → we set −1), `onIceGatheringState`, `iceGatheringState`, `getLocalDescription`, `RTCVideoRenderer.onFirstFrameRendered/onResize/videoWidth`, `RTCVideoView(objectFit, mirror)`, `Helper.setSpeakerphoneOn`, `createLocalMediaStream`, `MediaStream.addTrack(addToNative:)`, `StatsReport(id,type,timestamp,values)`. Android compileSdk 36, minSdk 21, AGP 9 aware; iOS 13.0 |
| webrtc_interface | (transitive ^1.5.1) | enums and interfaces above |
| flutter_blue_plus | 1.36.8 (2025-09-17) | commit `da4b13c`: `connect({timeout, mtu=512, autoConnect})` (we pass `mtu: null`), `requestMtu` (Android-only, throws elsewhere), `mtuNow`, `discoverServices`, `BluetoothService.uuid`, `BluetoothCharacteristic.uuid/read/write(withoutResponse, timeout)/setNotifyValue/onValueReceived`, `startScan(timeout, androidScanMode)`, `scanResults` (reset on start), `isScanning`, `adapterState`, `adapterStateNow`, `turnOn()`, `AndroidScanMode.lowLatency`, `Guid` equality. **Last BSD-3 release** (2.x = commercial license) |
| cryptography | 2.9.0 (2025-11-21) | `X25519()`, `newKeyPairFromSeed`, `sharedSecretKey`, `Hkdf(hmac:, outputLength:)`, `deriveKey(secretKey, nonce, info)`, `Hmac.sha256()`, `Chacha20.poly1305Aead()`, `SecretBox`, `Mac`, `SecretBoxAuthenticationError` |
| flutter_secure_storage | 10.3.1 | `const AndroidOptions()` (no deprecated `encryptedSharedPreferences`; 11.x removed it), `IOSOptions(accessibility: KeychainAccessibility.first_unlock)`; Android Java 17, minSdk 23 |
| permission_handler | 12.0.1 | `[..].request()` → `Map<Permission, PermissionStatus>`, `isGranted/isPermanentlyDenied/isLimited/isRestricted`, `Permission.microphone.isGranted` (status only), `openAppSettings()` |
| go_router | 17.2.3 | `GoRouter(routes, redirect, refreshListenable, initialLocation)`, `GoRoute(path, builder, pageBuilder, routes)`, `GoRouterState.matchedLocation/pathParameters/pageKey`, `context.go/push` |
| provider | 6.1.5+1 | `read` in initState/build allowed (no assert), `watch`, `select` |
| http | 1.5.0 | — |
| web_socket_channel | 3.0.3 | `WebSocketChannel.connect`, `ready` (completes with error on failed handshake → now handled), `closeCode` |
| firebase_core / firebase_messaging | 3.15.2 / 15.2.10 | last line on Firebase iOS SDK 11 (iOS 13); 4.x/16.x need iOS 15 |
| video_player | 2.10.1 | Flutter ≥ 3.29 |
| intl | `any` | **must not be pinned**: flutter_localizations pins intl exactly per Flutter release (0.20.2 on 3.35–3.44); the old `^0.19.0` made `pub get` fail on every Flutter ≥ 3.29 |
| flutter_lints | 6.0.0 | Dart ≥ 3.8 |

## 2. Compile errors found and fixed

1. `ThemeData(cardTheme: CardTheme(...))` — `cardTheme` is `CardThemeData?` on every Flutter ≥ 3.32 → **error**. Now `CardThemeData`.
2. `Clip` model class (core/models/device.dart) clashed with `Clip` from `package:flutter` in `activity_screen.dart` (both imported, name used) → **ambiguous import error**. Renamed to `RobotClip`.
3. `intl: ^0.19.0` vs flutter_localizations' exact pin → **pub get failure**. Now `any`.
4. `Env.signalUri` / `Env.apiBase` (compile-time const URL) replaced by runtime helpers; all call sites updated (`ApiClient(baseUrl:)`, `SignalingClient(uriFor:)`, `LiveSession(mvp:)`, `OnboardingController(api:, cloudUrl:, presence:)`).
5. `flutter create .` would generate `test/widget_test.dart` referencing a non-existent `MyApp` → `flutter test` compile error. A real smoke test now occupies that path.

## 3. Runtime / platform pitfalls found and fixed

- **iOS Info.plist**: the committed plist had no `UIApplicationSceneManifest`; Flutter ≥ 3.38 generates an `AppDelegate` with `FlutterImplicitEngineDelegate` + `SceneDelegate.swift` that relies on it (plugins would not register / black screen), while ≤ 3.35 templates must not have it. The plist is no longer committed: `flutter create` generates the right one and `tools/patch_ios.py` merges our keys (tested on the 3.35 and 3.44 templates, idempotent) and patches the Podfile.
- **Android cleartext**: manifest had `usesCleartextTraffic="false"` → now `true` + `res/xml/network_security_config.xml` (all builds, commented as prototype-only).
- **flutter_blue_plus `connect()` auto-requests MTU 512** on Android by default → now `mtu: null` + explicit `requestMtu(247)` (§9.4); writes sized `min(180, MTU−3)` (`bleMaxWrite`, tested).
- Status notifications are subscribed (`onValueReceived.listen` then `setNotifyValue(true)`) before any write.
- Android ≤ 11: location permission and "location services on" are enforced by flutter_blue_plus inside `startScan`; its errors are now mapped (`location_permission`, `location_off`) instead of a generic failure. Android 12+ no longer prompts for location (manifest `maxSdkVersion=30`).
- iOS adapter state starts `unknown` (and stays so while the permission prompt is visible): wait up to 15 s; `unauthorized` → "open settings".
- `LiveScreen` called `SessionHub.obtain()` (which notifies listeners) synchronously from `initState` in the new flow → moved to a post-frame callback ("markNeedsBuild during build" assertion).
- Live no longer requests the microphone on open (previously it blocked the screen if denied). MVP offer: `video recvonly` + DC `ctl`; ICE gathering ≤ 3 s then send; answer ≤ 15 s; signaling `error` with `to == device:<id>` or `ref == offer` (`peer_offline`, `forbidden`) fails fast with Italian text and *Riconnetti*.
- `LiveSession` teardown is serialized (`_release` returns the in-flight future), every async step of `start()` checks a generation counter, renderers are disposed only after the teardown and never touched after disposal, the answer completer's error is marked handled (`Future.ignore`) so an early release cannot raise an uncaught error.
- Remote video can be attached either via `e.streams` or a synthetic local stream (aiortc may send tracks without msid).
- "Waiting for video" overlay is now translucent and ignores pointers, so close/reconnect stay usable if the first-frame event never arrives (fallback: `onResize` with `videoWidth > 0`).
- Signaling: failed handshake no longer produces an uncaught `WebSocketChannelException`; `error: unauthorized` / close 4401 → sign out instead of reconnecting forever; `sink.add` on a closed sink no longer throws; reconnect immediately when the server URL changes.
- ApiClient: non-JSON 2xx body (wrong server/port) → `ApiException('invalid_response')` with an Italian message instead of a `FormatException`.
- Secure storage reads/writes never throw (keystore errors after backup restore → just log in again).
- Wizard: BLE link drop after `wifi_ok`/`registering` (the robot turns BLE off when done) is treated as probable success → poll the cloud; BLE completion timeout 90 s (pm-core gives nmcli 45 s) then 60 s online wait with guidance; every send uses a fresh claim token.
- DataChannel init `id` set to −1 (libwebrtc allocates the SCTP id from the DTLS role; the default 0 is only valid for a DTLS client, and aiortc answers `setup:active`).

## 4. Checks performed

- Every `.dart` file in `lib/` and `test/` parses with tree-sitter (`tree-sitter-dart-orchard` 0.7.0): 0 errors.
- Every `S.xxx` / `Brand.xxx` reference resolves; every project type used in a file is imported (script).
- No `_` used as a value (Dart ≥ 3.7 wildcard semantics); `(_, __)` lambdas switched to wildcards.
- Name clashes: all project class/enum/typedef names compared against Flutter 3.44 framework + dart:ui,
  flutter_webrtc/webrtc_interface and flutter_blue_plus exported names → none after the `Clip` rename.
- Switch expressions over enums/sealed values are exhaustive (`OnbStep`, `LiveStatus`, `MediaDirection`, `HealthStatus`, `ProvRetry?` with `_`).
- Widget lifecycle: all `TextEditingController`s disposed; stream subscriptions cancelled in `dispose`; `mounted` / `context.mounted` checked after every `await` that touches `context`/`setState`; controllers (`OnboardingController`, `LiveSession`) guard `notifyListeners` after dispose.
- go_router: MVP route set `/login`, `/register`, `/devices`, `/devices/add`, `/devices/live/:id`; full app adds `/d/:id/{live,schedule,settings,activity}`. All `context.go/push` targets go through `liveLocation()` / `deviceLocation()`.
- `docs/INTERFACES.md` §6 paths checked against `cloud/app` (read-only): health is `GET /v1/health` (also `/health`), signaling `/v1/signal?token=`, signaling errors carry `to` and `ref`.
- pm-core (read-only) emits `wifi_connect_failed`, `claim_token_invalid`, `register_failed`, `decrypt: …`, `chunk: …` today; the app maps these **and** the MVP canonical codes (`wifi_auth_failed`, `wifi_not_found`, `wifi_timeout`, `cloud_unreachable`, `claim_invalid`).

## 5. Unit tests added

`test/env_test.dart` (URL normalization, http→ws / https→wss, `/v1/health`), `test/offer_plan_test.dart`
(MVP plan = video recvonly + DC, no mic; full plan with/without mic; SDP summary), `test/error_mapping_test.dart`
(device + app provisioning codes, live errors, health messages, BLE write size), `test/widget_test.dart`
(login screen builds, advanced server field). `prov_crypto_test.dart` unchanged (crypto code untouched).

## 6. Residual risks (watch on the first `flutter run`)

1. **Not compiled.** Analyzer-only issues (lints such as `unawaited_futures`, `unnecessary_underscores`, deprecation infos) are expected; a hard error is not expected but cannot be excluded — paste the first error and fix forward.
2. **Flutter 3.44 + AGP 9**: the 3.44 template uses AGP 9.0.1 with `android.newDsl=false` / `android.builtInKotlin=false`. flutter_blue_plus 1.36.8's Android `build.gradle` still uses the old Groovy DSL (`lintOptions`); if Gradle fails on it, use Flutter 3.38/3.41 or keep those two template flags.
3. `flutter_webrtc` first-frame callback: if the overlay "Connesso, in attesa del video…" never disappears while video is playing behind it, the platform did not emit `didFirstFrameRendered`/`onResize`; the overlay is translucent so the video is still visible.
4. iOS: Local Network permission must be accepted; `NSAllowsArbitraryLoads` is dev-only.
5. pm-core error codes are partly provisional (aliases mapped); an unknown code shows "Configurazione non riuscita (<code>)".
6. `DateTime.now().timeZoneName` is not IANA on Android/iOS → robot timezone defaults to `Europe/Rome`.
7. Firebase pods are still linked (full-app push) even though MVP never initializes Firebase; if `pod install` fails on Firebase, set `--dart-define=PETMATE_FCM=false` (already default) and temporarily remove the two firebase deps + `core/push/push_service.dart` usage.
8. Transitive packages (e.g. `webrtc_interface`, `dart_webrtc`, `permission_handler_android`) resolve to the newest compatible versions: commit `pubspec.lock` after the first green build.
