import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:provider/provider.dart';

import '../../core/env.dart';
import '../../core/models/commands.dart';
import '../../core/models/robot_event.dart';
import '../../core/models/robot_state.dart';
import '../../core/teleop/teleop_controller.dart';
import '../../core/webrtc/link_quality.dart';
import '../../core/webrtc/live_session.dart';
import '../../core/webrtc/session_hub.dart';
import '../../core/widgets/common.dart';
import '../../core/widgets/theme.dart';
import '../../l10n/strings.dart';
import '../devices/devices_controller.dart';
import '../settings/settings_screen.dart' show MapPoiPanel;
import 'joystick.dart';

/// Live view. Opening it never asks for microphone/camera permission: the
/// MVP offer is video recvonly + DataChannel "ctl" (see offer_plan.dart).
class LiveScreen extends StatefulWidget {
  final String deviceId;
  const LiveScreen({super.key, required this.deviceId});

  @override
  State<LiveScreen> createState() => _LiveScreenState();
}

class _LiveScreenState extends State<LiveScreen> {
  late final SessionHub _hub;
  LiveSession? _s;
  TeleopController? _teleop;
  StreamSubscription<RobotEvent>? _evSub;
  StreamSubscription<Uint8List>? _snapSub;
  double _speed = 0.6;

  @override
  void initState() {
    super.initState();
    _hub = context.read<SessionHub>();
    unawaited(SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky));
    // obtain() notifies SessionHub listeners: never do it during build.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _attach(_hub.obtain(widget.deviceId));
    });
  }

  void _attach(LiveSession s) {
    if (!identical(s, _s)) {
      unawaited(_evSub?.cancel());
      unawaited(_snapSub?.cancel());
      _teleop?.dispose();
      _s = s;
      _teleop = TeleopController((cmd) => s.send(cmd));
      _evSub = s.events.listen((e) {
        if (mounted && !Env.mvp) showSnack(context, S.eventTitle(e.type));
      });
      _snapSub = s.snapshots.listen(_showSnapshot);
    }
    setState(() {});
  }

  void _reconnect() => _attach(_hub.reconnect(widget.deviceId));

  @override
  void dispose() {
    _teleop?.release();
    _teleop?.dispose();
    unawaited(_evSub?.cancel());
    unawaited(_snapSub?.cancel());
    unawaited(SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge));
    // Closing the screen ends the live session (camera/mic are privacy sensitive).
    unawaited(_hub.closeSession());
    super.dispose();
  }

  void _showSnapshot(Uint8List jpeg) {
    if (!mounted) return;
    unawaited(showDialog<void>(
      context: context,
      builder: (ctx) => Dialog(
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Image.memory(jpeg, fit: BoxFit.contain, errorBuilder: (_, _, _) => const Padding(
                padding: EdgeInsets.all(24),
                child: Text(S.genericError),
              )),
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text(S.close)),
        ]),
      ),
    ));
  }

  void _send(Map<String, dynamic> cmd, [String? okMsg]) {
    final ok = _s?.send(cmd) ?? false;
    if (!ok) {
      showSnack(context, S.requiresLive, error: true);
    } else if (okMsg != null) {
      showSnack(context, okMsg);
    }
  }

  void _close() => unawaited(Navigator.of(context).maybePop());

  @override
  Widget build(BuildContext context) {
    final s = _s;
    if (s == null) return const Scaffold(backgroundColor: Colors.black, body: LoadingView());
    return ListenableBuilder(
      listenable: s,
      builder: (context, _) => Env.mvp ? _mvpView(context, s) : _fullView(context, s),
    );
  }

  // ------------------------------------------------------------------ MVP
  Widget _mvpView(BuildContext context, LiveSession s) {
    final name = context.select<DevicesController, String?>((c) => c.byId(widget.deviceId)?.displayName);
    final connected = s.status == LiveStatus.connected;
    return Scaffold(
      backgroundColor: Colors.black,
      body: Stack(fit: StackFit.expand, children: [
        RTCVideoView(s.remoteRenderer, objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitContain),
        if (!connected || !s.hasVideo) _StatusOverlay(s: s, onRetry: _reconnect, translucent: connected),
        SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(8),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              _RoundBtn(icon: Icons.close_rounded, tooltip: S.close, onTap: _close),
              const SizedBox(width: 8),
              Expanded(child: _MvpStatusChip(s: s, name: name ?? widget.deviceId)),
              const SizedBox(width: 8),
              _RoundBtn(
                icon: Icons.refresh_rounded,
                tooltip: S.liveReconnect,
                onTap: _reconnect,
              ),
              const SizedBox(width: 8),
              _RoundBtn(
                icon: Icons.photo_camera_rounded,
                tooltip: S.snapshot,
                onTap: () {
                  if (!s.requestSnapshot()) showSnack(context, S.snapshotFailed, error: true);
                },
              ),
            ]),
          ),
        ),
      ]),
    );
  }

  // ------------------------------------------------------------- full app
  Widget _fullView(BuildContext context, LiveSession s) => Scaffold(
        backgroundColor: Colors.black,
        body: Stack(fit: StackFit.expand, children: [
          // ---- video ----
          RTCVideoView(s.remoteRenderer, objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitCover),
          if (!s.hasVideo || s.status != LiveStatus.connected)
            _StatusOverlay(s: s, onRetry: _reconnect, translucent: s.status == LiveStatus.connected),
          // ---- top bar ----
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(8),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                _RoundBtn(icon: Icons.close_rounded, onTap: _close),
                const SizedBox(width: 8),
                Expanded(child: _InfoBar(state: s.state, quality: s.linkQuality, relayed: s.relayed, rtt: s.rttMs)),
                const SizedBox(width: 8),
                Column(children: [
                  _RoundBtn(
                    icon: Icons.cameraswitch_rounded,
                    tooltip: S.switchCamera,
                    label: s.camera == 'front' ? S.camFront : S.camRear,
                    onTap: () => s.switchCamera(s.camera == 'front' ? 'rear' : 'front'),
                  ),
                  const SizedBox(height: 8),
                  _RoundBtn(
                    icon: s.speakerMuted ? Icons.volume_off_rounded : Icons.volume_up_rounded,
                    tooltip: s.speakerMuted ? S.unmute : S.mute,
                    onTap: () => s.setSpeakerMuted(!s.speakerMuted),
                  ),
                  const SizedBox(height: 8),
                  _RoundBtn(
                    icon: s.faceOn ? Icons.face_retouching_natural : Icons.face_rounded,
                    tooltip: S.showFace,
                    active: s.faceOn,
                    onTap: () async {
                      // Camera is requested only here, on explicit user action.
                      if (!s.faceOn && !(await Permission.camera.request()).isGranted) return;
                      await s.setFace(!s.faceOn);
                    },
                  ),
                ]),
              ]),
            ),
          ),
          // ---- own face preview ----
          if (s.faceOn)
            Positioned(
              right: 16,
              top: 200,
              child: ClipOval(
                child: SizedBox(
                  width: 96,
                  height: 96,
                  child: RTCVideoView(s.faceRenderer,
                      mirror: true, objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitCover),
                ),
              ),
            ),
          if (s.state?.safety.estop ?? false)
            const Align(alignment: Alignment(0, -0.45), child: _Banner(text: S.estopActive)),
          // ---- bottom controls ----
          Align(
            alignment: Alignment.bottomCenter,
            child: SafeArea(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
                child: Column(mainAxisSize: MainAxisSize.min, children: [
                  _ActionRow(s: s, send: _send),
                  const SizedBox(height: 12),
                  Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
                    Column(mainAxisSize: MainAxisSize.min, children: [
                      Joystick(
                        enabled: s.isOpen && !(s.state?.safety.estop ?? false),
                        onMove: (x, y) => _teleop?.input(x, y, speedScale: _speed),
                        onRelease: () => _teleop?.release(),
                      ),
                      SizedBox(
                        width: 168,
                        child: Slider(
                          value: _speed,
                          min: 0.2,
                          max: 1.0,
                          label: S.speed,
                          activeColor: Brand.yellow,
                          onChanged: (v) => setState(() => _speed = v),
                        ),
                      ),
                    ]),
                    const Spacer(),
                    _TalkControls(s: s),
                  ]),
                ]),
              ),
            ),
          ),
        ]),
      );
}

class _MvpStatusChip extends StatelessWidget {
  final LiveSession s;
  final String name;
  const _MvpStatusChip({required this.s, required this.name});

  @override
  Widget build(BuildContext context) {
    final (Color color, String text) = switch (s.status) {
      LiveStatus.connected => s.hasVideo ? (Brand.ok, S.liveConnected) : (Brand.yellow, S.liveWaitingVideo),
      LiveStatus.connecting || LiveStatus.idle => (Brand.yellow, S.liveConnecting),
      LiveStatus.failed || LiveStatus.closed => (Brand.danger, S.liveFailed),
    };
    final rtt = s.rttMs;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(color: Brand.scrim, borderRadius: BorderRadius.circular(18)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Container(width: 10, height: 10, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
        const SizedBox(width: 8),
        Flexible(
          child: Text(
            '$name · $text${rtt != null && s.status == LiveStatus.connected ? ' · ${rtt.round()} ms' : ''}',
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(color: Colors.white, fontSize: 13),
          ),
        ),
      ]),
    );
  }
}

class _StatusOverlay extends StatelessWidget {
  final LiveSession s;
  final VoidCallback onRetry;

  /// Connected but no frame yet: let the (black) video show through and keep
  /// the controls reachable.
  final bool translucent;
  const _StatusOverlay({required this.s, required this.onRetry, this.translucent = false});

  @override
  Widget build(BuildContext context) {
    final failed = s.status == LiveStatus.failed || s.status == LiveStatus.closed;
    return IgnorePointer(
      ignoring: translucent,
      child: ColoredBox(
        color: translucent ? Colors.transparent : Brand.ink,
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(32),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              if (!failed) const CircularProgressIndicator(color: Brand.yellow),
              if (failed) const Icon(Icons.videocam_off_rounded, color: Brand.yellow, size: 56),
              const SizedBox(height: 16),
              Text(
                failed
                    ? S.liveError(s.error)
                    : (s.status == LiveStatus.connected ? S.liveWaitingVideo : S.liveConnecting),
                style: const TextStyle(color: Colors.white, fontSize: 16),
                textAlign: TextAlign.center,
              ),
              if (failed) ...[
                const SizedBox(height: 16),
                FilledButton.icon(
                  onPressed: onRetry,
                  icon: const Icon(Icons.refresh_rounded),
                  label: const Text(S.liveReconnect),
                ),
              ],
            ]),
          ),
        ),
      ),
    );
  }
}

class _InfoBar extends StatelessWidget {
  final RobotState? state;
  final LinkQuality quality;
  final bool relayed;
  final double? rtt;
  const _InfoBar({required this.state, required this.quality, required this.relayed, required this.rtt});

  @override
  Widget build(BuildContext context) {
    final st = state;
    final b = st?.battery;
    final (qColor, qText, qBars) = switch (quality) {
      LinkQuality.good => (Brand.ok, S.qualityGood, 3),
      LinkQuality.fair => (Brand.yellow, S.qualityFair, 2),
      LinkQuality.poor => (Brand.danger, S.qualityPoor, 1),
      LinkQuality.unknown => (Colors.white54, S.qualityUnknown, 0),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(color: Brand.scrim, borderRadius: BorderRadius.circular(18)),
      child: DefaultTextStyle(
        style: const TextStyle(color: Colors.white, fontSize: 13),
        child: Wrap(spacing: 12, runSpacing: 4, crossAxisAlignment: WrapCrossAlignment.center, children: [
          if (b != null && b.known)
            Row(mainAxisSize: MainAxisSize.min, children: [
              Icon(
                b.charging
                    ? Icons.battery_charging_full_rounded
                    : b.soc > 60
                        ? Icons.battery_full_rounded
                        : b.soc > 25
                            ? Icons.battery_std_rounded
                            : Icons.battery_alert_rounded,
                color: b.soc <= 15 && !b.charging ? Brand.danger : Colors.white,
                size: 18,
              ),
              Text(S.battery(b.soc)),
              if (b.docked) ...[const SizedBox(width: 4), const Text('· ${S.docked}')],
            ]),
          if (st != null)
            Row(mainAxisSize: MainAxisSize.min, children: [
              const Icon(Icons.pets_rounded, size: 16, color: Brand.yellow),
              const SizedBox(width: 4),
              Text(S.activityLabel(st.activity)),
            ]),
          Tooltip(
            message: '${S.quality}: $qText${rtt != null ? ' · ${rtt!.round()} ms' : ''}${relayed ? ' · ${S.relayed}' : ''}',
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              for (var i = 1; i <= 3; i++)
                Container(
                  width: 4,
                  height: 5.0 + i * 4,
                  margin: const EdgeInsets.only(right: 2),
                  decoration: BoxDecoration(
                    color: i <= qBars ? qColor : Colors.white24,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              if (rtt != null) Text(' ${rtt!.round()} ms', style: const TextStyle(fontSize: 11)),
            ]),
          ),
          if (st != null && st.feeder.state != 'idle')
            Text('${S.feed}: ${S.feederStateLabel(st.feeder.state)}'),
        ]),
      ),
    );
  }
}

class _ActionRow extends StatelessWidget {
  final LiveSession s;
  final void Function(Map<String, dynamic>, [String?]) send;
  const _ActionRow({required this.s, required this.send});

  @override
  Widget build(BuildContext context) {
    final st = s.state;
    final estop = st?.safety.estop ?? false;
    final dispensing = st?.feeder.state == 'dispensing';
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: Row(children: [
        _Action(
          icon: estop ? Icons.lock_open_rounded : Icons.pan_tool_rounded,
          label: estop ? S.estopRelease : S.estop,
          color: Brand.danger,
          onTap: () => send(Cmd.estop(!estop)),
        ),
        dispensing
            ? _Action(icon: Icons.stop_circle_rounded, label: S.feedAbort, onTap: () => send(Cmd.feedAbort()))
            : _Action(icon: Icons.rice_bowl_rounded, label: S.feed, onTap: () => _feedSheet(context)),
        _Action(
          icon: Icons.sports_baseball_rounded,
          label: S.launchBall,
          badge: st == null ? null : '${st.launcher.balls}',
          onTap: () => send(Cmd.launch(power: 40, count: 1)),
        ),
        _Action(icon: Icons.auto_awesome_rounded, label: S.teaser, onTap: () => _teaseSheet(context)),
        _Action(icon: Icons.lightbulb_rounded, label: S.led, onTap: () => _ledSheet(context)),
        _Action(icon: Icons.ev_station_rounded, label: S.goDock, onTap: () => send(Cmd.gotoDock())),
        _Action(icon: Icons.pets_rounded, label: S.goPet, onTap: () => send(Cmd.gotoPet())),
        if ((st?.pois ?? const []).isNotEmpty)
          _Action(icon: Icons.place_rounded, label: S.goPoi, onTap: () => _poiSheet(context, st!.pois)),
        _Action(
          icon: Icons.map_rounded,
          label: S.mapPoi,
          onTap: () => showModalBottomSheet<void>(
            context: context,
            showDragHandle: true,
            isScrollControlled: true,
            builder: (_) => SafeArea(
              child: SingleChildScrollView(child: MapPoiPanel(deviceId: s.deviceId, card: false)),
            ),
          ),
        ),
        _Action(icon: Icons.photo_camera_rounded, label: S.snapshot, onTap: () => send(Cmd.snapshot())),
        _Action(
          icon: Icons.fiber_manual_record_rounded,
          label: S.recordClip,
          onTap: () => send(Cmd.recordClip(15), S.clipRecording(15)),
        ),
      ]),
    );
  }

  Future<void> _feedSheet(BuildContext context) async {
    final g = await showModalBottomSheet<int>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => _GramsSheet(hopper: s.state?.feeder.hopperG),
    );
    if (g != null) send(Cmd.feed(g), S.feedGrams(g));
  }

  Future<void> _teaseSheet(BuildContext context) async {
    final p = await _choice(context, Cmd.teasePatterns, S.teaseLabel);
    if (p != null) send(Cmd.tease(p, durationS: 20));
  }

  Future<void> _ledSheet(BuildContext context) async {
    final m = await _choice(context, Cmd.ledModes, S.ledModeLabel);
    if (m != null) send(Cmd.led(m, '#F0A830'));
  }

  Future<void> _poiSheet(BuildContext context, List<String> pois) async {
    final p = await _choice(context, pois, (x) => x);
    if (p != null) send(Cmd.gotoPoi(p));
  }

  Future<String?> _choice(BuildContext context, List<String> items, String Function(String) label) =>
      showModalBottomSheet<String>(
        context: context,
        showDragHandle: true,
        builder: (ctx) => SafeArea(
          child: Wrap(spacing: 8, runSpacing: 8, children: [
            for (final i in items)
              Padding(
                padding: const EdgeInsets.all(4),
                child: ActionChip(label: Text(label(i)), onPressed: () => Navigator.pop(ctx, i)),
              ),
          ]),
        ),
      );
}

class _GramsSheet extends StatefulWidget {
  final double? hopper;
  const _GramsSheet({this.hopper});
  @override
  State<_GramsSheet> createState() => _GramsSheetState();
}

class _GramsSheetState extends State<_GramsSheet> {
  double _g = 15;
  @override
  Widget build(BuildContext context) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 0, 24, 16),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text(S.feedGrams(_g.round()), style: Theme.of(context).textTheme.titleLarge),
            if (widget.hopper != null) Text(S.hopper(widget.hopper!)),
            Slider(value: _g, min: 1, max: 60, divisions: 59, onChanged: (v) => setState(() => _g = v)),
            Wrap(spacing: 8, children: [
              for (final q in const [5, 10, 20, 30])
                ChoiceChip(label: Text('$q g'), selected: _g.round() == q, onSelected: (_) => setState(() => _g = q.toDouble())),
            ]),
            const SizedBox(height: 12),
            FilledButton(onPressed: () => Navigator.pop(context, _g.round()), child: const Text(S.quickFeed)),
          ]),
        ),
      );
}

class _TalkControls extends StatelessWidget {
  final LiveSession s;
  const _TalkControls({required this.s});

  /// The mic is never requested when Live opens; ask here, on explicit
  /// action. It is attached to the offer on the next (re)connection.
  Future<void> _askMic(BuildContext context) async {
    final st = await Permission.microphone.request();
    if (!context.mounted) return;
    if (st.isGranted) {
      showSnack(context, S.micGrantedReconnect);
    } else if (st.isPermanentlyDenied) {
      showSnack(context, S.permMic, error: true);
      await openAppSettings();
    } else {
      showSnack(context, S.permMic, error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (!s.micAvailable) {
      return GestureDetector(
        onTap: () => unawaited(_askMic(context)),
        child: Container(
          width: 84,
          height: 84,
          decoration: const BoxDecoration(color: Colors.white54, shape: BoxShape.circle),
          child: const Icon(Icons.mic_off_rounded, size: 40, color: Brand.blue),
        ),
      );
    }
    final duplex = s.talkMode == TalkMode.duplex;
    return Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.end, children: [
      Container(
        decoration: BoxDecoration(color: Brand.scrim, borderRadius: BorderRadius.circular(18)),
        padding: const EdgeInsets.only(left: 12),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          const Text(S.duplex, style: TextStyle(color: Colors.white, fontSize: 12)),
          Switch(
            value: duplex,
            activeTrackColor: Brand.yellow,
            onChanged: (v) => s.setTalkMode(v ? TalkMode.duplex : TalkMode.pushToTalk),
          ),
        ]),
      ),
      const SizedBox(height: 12),
      GestureDetector(
        onTapDown: duplex ? null : (_) => s.setTalking(true),
        onTapUp: duplex ? null : (_) => s.setTalking(false),
        onTapCancel: duplex ? null : () => s.setTalking(false),
        onLongPressEnd: duplex ? null : (_) => s.setTalking(false),
        onTap: duplex ? () => s.setTalkMode(TalkMode.pushToTalk) : null,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 150),
          width: s.micLive ? 96 : 84,
          height: s.micLive ? 96 : 84,
          decoration: BoxDecoration(
            color: s.micLive ? Brand.yellow : Colors.white,
            shape: BoxShape.circle,
            boxShadow: s.micLive ? [const BoxShadow(color: Brand.yellow, blurRadius: 24)] : null,
          ),
          child: Icon(s.micLive ? Icons.mic_rounded : Icons.mic_none_rounded, size: 40, color: Brand.blue),
        ),
      ),
      const SizedBox(height: 6),
      Text(
        duplex ? S.duplex : (s.talking ? S.talking : S.pushToTalk),
        style: const TextStyle(color: Colors.white, fontSize: 12),
      ),
    ]);
  }
}

class _Action extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final Color? color;
  final String? badge;
  const _Action({required this.icon, required this.label, required this.onTap, this.color, this.badge});

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(right: 8),
        child: Material(
          color: Brand.scrim,
          borderRadius: BorderRadius.circular(18),
          child: InkWell(
            borderRadius: BorderRadius.circular(18),
            onTap: onTap,
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              child: Column(mainAxisSize: MainAxisSize.min, children: [
                Badge(
                  isLabelVisible: badge != null,
                  label: Text(badge ?? ''),
                  backgroundColor: Brand.yellow,
                  textColor: Brand.ink,
                  child: Icon(icon, color: color ?? Colors.white),
                ),
                const SizedBox(height: 4),
                Text(label, style: const TextStyle(color: Colors.white, fontSize: 11)),
              ]),
            ),
          ),
        ),
      );
}

class _RoundBtn extends StatelessWidget {
  final IconData icon;
  final VoidCallback onTap;
  final String? tooltip;
  final String? label;
  final bool active;
  const _RoundBtn({required this.icon, required this.onTap, this.tooltip, this.label, this.active = false});

  @override
  Widget build(BuildContext context) {
    final btn = Material(
      color: active ? Brand.yellow : Brand.scrim,
      shape: const CircleBorder(),
      child: InkWell(
        customBorder: const CircleBorder(),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(10),
          child: Icon(icon, color: active ? Brand.ink : Colors.white),
        ),
      ),
    );
    return Tooltip(
      message: tooltip ?? '',
      child: label == null
          ? btn
          : Column(mainAxisSize: MainAxisSize.min, children: [
              btn,
              Text(label!, style: const TextStyle(color: Colors.white, fontSize: 10)),
            ]),
    );
  }
}

class _Banner extends StatelessWidget {
  final String text;
  const _Banner({required this.text});
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        decoration: BoxDecoration(color: Brand.danger, borderRadius: BorderRadius.circular(18)),
        child: Text(text, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w700)),
      );
}
