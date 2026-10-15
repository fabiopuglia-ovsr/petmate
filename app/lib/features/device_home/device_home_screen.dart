import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../core/models/commands.dart';
import '../../core/webrtc/session_hub.dart';
import '../../core/widgets/common.dart';
import '../../core/widgets/theme.dart';
import '../../l10n/strings.dart';
import '../devices/devices_controller.dart';

/// Hub for one robot: live, schedule, activity, settings + quick actions
/// (quick actions go over REST /commands when no live session is open).
class DeviceHomeScreen extends StatelessWidget {
  final String deviceId;
  const DeviceHomeScreen({super.key, required this.deviceId});

  String get _base => '/d/${Uri.encodeComponent(deviceId)}';

  Future<void> _cmd(BuildContext context, Map<String, dynamic> cmd) async {
    try {
      final r = await context.read<SessionHub>().sendCommand(deviceId, cmd);
      if (context.mounted) showSnack(context, S.sentVia(r == SendRoute.dataChannel));
    } catch (e) {
      if (context.mounted) showSnack(context, errorText(e), error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final d = context.watch<DevicesController>().byId(deviceId);
    final online = d?.online ?? false;
    return Scaffold(
      appBar: AppBar(title: Text(d?.displayName ?? deviceId)),
      body: ListView(padding: const EdgeInsets.only(bottom: 24), children: [
        Card(
          color: Brand.blue,
          child: InkWell(
            borderRadius: BorderRadius.circular(20),
            onTap: online ? () => context.push('$_base/live') : null,
            child: SizedBox(
              height: 180,
              child: Center(
                child: Column(mainAxisSize: MainAxisSize.min, children: [
                  Icon(online ? Icons.videocam_rounded : Icons.videocam_off_rounded,
                      size: 56, color: Brand.yellow),
                  const SizedBox(height: 8),
                  Text(online ? S.live : S.deviceOffline,
                      style: const TextStyle(color: Colors.white, fontSize: 20, fontWeight: FontWeight.w700)),
                ]),
              ),
            ),
          ),
        ),
        const SectionTitle(S.quickActions),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Wrap(spacing: 10, runSpacing: 10, children: [
            _Quick(
              icon: Icons.rice_bowl_rounded,
              label: S.quickFeed,
              enabled: online,
              onTap: () async {
                final g = await _askGrams(context);
                if (g != null && context.mounted) await _cmd(context, Cmd.feed(g));
              },
            ),
            _Quick(
              icon: Icons.sports_baseball_rounded,
              label: S.quickPlay,
              enabled: online,
              onTap: () => _cmd(context, Cmd.play('chase_ball')),
            ),
            _Quick(
              icon: Icons.ev_station_rounded,
              label: S.quickDock,
              enabled: online,
              onTap: () => _cmd(context, Cmd.gotoDock()),
            ),
          ]),
        ),
        const SizedBox(height: 8),
        _NavTile(icon: Icons.schedule_rounded, title: S.schedule, onTap: () => context.push('$_base/schedule')),
        _NavTile(icon: Icons.timeline_rounded, title: S.activity, onTap: () => context.push('$_base/activity')),
        _NavTile(icon: Icons.tune_rounded, title: S.settings, onTap: () => context.push('$_base/settings')),
      ]),
    );
  }
}

Future<int?> _askGrams(BuildContext context) => showModalBottomSheet<int>(
      context: context,
      showDragHandle: true,
      builder: (ctx) {
        var g = 20.0;
        return StatefulBuilder(
          builder: (ctx, set) => Padding(
            padding: const EdgeInsets.fromLTRB(24, 0, 24, 24),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              Text(S.feedGrams(g.round()), style: Theme.of(ctx).textTheme.titleLarge),
              Slider(value: g, min: 1, max: 60, divisions: 59, onChanged: (v) => set(() => g = v)),
              FilledButton(onPressed: () => Navigator.pop(ctx, g.round()), child: const Text(S.quickFeed)),
            ]),
          ),
        );
      },
    );

class _Quick extends StatelessWidget {
  final IconData icon;
  final String label;
  final bool enabled;
  final VoidCallback onTap;
  const _Quick({required this.icon, required this.label, required this.enabled, required this.onTap});

  @override
  Widget build(BuildContext context) => ActionChip(
        avatar: Icon(icon, color: Brand.blue),
        label: Text(label),
        onPressed: enabled ? onTap : null,
        backgroundColor: Colors.white,
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 10),
      );
}

class _NavTile extends StatelessWidget {
  final IconData icon;
  final String title;
  final VoidCallback onTap;
  const _NavTile({required this.icon, required this.title, required this.onTap});

  @override
  Widget build(BuildContext context) => Card(
        child: ListTile(
          leading: CircleAvatar(backgroundColor: Brand.yellowSoft, child: Icon(icon, color: Brand.blue)),
          title: Text(title, style: const TextStyle(fontWeight: FontWeight.w600)),
          trailing: const Icon(Icons.chevron_right_rounded),
          onTap: onTap,
        ),
      );
}
