import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/config/config_controller.dart';
import '../../core/models/config.dart';
import '../../core/webrtc/session_hub.dart';
import '../../core/widgets/common.dart';
import '../../core/widgets/theme.dart';
import '../../l10n/strings.dart';

class ScheduleScreen extends StatelessWidget {
  final String deviceId;
  const ScheduleScreen({super.key, required this.deviceId});

  @override
  Widget build(BuildContext context) => ChangeNotifierProvider(
        create: (ctx) => ConfigController(deviceId, ctx.read<SessionHub>())..load(),
        child: const _ScheduleView(),
      );
}

/// Shared save bar for config editors.
class ConfigSaveBar extends StatelessWidget {
  const ConfigSaveBar({super.key});

  @override
  Widget build(BuildContext context) {
    final c = context.watch<ConfigController>();
    if (!c.dirty) return const SizedBox.shrink();
    final problems = c.problems;
    return SafeArea(
      child: Container(
        margin: const EdgeInsets.all(12),
        padding: const EdgeInsets.fromLTRB(16, 10, 10, 10),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(20),
          boxShadow: const [BoxShadow(color: Color(0x22000000), blurRadius: 12)],
        ),
        child: Row(children: [
          Expanded(
            child: problems.isEmpty
                ? const Text(S.unsaved)
                : Text(problems.map(S.problem).join('\n'), style: const TextStyle(color: Brand.danger)),
          ),
          TextButton(onPressed: c.discard, child: const Text(S.cancel)),
          FilledButton(
            onPressed: problems.isNotEmpty || c.saving
                ? null
                : () async {
                    final ok = await c.save();
                    if (!context.mounted) return;
                    if (ok) {
                      showSnack(context, '${S.saved} · ${S.sentVia(c.lastRoute == SendRoute.dataChannel)}');
                    } else {
                      showSnack(context, errorText(c.error), error: true);
                    }
                  },
            child: c.saving
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text(S.save),
          ),
        ]),
      ),
    );
  }
}

class _ScheduleView extends StatelessWidget {
  const _ScheduleView();

  @override
  Widget build(BuildContext context) {
    final c = context.watch<ConfigController>();
    final cfg = c.config;
    return Scaffold(
      appBar: AppBar(title: const Text(S.schedule)),
      bottomNavigationBar: const ConfigSaveBar(),
      body: cfg == null
          ? (c.loading ? const LoadingView() : ErrorView(error: c.error, onRetry: c.load))
          : ListView(padding: const EdgeInsets.only(bottom: 24), children: [
              _MealsSection(cfg: cfg.feeding, edit: (f) => c.edit((x) => x.copyWith(feeding: f))),
              _PlaySection(cfg: cfg.play, edit: (p) => c.edit((x) => x.copyWith(play: p))),
              const SectionTitle(S.patrol),
              Card(
                child: Column(children: [
                  SwitchListTile(
                    title: const Text(S.patrolEnabled),
                    value: cfg.patrol.enabled,
                    onChanged: (v) => c.edit((x) => x.copyWith(patrol: x.patrol.copyWith(enabled: v))),
                  ),
                  if (cfg.patrol.enabled)
                    ListTile(
                      title: Text(S.patrolEvery(cfg.patrol.intervalMin)),
                      subtitle: Slider(
                        value: cfg.patrol.intervalMin.toDouble().clamp(15.0, 480.0),
                        min: 15,
                        max: 480,
                        divisions: 31,
                        onChanged: (v) =>
                            c.edit((x) => x.copyWith(patrol: x.patrol.copyWith(intervalMin: v.round()))),
                      ),
                    ),
                ]),
              ),
            ]),
    );
  }
}

class _MealsSection extends StatelessWidget {
  final FeedingConfig cfg;
  final void Function(FeedingConfig) edit;
  const _MealsSection({required this.cfg, required this.edit});

  void _setMeal(int i, MealSlot m) => edit(cfg.copyWith(schedule: [...cfg.schedule]..[i] = m));

  @override
  Widget build(BuildContext context) {
    final sorted = [...cfg.schedule.asMap().entries]..sort((a, b) => a.value.time.compareTo(b.value.time));
    final perDay = cfg.maxScheduledPerDay;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      SectionTitle(S.meals,
          trailing: TextButton.icon(
            icon: const Icon(Icons.add),
            label: const Text(S.addMeal),
            onPressed: () => edit(cfg.copyWith(schedule: [
              ...cfg.schedule,
              MealSlot(time: '12:00', grams: cfg.portionMaxG.clamp(1, 20)),
            ])),
          )),
      for (final e in sorted)
        Card(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 8, 12),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                ActionChip(
                  avatar: const Icon(Icons.access_time_rounded, size: 18),
                  label: Text(e.value.time, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700)),
                  onPressed: () async {
                    final t = await pickHhMm(context, e.value.time);
                    if (t != null) _setMeal(e.key, e.value.copyWith(time: t));
                  },
                ),
                const SizedBox(width: 12),
                Text('${e.value.grams} g', style: const TextStyle(fontSize: 18, color: Brand.blue)),
                const Spacer(),
                IconButton(
                  icon: const Icon(Icons.delete_outline_rounded),
                  tooltip: S.delete,
                  onPressed: () => edit(cfg.copyWith(schedule: [...cfg.schedule]..removeAt(e.key))),
                ),
              ]),
              Slider(
                value: e.value.grams.toDouble().clamp(1.0, cfg.portionMaxG.toDouble().clamp(2.0, 60.0)),
                min: 1,
                max: cfg.portionMaxG.toDouble().clamp(2.0, 60.0),
                divisions: (cfg.portionMaxG - 1).clamp(1, 59),
                label: '${e.value.grams} g',
                onChanged: (v) => _setMeal(e.key, e.value.copyWith(grams: v.round())),
              ),
              DaysPicker(days: e.value.days, onChanged: (d) => _setMeal(e.key, e.value.copyWith(days: d))),
            ]),
          ),
        ),
      Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(S.perDayTotal(perDay, cfg.dailyMaxG),
                style: TextStyle(color: perDay > cfg.dailyMaxG ? Brand.danger : null)),
            const SizedBox(height: 8),
            _NumberRow(
              label: S.dailyMax,
              value: cfg.dailyMaxG,
              min: 10,
              max: 400,
              step: 5,
              unit: 'g',
              onChanged: (v) => edit(cfg.copyWith(dailyMaxG: v)),
            ),
            _NumberRow(
              label: S.portionMax,
              value: cfg.portionMaxG,
              min: 1,
              max: 60,
              step: 1,
              unit: 'g',
              onChanged: (v) => edit(cfg.copyWith(portionMaxG: v)),
            ),
            _PoiField(
              value: cfg.feedingPoi,
              onChanged: (v) => edit(v == null ? cfg.copyWith(clearFeedingPoi: true) : cfg.copyWith(feedingPoi: v)),
            ),
          ]),
        ),
      ),
    ]);
  }
}

class _PoiField extends StatelessWidget {
  final String? value;
  final ValueChanged<String?> onChanged;
  const _PoiField({required this.value, required this.onChanged});

  @override
  Widget build(BuildContext context) {
    // POI list is only known from robot state (§4.3 `pois`) during a live session.
    final pois = context.select<SessionHub, List<String>>((h) => h.session?.state?.pois ?? const []);
    final options = {...pois, if (value != null) value!}.toList()..sort();
    return Row(children: [
      const Expanded(child: Text(S.feedingPoi)),
      DropdownButton<String?>(
        value: value,
        items: [
          const DropdownMenuItem<String?>(value: null, child: Text(S.none)),
          for (final p in options) DropdownMenuItem<String?>(value: p, child: Text(p)),
        ],
        onChanged: onChanged,
      ),
    ]);
  }
}

class _PlaySection extends StatelessWidget {
  final PlayConfig cfg;
  final void Function(PlayConfig) edit;
  const _PlaySection({required this.cfg, required this.edit});

  void _setSession(int i, PlaySession s) => edit(cfg.copyWith(sessions: [...cfg.sessions]..[i] = s));

  @override
  Widget build(BuildContext context) {
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      SectionTitle(S.playSessions,
          trailing: TextButton.icon(
            icon: const Icon(Icons.add),
            label: const Text(S.addSession),
            onPressed: () => edit(cfg.copyWith(
                sessions: [...cfg.sessions, const PlaySession(time: '17:00', durationMin: 10)])),
          )),
      Card(
        child: SwitchListTile(
          title: const Text(S.autoPlay),
          subtitle: const Text(S.autoPlayHint),
          value: cfg.autoPlay,
          onChanged: (v) => edit(cfg.copyWith(autoPlay: v)),
        ),
      ),
      for (var i = 0; i < cfg.sessions.length; i++)
        Card(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 8, 12),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                ActionChip(
                  avatar: const Icon(Icons.access_time_rounded, size: 18),
                  label: Text(cfg.sessions[i].time,
                      style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700)),
                  onPressed: () async {
                    final t = await pickHhMm(context, cfg.sessions[i].time);
                    if (t != null) _setSession(i, cfg.sessions[i].copyWith(time: t));
                  },
                ),
                const SizedBox(width: 12),
                Text('${cfg.sessions[i].durationMin} min', style: const TextStyle(fontSize: 18, color: Brand.blue)),
                const Spacer(),
                IconButton(
                  icon: const Icon(Icons.delete_outline_rounded),
                  tooltip: S.delete,
                  onPressed: () => edit(cfg.copyWith(sessions: [...cfg.sessions]..removeAt(i))),
                ),
              ]),
              Slider(
                value: cfg.sessions[i].durationMin.toDouble().clamp(1.0, 60.0),
                min: 1,
                max: 60,
                divisions: 59,
                label: '${cfg.sessions[i].durationMin} min',
                onChanged: (v) => _setSession(i, cfg.sessions[i].copyWith(durationMin: v.round())),
              ),
              DaysPicker(
                  days: cfg.sessions[i].days, onChanged: (d) => _setSession(i, cfg.sessions[i].copyWith(days: d))),
            ]),
          ),
        ),
      const SectionTitle(S.games),
      Card(
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Wrap(spacing: 8, runSpacing: 8, children: [
            for (final g in kGames)
              FilterChip(
                label: Text(S.gameLabel(g)),
                selected: cfg.games.contains(g),
                onSelected: (sel) =>
                    edit(cfg.copyWith(games: sel ? [...cfg.games, g] : cfg.games.where((x) => x != g).toList())),
              ),
          ]),
        ),
      ),
      Card(
        child: ListTile(
          title: Text('${S.launchPower}: ${cfg.launchPower}%'),
          subtitle: Slider(
            value: cfg.launchPower.toDouble().clamp(20.0, 70.0),
            min: 20,
            max: 70,
            divisions: 10,
            onChanged: (v) => edit(cfg.copyWith(launchPower: v.round())),
          ),
        ),
      ),
      const SectionTitle(S.quietHours),
      Card(
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Row(children: [
            const Icon(Icons.bedtime_rounded, color: Brand.blue),
            const SizedBox(width: 12),
            const Text(S.from),
            const SizedBox(width: 8),
            ActionChip(
              label: Text(cfg.quietHours.start),
              onPressed: () async {
                final t = await pickHhMm(context, cfg.quietHours.start);
                if (t != null) edit(cfg.copyWith(quietHours: cfg.quietHours.copyWith(start: t)));
              },
            ),
            const SizedBox(width: 12),
            const Text(S.to),
            const SizedBox(width: 8),
            ActionChip(
              label: Text(cfg.quietHours.end),
              onPressed: () async {
                final t = await pickHhMm(context, cfg.quietHours.end);
                if (t != null) edit(cfg.copyWith(quietHours: cfg.quietHours.copyWith(end: t)));
              },
            ),
          ]),
        ),
      ),
    ]);
  }
}

class _NumberRow extends StatelessWidget {
  final String label;
  final int value, min, max, step;
  final String unit;
  final ValueChanged<int> onChanged;
  const _NumberRow({
    required this.label,
    required this.value,
    required this.min,
    required this.max,
    required this.step,
    required this.unit,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) => Row(children: [
        Expanded(child: Text(label)),
        IconButton(
          icon: const Icon(Icons.remove_circle_outline),
          onPressed: value - step >= min ? () => onChanged(value - step) : null,
        ),
        SizedBox(width: 64, child: Text('$value $unit', textAlign: TextAlign.center)),
        IconButton(
          icon: const Icon(Icons.add_circle_outline),
          onPressed: value + step <= max ? () => onChanged(value + step) : null,
        ),
      ]);
}

