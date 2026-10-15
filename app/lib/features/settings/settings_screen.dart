import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../core/config/config_controller.dart';
import '../../core/models/commands.dart';
import '../../core/webrtc/live_session.dart';
import '../../core/webrtc/session_hub.dart';
import '../../core/widgets/common.dart';
import '../../core/widgets/theme.dart';
import '../../l10n/strings.dart';
import '../schedule/schedule_screen.dart' show ConfigSaveBar;

class SettingsScreen extends StatelessWidget {
  final String deviceId;
  const SettingsScreen({super.key, required this.deviceId});

  @override
  Widget build(BuildContext context) => ChangeNotifierProvider(
        create: (ctx) => ConfigController(deviceId, ctx.read<SessionHub>())..load(),
        child: _SettingsView(deviceId: deviceId),
      );
}

class _SettingsView extends StatelessWidget {
  final String deviceId;
  const _SettingsView({required this.deviceId});

  @override
  Widget build(BuildContext context) {
    final c = context.watch<ConfigController>();
    final cfg = c.config;
    if (cfg == null) {
      return Scaffold(
        appBar: AppBar(title: const Text(S.settings)),
        body: c.loading ? const LoadingView() : ErrorView(error: c.error, onRetry: c.load),
      );
    }
    return Scaffold(
      appBar: AppBar(title: const Text(S.settings)),
      bottomNavigationBar: const ConfigSaveBar(),
      body: ListView(padding: const EdgeInsets.only(bottom: 24), children: [
        // ---- pet profile ----
        const SectionTitle(S.petProfile),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(children: [
              _TextSetting(
                label: S.petName,
                value: cfg.pet.name,
                onChanged: (v) => c.edit((x) => x.copyWith(pet: x.pet.copyWith(name: v))),
              ),
              const SizedBox(height: 12),
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'cat', label: Text(S.cat), icon: Icon(Icons.pets_rounded)),
                  ButtonSegment(value: 'dog', label: Text(S.dog), icon: Icon(Icons.cruelty_free)),
                ],
                selected: {cfg.pet.species == 'dog' ? 'dog' : 'cat'},
                onSelectionChanged: (s) => c.edit((x) => x.copyWith(pet: x.pet.copyWith(species: s.first))),
              ),
              const SizedBox(height: 8),
              Row(children: [
                const Expanded(child: Text(S.weightKg)),
                Text(cfg.pet.weightKg.toStringAsFixed(1)),
              ]),
              Slider(
                value: cfg.pet.weightKg.clamp(0.5, 60.0),
                min: 0.5,
                max: 60,
                divisions: 119,
                onChanged: (v) => c.edit((x) => x.copyWith(pet: x.pet.copyWith(weightKg: (v * 2).round() / 2))),
              ),
            ]),
          ),
        ),

        // ---- notifications ----
        const SectionTitle(S.notifications),
        Card(
          child: Column(children: [
            SwitchListTile(
              title: const Text(S.notifMeals),
              value: cfg.notifications.meals,
              onChanged: (v) => c.edit((x) => x.copyWith(notifications: x.notifications.copyWith(meals: v))),
            ),
            SwitchListTile(
              title: const Text(S.notifActivity),
              value: cfg.notifications.activity,
              onChanged: (v) => c.edit((x) => x.copyWith(notifications: x.notifications.copyWith(activity: v))),
            ),
            SwitchListTile(
              title: const Text(S.notifBattery),
              value: cfg.notifications.battery,
              onChanged: (v) => c.edit((x) => x.copyWith(notifications: x.notifications.copyWith(battery: v))),
            ),
            SwitchListTile(
              title: const Text(S.notifPet),
              value: cfg.notifications.petDetected,
              onChanged: (v) => c.edit((x) => x.copyWith(notifications: x.notifications.copyWith(petDetected: v))),
            ),
          ]),
        ),

        // ---- privacy ----
        const SectionTitle(S.privacy),
        Card(
          child: Column(children: [
            SwitchListTile(
              title: const Text(S.streamLed),
              value: cfg.privacy.streamLed,
              onChanged: (v) => c.edit((x) => x.copyWith(privacy: x.privacy.copyWith(streamLed: v))),
            ),
            SwitchListTile(
              title: const Text(S.rearCamera),
              value: cfg.privacy.rearCameraStreaming,
              onChanged: (v) => c.edit((x) => x.copyWith(privacy: x.privacy.copyWith(rearCameraStreaming: v))),
            ),
          ]),
        ),

        // ---- audio ----
        const SectionTitle(S.audio),
        Card(
          child: ListTile(
            leading: const Icon(Icons.volume_up_rounded, color: Brand.blue),
            title: Text('${S.volume}: ${cfg.audio.speakerVolume}%'),
            subtitle: Slider(
              value: cfg.audio.speakerVolume.toDouble().clamp(0.0, 100.0),
              min: 0,
              max: 100,
              divisions: 20,
              onChanged: (v) => c.edit((x) => x.copyWith(audio: x.audio.copyWith(speakerVolume: v.round()))),
              onChangeEnd: (v) {
                // Apply immediately for feedback when live; persisted on Save.
                context.read<SessionHub>().openSessionFor(deviceId)?.send(Cmd.volume(v.round()));
              },
            ),
          ),
        ),

        // ---- map & POI ----
        const SectionTitle(S.mapPoi),
        MapPoiPanel(deviceId: deviceId),

        const SectionTitle(S.timezone),
        Card(child: ListTile(leading: const Icon(Icons.public_rounded), title: Text(cfg.timezone))),
      ]),
    );
  }
}

class _TextSetting extends StatefulWidget {
  final String label;
  final String value;
  final ValueChanged<String> onChanged;
  const _TextSetting({required this.label, required this.value, required this.onChanged});
  @override
  State<_TextSetting> createState() => _TextSettingState();
}

class _TextSettingState extends State<_TextSetting> {
  late final _c = TextEditingController(text: widget.value);

  @override
  void didUpdateWidget(_TextSetting old) {
    super.didUpdateWidget(old);
    if (widget.value != _c.text) _c.text = widget.value;
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => TextField(
        controller: _c,
        decoration: InputDecoration(labelText: widget.label),
        onChanged: widget.onChanged,
      );
}

/// Mapping and POIs need the robot's live state (`nav.mapping`, `pois`), so
/// the controls require an open live session; otherwise they offer to open one.
/// Also shown as a bottom sheet from the live screen (where you drive the
/// robot around while mapping).
class MapPoiPanel extends StatelessWidget {
  final String deviceId;
  final bool card;
  const MapPoiPanel({super.key, required this.deviceId, this.card = true});

  @override
  Widget build(BuildContext context) {
    final hub = context.watch<SessionHub>();
    final s = hub.openSessionFor(deviceId);
    final content = Padding(
      padding: const EdgeInsets.all(16),
      child: s == null ? _offline(context) : _live(context, s),
    );
    return card ? Card(child: content) : content;
  }

  Widget _offline(BuildContext context) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Text(S.mapHint),
        const SizedBox(height: 12),
        FilledButton.icon(
          icon: const Icon(Icons.videocam_rounded),
          label: const Text(S.live),
          onPressed: () => context.push('/d/${Uri.encodeComponent(deviceId)}/live'),
        ),
      ]);

  Widget _live(BuildContext context, LiveSession s) {
    final mapping = s.state?.nav.mapping ?? false;
    final pois = s.state?.pois ?? const <String>[];
    void send(Map<String, dynamic> cmd) {
      if (!s.send(cmd)) showSnack(context, S.requiresLive, error: true);
    }

    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (mapping)
        const Padding(
          padding: EdgeInsets.only(bottom: 8),
          child: Row(children: [
            Icon(Icons.radar_rounded, color: Brand.yellow),
            SizedBox(width: 8),
            Text(S.mappingActive, style: TextStyle(fontWeight: FontWeight.w700)),
          ]),
        ),
      const Text(S.mapHint, style: TextStyle(color: Colors.black54)),
      const SizedBox(height: 12),
      Wrap(spacing: 8, runSpacing: 8, children: [
        if (!mapping)
          FilledButton.icon(
            icon: const Icon(Icons.play_arrow_rounded),
            label: const Text(S.startMapping),
            onPressed: () => send(Cmd.map('start_mapping')),
          ),
        if (mapping) ...[
          FilledButton.icon(
            icon: const Icon(Icons.save_rounded),
            label: const Text(S.saveMap),
            onPressed: () => send(Cmd.map('save')),
          ),
          OutlinedButton.icon(
            icon: const Icon(Icons.stop_rounded),
            label: const Text(S.stopMapping),
            onPressed: () => send(Cmd.map('stop_mapping')),
          ),
        ],
        OutlinedButton.icon(
          icon: const Icon(Icons.delete_sweep_rounded),
          label: const Text(S.clearMap),
          style: OutlinedButton.styleFrom(foregroundColor: Brand.danger),
          onPressed: () async {
            if (await confirm(context, S.clearMapConfirm, ok: S.delete, danger: true)) send(Cmd.map('clear'));
          },
        ),
      ]),
      const Divider(height: 32),
      Row(children: [
        const Expanded(child: Text(S.pois, style: TextStyle(fontWeight: FontWeight.w700))),
        TextButton.icon(
          icon: const Icon(Icons.add_location_alt_rounded),
          label: const Text(S.savePoiHere),
          onPressed: () async {
            final name = await promptText(context, title: S.savePoiHere, hint: S.poiName);
            if (name != null) send(Cmd.savePoi(name.toLowerCase()));
          },
        ),
      ]),
      if (pois.isEmpty) const Text(S.noPois, style: TextStyle(color: Colors.black54)),
      for (final p in pois)
        ListTile(
          contentPadding: EdgeInsets.zero,
          leading: const Icon(Icons.place_rounded, color: Brand.blue),
          title: Text(p),
          trailing: TextButton(onPressed: () => send(Cmd.gotoPoi(p)), child: const Text(S.goThere)),
        ),
    ]);
  }
}
