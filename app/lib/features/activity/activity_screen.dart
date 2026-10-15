import 'dart:async';

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';
import 'package:video_player/video_player.dart';

import '../../core/api/api_client.dart';
import '../../core/models/device.dart';
import '../../core/models/robot_event.dart';
import '../../core/webrtc/session_hub.dart';
import '../../core/widgets/common.dart';
import '../../core/widgets/theme.dart';
import '../../l10n/strings.dart';

class ActivityScreen extends StatelessWidget {
  final String deviceId;
  const ActivityScreen({super.key, required this.deviceId});

  @override
  Widget build(BuildContext context) => DefaultTabController(
        length: 2,
        child: Scaffold(
          appBar: AppBar(
            title: const Text(S.activity),
            bottom: const TabBar(tabs: [Tab(text: S.events), Tab(text: S.clips)]),
          ),
          body: TabBarView(children: [
            _EventsTab(deviceId: deviceId),
            _ClipsTab(deviceId: deviceId),
          ]),
        ),
      );
}

// ------------------------------------------------------------------ events
class _EventsTab extends StatefulWidget {
  final String deviceId;
  const _EventsTab({required this.deviceId});
  @override
  State<_EventsTab> createState() => _EventsTabState();
}

class _EventsTabState extends State<_EventsTab> with AutomaticKeepAliveClientMixin {
  List<RobotEvent>? _events;
  Object? _error;
  StreamSubscription<RobotEvent>? _liveSub;

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
    // Merge live events when a session is open.
    final s = context.read<SessionHub>().openSessionFor(widget.deviceId);
    _liveSub = s?.events.listen((e) => setState(() => _events = [e, ...?_events]));
  }

  @override
  void dispose() {
    _liveSub?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final ev = await context.read<ApiClient>().events(widget.deviceId, limit: 50);
      if (!mounted) return;
      ev.sort((a, b) => b.ts.compareTo(a.ts));
      setState(() {
        _events = ev;
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final ev = _events;
    if (ev == null) return _error != null ? ErrorView(error: _error, onRetry: _load) : const LoadingView();
    if (ev.isEmpty) {
      return RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: const [
          SizedBox(height: 120),
          EmptyView(icon: Icons.timeline_rounded, text: S.noEvents),
        ]),
      );
    }
    final items = <Widget>[];
    String? lastDay;
    for (final e in ev) {
      final day = _dayLabel(e.time);
      if (day != lastDay) {
        items.add(SectionTitle(day));
        lastDay = day;
      }
      items.add(_EventTile(e));
    }
    return RefreshIndicator(onRefresh: _load, child: ListView(padding: const EdgeInsets.only(bottom: 24), children: items));
  }

  static String _dayLabel(DateTime? t) {
    if (t == null) return '—';
    final now = DateTime.now();
    final d0 = DateTime(now.year, now.month, now.day);
    final d = DateTime(t.year, t.month, t.day);
    final diff = d0.difference(d).inDays;
    if (diff == 0) return S.today;
    if (diff == 1) return S.yesterday;
    return DateFormat('EEEE d MMMM', 'it').format(t);
  }
}

class _EventTile extends StatelessWidget {
  final RobotEvent e;
  const _EventTile(this.e);

  (IconData, Color) get _icon => switch (e.type) {
        EventTypes.mealDispensed => (Icons.rice_bowl_rounded, Brand.ok),
        EventTypes.mealFailed || EventTypes.feederJam => (Icons.no_meals_rounded, Brand.danger),
        EventTypes.hopperLow => (Icons.inventory_2_rounded, Brand.yellow),
        EventTypes.petDetected => (Icons.pets_rounded, Brand.blue),
        EventTypes.playStarted || EventTypes.playEnded => (Icons.sports_baseball_rounded, Brand.yellow),
        EventTypes.batteryLow => (Icons.battery_alert_rounded, Brand.danger),
        EventTypes.docked || EventTypes.undocked => (Icons.ev_station_rounded, Brand.blue),
        EventTypes.stuck || EventTypes.cliff || EventTypes.estop || EventTypes.navFailed =>
          (Icons.warning_amber_rounded, Brand.danger),
        _ => (Icons.info_outline_rounded, Colors.black45),
      };

  String? get _detail {
    final parts = <String>[];
    if (e.grams != null) parts.add('${e.grams!.round()} g');
    if (e.soc != null) parts.add('${e.soc}%');
    if (e.game != null) parts.add(S.gameLabel(e.game!));
    if (e.durationS != null) {
      final m = (e.durationS! / 60).round();
      parts.add(S.playDuration(m, e.activeS == null ? null : (e.activeS! / 60).round()));
    }
    if (e.cls != null) parts.add(e.cls == 'cat' ? S.cat : (e.cls == 'dog' ? S.dog : e.cls!));
    if (e.camera != null) parts.add(e.camera == 'front' ? S.camFront : S.camRear);
    if (e.target != null) parts.add(e.target!);
    if (e.reason != null) parts.add(e.reason!);
    return parts.isEmpty ? null : parts.join(' · ');
  }

  @override
  Widget build(BuildContext context) {
    final (icon, color) = _icon;
    final t = e.time;
    return Card(
      child: ListTile(
        leading: CircleAvatar(backgroundColor: Brand.offWhite, child: Icon(icon, color: color)),
        title: Text(S.eventTitle(e.type), style: const TextStyle(fontWeight: FontWeight.w600)),
        subtitle: _detail == null ? null : Text(_detail!),
        trailing: Text(t == null ? '' : DateFormat.Hm('it').format(t)),
      ),
    );
  }
}

// ------------------------------------------------------------------ clips
class _ClipsTab extends StatefulWidget {
  final String deviceId;
  const _ClipsTab({required this.deviceId});
  @override
  State<_ClipsTab> createState() => _ClipsTabState();
}

class _ClipsTabState extends State<_ClipsTab> with AutomaticKeepAliveClientMixin {
  List<RobotClip>? _clips;
  Object? _error;

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final c = await context.read<ApiClient>().clips(widget.deviceId);
      if (!mounted) return;
      c.sort((a, b) => b.ts.compareTo(a.ts));
      setState(() {
        _clips = c;
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final clips = _clips;
    if (clips == null) return _error != null ? ErrorView(error: _error, onRetry: _load) : const LoadingView();
    if (clips.isEmpty) {
      return RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: const [
          SizedBox(height: 120),
          EmptyView(icon: Icons.video_library_rounded, text: S.noClips),
        ]),
      );
    }
    final api = context.read<ApiClient>();
    return RefreshIndicator(
      onRefresh: _load,
      child: GridView.builder(
        padding: const EdgeInsets.all(12),
        gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
          maxCrossAxisExtent: 220,
          mainAxisSpacing: 10,
          crossAxisSpacing: 10,
          childAspectRatio: 16 / 12,
        ),
        itemCount: clips.length,
        itemBuilder: (_, i) {
          final c = clips[i];
          final thumb = api.thumbUri(c);
          return InkWell(
            borderRadius: BorderRadius.circular(16),
            onTap: () => Navigator.of(context).push(MaterialPageRoute<void>(
              fullscreenDialog: true,
              builder: (_) => ClipPlayerScreen(uri: api.clipUri(c), headers: api.authHeaders, clip: c),
            )),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(16),
              child: Stack(fit: StackFit.expand, children: [
                ColoredBox(
                  color: Brand.blue,
                  child: thumb == null
                      ? const Icon(Icons.movie_rounded, color: Brand.yellow, size: 40)
                      : Image.network(thumb.toString(),
                          headers: api.authHeaders,
                          fit: BoxFit.cover,
                          errorBuilder: (_, _, _) =>
                              const Icon(Icons.movie_rounded, color: Brand.yellow, size: 40)),
                ),
                const Center(child: Icon(Icons.play_circle_fill_rounded, color: Colors.white70, size: 44)),
                Positioned(
                  left: 0,
                  right: 0,
                  bottom: 0,
                  child: Container(
                    color: Brand.scrim,
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                    child: Text(
                      '${relativeTime(c.time)} · ${c.durationS.round()} s',
                      style: const TextStyle(color: Colors.white, fontSize: 12),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ),
              ]),
            ),
          );
        },
      ),
    );
  }
}

class ClipPlayerScreen extends StatefulWidget {
  final Uri uri;
  final Map<String, String> headers;
  final RobotClip clip;
  const ClipPlayerScreen({super.key, required this.uri, required this.headers, required this.clip});

  @override
  State<ClipPlayerScreen> createState() => _ClipPlayerScreenState();
}

class _ClipPlayerScreenState extends State<ClipPlayerScreen> {
  late final VideoPlayerController _vc =
      VideoPlayerController.networkUrl(widget.uri, httpHeaders: widget.headers);
  Object? _error;

  @override
  void initState() {
    super.initState();
    _vc.initialize().then((_) {
      if (!mounted) return;
      setState(() {});
      unawaited(_vc.play());
    }).catchError((Object e) {
      if (mounted) setState(() => _error = e);
    });
    _vc.addListener(_tick);
  }

  void _tick() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _vc.removeListener(_tick);
    unawaited(_vc.dispose());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: Colors.black,
        appBar: AppBar(
          backgroundColor: Colors.black,
          foregroundColor: Colors.white,
          title: Text(relativeTime(widget.clip.time), style: const TextStyle(color: Colors.white, fontSize: 16)),
        ),
        body: Center(
          child: _error != null
              ? const Text(S.genericError, style: TextStyle(color: Colors.white))
              : !_vc.value.isInitialized
                  ? const CircularProgressIndicator(color: Brand.yellow)
                  : GestureDetector(
                      onTap: () => _vc.value.isPlaying ? _vc.pause() : _vc.play(),
                      child: Column(mainAxisSize: MainAxisSize.min, children: [
                        AspectRatio(aspectRatio: _vc.value.aspectRatio, child: VideoPlayer(_vc)),
                        VideoProgressIndicator(_vc,
                            allowScrubbing: true,
                            colors: const VideoProgressColors(playedColor: Brand.yellow)),
                        IconButton(
                          iconSize: 48,
                          color: Colors.white,
                          icon: Icon(_vc.value.isPlaying ? Icons.pause_rounded : Icons.play_arrow_rounded),
                          onPressed: () => _vc.value.isPlaying ? _vc.pause() : _vc.play(),
                        ),
                      ]),
                    ),
        ),
      );
}
