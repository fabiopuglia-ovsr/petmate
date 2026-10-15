// One WebRTC session App ↔ robot — INTERFACES.md §4.1, §4.2, §4.3, §6.
//
// The app creates the offer and sends it only after ICE gathering completes
// (no trickle, aiortc compatible). Gathering waits at most 3 s, then whatever
// was gathered is sent (host candidates are enough in the LAN MVP).
//
// Offer layout: see offer_plan.dart. MVP = `video recvonly` + DataChannel
// "ctl"; no microphone/camera access at all.
import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:permission_handler/permission_handler.dart';

import '../api/api_client.dart';
import '../models/commands.dart';
import '../models/config.dart';
import '../models/device.dart';
import '../models/json_utils.dart';
import '../models/robot_event.dart';
import '../models/robot_state.dart';
import '../signaling/signaling_client.dart';
import 'link_quality.dart';
import 'offer_plan.dart';

enum LiveStatus { idle, connecting, connected, failed, closed }

enum TalkMode { pushToTalk, duplex }

/// Internal failure with a machine-readable code (mapped by `S.liveError`).
class LiveFailure implements Exception {
  final String code;
  const LiveFailure(this.code);
  @override
  String toString() => 'LiveFailure($code)';
}

class LiveSession extends ChangeNotifier {
  static const gatherTimeout = Duration(seconds: 3);
  static const answerTimeout = Duration(seconds: 15);
  static const signalingTimeout = Duration(seconds: 10);

  final String deviceId;
  final ApiClient _api;
  final SignalingClient _signaling;

  /// MVP offer (video recvonly + DC, no media permissions).
  final bool mvp;

  LiveSession({
    required this.deviceId,
    required ApiClient api,
    required SignalingClient signaling,
    required this.mvp,
  })  : _api = api,
        _signaling = signaling;

  RTCPeerConnection? _pc;
  RTCDataChannel? _dc;
  RTCRtpSender? _videoSender;
  MediaStream? _micStream;
  MediaStream? _faceStream;
  MediaStream? _remoteStream;
  StreamSubscription<Json>? _sigSub;
  Completer<String>? _answer;
  Timer? _statsTimer;
  final LinkQualityEstimator _quality = LinkQualityEstimator();

  final RTCVideoRenderer remoteRenderer = RTCVideoRenderer();
  final RTCVideoRenderer faceRenderer = RTCVideoRenderer();
  bool _renderersReady = false;
  bool _renderersDisposed = false;

  /// Incremented by every start/release: async continuations of an older
  /// attempt compare against it and bail out.
  int _gen = 0;
  Future<void>? _releasing;
  bool _disposed = false;

  LiveStatus _status = LiveStatus.idle;
  String? _error;
  RobotState? _state;
  final List<RobotEvent> _events = [];
  String _camera = 'front';
  TalkMode _talkMode = TalkMode.pushToTalk;
  bool _talking = false;
  bool _speakerMuted = false;
  bool _faceOn = false;
  LinkQuality _linkQuality = LinkQuality.unknown;
  bool _hasVideo = false;
  bool _micAvailable = false;

  final _eventCtl = StreamController<RobotEvent>.broadcast();
  final _snapshotCtl = StreamController<Uint8List>.broadcast();
  Completer<DeviceConfig>? _configWaiter;

  LiveStatus get status => _status;

  /// Failure code (see `S.liveError`), null while healthy.
  String? get error => _error;
  RobotState? get state => _state;
  List<RobotEvent> get recentEvents => List.unmodifiable(_events);
  Stream<RobotEvent> get events => _eventCtl.stream;
  Stream<Uint8List> get snapshots => _snapshotCtl.stream;
  String get camera => _camera;
  TalkMode get talkMode => _talkMode;
  bool get talking => _talking;
  bool get micLive => _micAvailable && (_talkMode == TalkMode.duplex || _talking);

  /// False when the offer was made without the phone microphone (MVP, or
  /// permission not granted beforehand).
  bool get micAvailable => _micAvailable;
  bool get speakerMuted => _speakerMuted;
  bool get faceOn => _faceOn;
  LinkQuality get linkQuality => _linkQuality;

  /// True once the first remote video frame has been rendered.
  bool get hasVideo => _hasVideo;
  bool get isOpen => _status == LiveStatus.connected && _dc?.state == RTCDataChannelState.RTCDataChannelOpen;

  // ---------------------------------------------------------------- connect
  Future<void> start() async {
    if (_disposed || _status == LiveStatus.connecting || _status == LiveStatus.connected) return;
    // A previous failed attempt may still be tearing down.
    final pending = _releasing;
    if (pending != null) await pending;
    if (_disposed) return;
    final gen = ++_gen;
    _error = null;
    _hasVideo = false;
    _setStatus(LiveStatus.connecting);
    try {
      await _ensureRenderers();
      try {
        await _signaling.ready(timeout: signalingTimeout);
      } on TimeoutException {
        throw const LiveFailure('signaling_down');
      }
      if (_stale(gen)) return;

      // ICE servers are optional in the LAN MVP: host candidates suffice.
      List<IceServer> ice = const [];
      try {
        ice = await _api.iceServers();
      } catch (e) {
        debugPrint('[live] ice-servers unavailable, using host candidates only: $e');
      }
      final micGranted = mvp ? false : await _micAlreadyGranted();
      final plan = buildOfferPlan(mvp: mvp, micGranted: micGranted);
      if (_stale(gen)) return;

      final pc = await createPeerConnection(<String, dynamic>{
        'iceServers': ice.map((e) => e.toJson()).toList(),
        'sdpSemantics': 'unified-plan',
        'bundlePolicy': 'max-bundle',
      });
      if (_stale(gen)) {
        await _safe(pc.close());
        return;
      }
      _pc = pc;
      pc.onTrack = _onTrack;
      pc.onConnectionState = _onConnectionState;

      for (final t in plan.transceivers) {
        await _addTransceiver(pc, t);
      }

      // id -1 = let libwebrtc pick the SCTP stream id from the negotiated DTLS
      // role (RTCDataChannelInit defaults to 0, which is only valid for a
      // DTLS client per RFC 8832; aiortc answers as client → we are server).
      final dcInit = RTCDataChannelInit()
        ..ordered = plan.dataChannelOrdered
        ..id = -1;
      if (_stale(gen)) return;
      final dc = await pc.createDataChannel(plan.dataChannelLabel, dcInit);
      if (_stale(gen)) {
        await _safe(dc.close());
        return;
      }
      _dc = dc;
      dc.onMessage = _onDcMessage;
      dc.onDataChannelState = _onDcState;

      _sigSub = _signaling.messages.listen(_onSignal);

      // Register the gathering listener before setLocalDescription so the
      // "complete" event cannot be missed.
      final gathered = Completer<void>();
      pc.onIceGatheringState = (s) {
        if (s == RTCIceGatheringState.RTCIceGatheringStateComplete && !gathered.isCompleted) {
          gathered.complete();
        }
      };
      final offer = await pc.createOffer(<String, dynamic>{});
      await pc.setLocalDescription(offer);
      if (pc.iceGatheringState != RTCIceGatheringState.RTCIceGatheringStateComplete) {
        // Timeout → send whatever was gathered so far.
        await gathered.future.timeout(gatherTimeout, onTimeout: () {});
      }
      if (_stale(gen)) return;
      final local = await pc.getLocalDescription();
      final sdp = local?.sdp;
      if (sdp == null || sdp.isEmpty) throw const LiveFailure('no_local_sdp');
      debugPrint('[live] offer ${plan.describe()} → ${sdpMediaSummary(sdp)}');

      final answer = Completer<String>();
      // Errors may be delivered before anyone awaits (release during send):
      // mark them handled; the await below still receives them.
      answer.future.ignore();
      _answer = answer;
      if (!_signaling.sendOffer(deviceId, sdp)) throw const LiveFailure('signaling_down');
      final String answerSdp;
      try {
        answerSdp = await answer.future.timeout(answerTimeout);
      } on TimeoutException {
        throw const LiveFailure('answer_timeout');
      }
      if (_stale(gen)) return;
      await pc.setRemoteDescription(RTCSessionDescription(answerSdp, 'answer'));
      _startStats();
    } on LiveFailure catch (e) {
      if (!_stale(gen)) _fail(e.code);
    } catch (e) {
      debugPrint('[live] start failed: $e');
      if (!_stale(gen)) _fail('error');
    }
  }

  /// "Riconnetti": tears down the current connection (if any) and connects
  /// again with a fresh offer.
  Future<void> restart() async {
    if (_disposed) return;
    if (_status == LiveStatus.connecting || _status == LiveStatus.connected) {
      _status = LiveStatus.idle; // shows "connecting"; lets start() run after the teardown
      await _release(sendBye: true);
    } else {
      final r = _releasing;
      if (r != null) await r;
    }
    if (_disposed) return;
    _status = LiveStatus.idle;
    await start();
  }

  bool _stale(int gen) => _disposed || gen != _gen;

  Future<bool> _micAlreadyGranted() async {
    try {
      return await Permission.microphone.isGranted; // status only, never requests
    } catch (_) {
      return false;
    }
  }

  Future<void> _addTransceiver(RTCPeerConnection pc, TransceiverSpec t) async {
    final kind = t.kind == MediaKind.audio ? RTCRtpMediaType.RTCRtpMediaTypeAudio : RTCRtpMediaType.RTCRtpMediaTypeVideo;
    final dir = switch (t.direction) {
      MediaDirection.sendrecv => TransceiverDirection.SendRecv,
      MediaDirection.sendonly => TransceiverDirection.SendOnly,
      MediaDirection.recvonly => TransceiverDirection.RecvOnly,
      MediaDirection.inactive => TransceiverDirection.Inactive,
    };
    if (t.attachMicrophone) {
      try {
        final mic = await navigator.mediaDevices.getUserMedia(<String, dynamic>{
          'audio': {'echoCancellation': true, 'noiseSuppression': true, 'autoGainControl': true},
          'video': false,
        });
        final tracks = mic.getAudioTracks();
        if (tracks.isNotEmpty) {
          _micStream = mic;
          _micAvailable = true;
          tracks.first.enabled = micLive;
          await pc.addTransceiver(
            track: tracks.first,
            kind: kind,
            init: RTCRtpTransceiverInit(direction: dir, streams: [mic]),
          );
          return;
        }
        await _stopStream(mic);
      } catch (e) {
        debugPrint('[live] microphone unavailable, audio recvonly: $e');
      }
      await pc.addTransceiver(kind: kind, init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly));
      return;
    }
    final tr = await pc.addTransceiver(kind: kind, init: RTCRtpTransceiverInit(direction: dir));
    if (t.kind == MediaKind.video && t.direction == MediaDirection.sendrecv) _videoSender = tr.sender;
  }

  Future<void> _ensureRenderers() async {
    if (_renderersReady) return;
    await remoteRenderer.initialize();
    await faceRenderer.initialize();
    void markVideo() {
      if (_disposed || _hasVideo || _status == LiveStatus.failed || _status == LiveStatus.closed) return;
      _hasVideo = true;
      notifyListeners();
    }

    remoteRenderer.onFirstFrameRendered = markVideo;
    // Fallback: some platform/versions only report the size change.
    remoteRenderer.onResize = () {
      if (!_renderersDisposed && remoteRenderer.videoWidth > 0) markVideo();
    };
    _renderersReady = true;
  }

  bool _fromDevice(Json m) {
    final from = asStringOrNull(m['from']);
    return from == null || from == 'device:$deviceId';
  }

  void _onSignal(Json m) {
    switch (m['type']) {
      case 'answer':
        final a = _answer;
        if (_fromDevice(m) && a != null && !a.isCompleted) a.complete(asString(m['sdp']));
      case 'error':
        // Only errors about our offer/peer matter (the server adds `to`/`ref`).
        final relevant = m['to'] == 'device:$deviceId' || m['ref'] == 'offer';
        if (!relevant) return;
        final err = asString(m['error'], 'signaling_error');
        final code = switch (err) {
          'peer_offline' => 'peer_offline',
          'forbidden' => 'forbidden',
          _ => 'signaling_error',
        };
        _failAnswer(code);
        if (_status == LiveStatus.connecting || _status == LiveStatus.connected) _fail(code);
      case 'ice': // optional (§4.1): accept if the robot sends any
        if (!_fromDevice(m)) return;
        final c = asMap(m['candidate']);
        final cand = asStringOrNull(c['candidate']);
        final pc = _pc;
        if (pc != null && cand != null && cand.isNotEmpty) {
          unawaited(_safe(pc.addCandidate(
              RTCIceCandidate(cand, asStringOrNull(c['sdpMid']), asIntOrNull(c['sdpMLineIndex'])))));
        }
      case 'bye':
        if (!_fromDevice(m)) return;
        _failAnswer('remote_bye');
        _fail('remote_bye');
      case 'presence':
        if (asString(m['device_id']) == deviceId && !asBool(m['online'], true)) {
          _failAnswer('peer_offline');
          _fail('peer_offline');
        }
    }
  }

  void _failAnswer(String code) {
    final a = _answer;
    if (a != null && !a.isCompleted) a.completeError(LiveFailure(code));
  }

  void _onTrack(RTCTrackEvent e) {
    if (_disposed) return;
    final track = e.track;
    if (e.streams.isNotEmpty) {
      _remoteStream = e.streams.first;
      _setRemoteSrc(e.streams.first);
    } else {
      unawaited(() async {
        try {
          final s = _remoteStream ?? await createLocalMediaStream('petmate-remote');
          _remoteStream = s;
          await s.addTrack(track, addToNative: true);
          _setRemoteSrc(s);
        } catch (err) {
          debugPrint('[live] cannot attach remote track: $err');
        }
      }());
    }
    if (track.kind == 'audio') track.enabled = !_speakerMuted;
    notifyListeners();
  }

  void _setRemoteSrc(MediaStream? s) {
    if (!_renderersReady || _renderersDisposed) return;
    try {
      remoteRenderer.srcObject = s;
    } catch (err) {
      debugPrint('[live] renderer: $err');
    }
  }

  void _onConnectionState(RTCPeerConnectionState s) {
    if (_disposed) return;
    switch (s) {
      case RTCPeerConnectionState.RTCPeerConnectionStateConnected:
        if (_status == LiveStatus.connecting) _setStatus(LiveStatus.connected);
        if (!mvp && _micAvailable) unawaited(_safe(Helper.setSpeakerphoneOn(true)));
      case RTCPeerConnectionState.RTCPeerConnectionStateFailed:
        _fail('ice_failed');
      case RTCPeerConnectionState.RTCPeerConnectionStateDisconnected:
        _linkQuality = LinkQuality.poor; // may recover by itself
        notifyListeners();
      default:
        break;
    }
  }

  // ------------------------------------------------------------ DataChannel
  void _onDcState(RTCDataChannelState s) {
    if (_disposed) return;
    if (s == RTCDataChannelState.RTCDataChannelOpen) {
      if (!mvp) send(Cmd.camera(_camera));
    } else if (s == RTCDataChannelState.RTCDataChannelClosed && _status == LiveStatus.connected) {
      _fail('datachannel_closed');
    }
    notifyListeners();
  }

  void _onDcMessage(RTCDataChannelMessage msg) {
    if (_disposed || msg.isBinary) return;
    Json j;
    try {
      final d = jsonDecode(msg.text);
      if (d is! Map) return;
      j = asMap(d);
    } catch (_) {
      return;
    }
    switch (j['t']) {
      case 'state':
        _state = RobotState.fromJson(j);
        notifyListeners();
      case 'event':
        final ev = RobotEvent.fromJson(j);
        _events.insert(0, ev);
        if (_events.length > 100) _events.removeLast();
        if (!_eventCtl.isClosed) _eventCtl.add(ev);
        notifyListeners();
      case 'snapshot':
        final b64 = asStringOrNull(j['jpeg_b64']);
        if (b64 != null && !_snapshotCtl.isClosed) {
          try {
            _snapshotCtl.add(base64Decode(b64));
          } on FormatException {
            // ignore malformed snapshot
          }
        }
      case 'config':
      case 'config_get':
        // §9.2: {"t":"config","config":{...}}
        final cfg = j['config'];
        final w = _configWaiter;
        if (cfg is Map && w != null && !w.isCompleted) w.complete(DeviceConfig.fromJson(asMap(cfg)));
    }
  }

  /// Sends a §4.2 command over "ctl". Returns false when the channel is not open.
  bool send(Json cmd) {
    final dc = _dc;
    if (dc == null || dc.state != RTCDataChannelState.RTCDataChannelOpen) return false;
    unawaited(_safe(dc.send(RTCDataChannelMessage(jsonEncode(cmd)))));
    return true;
  }

  Future<DeviceConfig> fetchConfig({Duration timeout = const Duration(seconds: 5)}) {
    final w = Completer<DeviceConfig>();
    _configWaiter = w;
    if (!send(Cmd.configGet())) return Future<DeviceConfig>.error(StateError('not_open'));
    return w.future.timeout(timeout);
  }

  bool pushConfig(DeviceConfig cfg) => send(Cmd.configSet(cfg.toJson()));

  /// Asks the robot for a JPEG (`{"t":"snapshot"}` → `{"t":"snapshot","jpeg_b64":…}`).
  bool requestSnapshot() => send(Cmd.snapshot());

  // ------------------------------------------------------------ controls
  void switchCamera(String id) {
    if (send(Cmd.camera(id))) {
      _camera = id;
      notifyListeners();
    }
  }

  void setTalkMode(TalkMode m) {
    _talkMode = m;
    _talking = false;
    _applyMic();
  }

  /// Push-to-talk press/release.
  void setTalking(bool on) {
    if (_talkMode != TalkMode.pushToTalk) return;
    _talking = on;
    _applyMic();
  }

  void _applyMic() {
    for (final t in _micStream?.getAudioTracks() ?? const <MediaStreamTrack>[]) {
      t.enabled = micLive;
    }
    notifyListeners();
  }

  void setSpeakerMuted(bool muted) {
    _speakerMuted = muted;
    for (final t in _remoteStream?.getAudioTracks() ?? const <MediaStreamTrack>[]) {
      t.enabled = !muted;
    }
    notifyListeners();
  }

  /// "Show my face on the robot" (full app only): sends the phone's front
  /// camera on the video m-line and switches the round display to owner mode.
  /// The caller must have obtained the camera permission.
  Future<void> setFace(bool on) async {
    if (on == _faceOn || _videoSender == null) return;
    try {
      if (on) {
        final face = await navigator.mediaDevices.getUserMedia(<String, dynamic>{
          'audio': false,
          'video': {
            'facingMode': 'user',
            'width': {'ideal': 640},
            'height': {'ideal': 480},
            'frameRate': {'ideal': 15},
          },
        });
        _faceStream = face;
        await _videoSender?.replaceTrack(face.getVideoTracks().first);
        if (!_renderersDisposed) faceRenderer.srcObject = face;
        send(Cmd.display('owner'));
      } else {
        await _videoSender?.replaceTrack(null);
        if (!_renderersDisposed) faceRenderer.srcObject = null;
        await _stopStream(_faceStream);
        _faceStream = null;
        send(Cmd.display('face'));
      }
      _faceOn = on;
    } catch (e) {
      debugPrint('[live] face: $e');
    }
    notifyListeners();
  }

  // ------------------------------------------------------------ stats
  void _startStats() {
    _statsTimer?.cancel();
    _statsTimer = Timer.periodic(const Duration(seconds: 2), (_) async {
      final pc = _pc;
      if (pc == null || _disposed) return;
      try {
        final reports = await pc.getStats();
        final q = _quality.update(reports.map((r) => StatsSample(r.id, r.type, r.values)).toList());
        if (q != _linkQuality && !_disposed) {
          _linkQuality = q;
          notifyListeners();
        }
      } catch (_) {}
    });
  }

  double? get rttMs => _quality.rttMs;
  double? get fps => _quality.fps;
  bool get relayed => _quality.relayed;

  // ------------------------------------------------------------ teardown
  void _fail(String reason) {
    if (_status == LiveStatus.closed || _status == LiveStatus.failed) return;
    _error = reason;
    _setStatus(LiveStatus.failed);
    unawaited(_release(sendBye: reason != 'remote_bye' && reason != 'peer_offline'));
  }

  Future<void> close() async {
    if (_status == LiveStatus.closed) {
      final r = _releasing;
      if (r != null) await r;
      return;
    }
    _setStatus(LiveStatus.closed);
    await _release(sendBye: true);
  }

  /// Serialized: concurrent callers await the same teardown.
  Future<void> _release({required bool sendBye}) {
    final current = _releasing;
    if (current != null) return current;
    final f = _doRelease(sendBye: sendBye);
    _releasing = f;
    return f.whenComplete(() {
      if (identical(_releasing, f)) _releasing = null;
    });
  }

  Future<void> _doRelease({required bool sendBye}) async {
    _gen++; // invalidate in-flight start()
    _failAnswer('closed');
    _answer = null;
    _statsTimer?.cancel();
    _statsTimer = null;
    final sub = _sigSub;
    _sigSub = null;
    await _safe(sub?.cancel());
    if (sendBye) _signaling.sendBye(deviceId);
    _configWaiter = null;
    final dc = _dc;
    _dc = null;
    await _safe(dc?.close());
    final pc = _pc;
    _pc = null;
    await _safe(pc?.close());
    await _safe(pc?.dispose());
    await _stopStream(_micStream);
    await _stopStream(_faceStream);
    _micStream = null;
    _faceStream = null;
    _remoteStream = null;
    _videoSender = null;
    _micAvailable = false;
    _faceOn = false;
    _hasVideo = false;
    if (_renderersReady && !_renderersDisposed) {
      try {
        remoteRenderer.srcObject = null;
        faceRenderer.srcObject = null;
      } catch (_) {}
    }
    notifyListeners();
  }

  Future<void> _stopStream(MediaStream? s) async {
    if (s == null) return;
    for (final t in s.getTracks()) {
      await _safe(t.stop());
    }
    await _safe(s.dispose());
  }

  static Future<void> _safe(Future<Object?>? f) async {
    if (f == null) return;
    try {
      await f;
    } catch (_) {}
  }

  void _setStatus(LiveStatus s) {
    _status = s;
    notifyListeners();
  }

  @override
  void notifyListeners() {
    if (!_disposed) super.notifyListeners();
  }

  @override
  void dispose() {
    if (_disposed) return;
    // close() must run its teardown before _disposed suppresses callbacks.
    final closing = close();
    _disposed = true;
    unawaited(closing.whenComplete(() async {
      if (_renderersReady && !_renderersDisposed) {
        _renderersDisposed = true;
        await _safe(remoteRenderer.dispose());
        await _safe(faceRenderer.dispose());
      }
      await _eventCtl.close();
      await _snapshotCtl.close();
    }));
    super.dispose();
  }
}
