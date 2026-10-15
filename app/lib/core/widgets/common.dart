import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../../l10n/strings.dart';
import '../api/api_client.dart';
import 'theme.dart';

void showSnack(BuildContext context, String msg, {bool error = false}) {
  ScaffoldMessenger.of(context)
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(
      content: Text(msg),
      backgroundColor: error ? Brand.danger : null,
    ));
}

String errorText(Object? e) {
  if (e is ApiException) {
    if (e.isNetwork) return S.networkError;
    if (e.isUnauthorized) return S.sessionExpired;
    if (e.isOffline) return S.deviceOffline;
    if (e.message == 'invalid_response') return S.invalidServerResponse;
    return e.message.isNotEmpty ? e.message : S.genericError;
  }
  if (e is StateError && e.message == 'requires_live_session') return S.requiresLive;
  return S.genericError;
}

class SectionTitle extends StatelessWidget {
  final String text;
  final Widget? trailing;
  const SectionTitle(this.text, {super.key, this.trailing});

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(20, 20, 16, 6),
        child: Row(children: [
          Expanded(
            child: Text(text,
                style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700, color: Brand.blue)),
          ),
          if (trailing != null) trailing!,
        ]),
      );
}

class LoadingView extends StatelessWidget {
  const LoadingView({super.key});
  @override
  Widget build(BuildContext context) => const Center(child: CircularProgressIndicator());
}

class ErrorView extends StatelessWidget {
  final Object? error;
  final VoidCallback onRetry;
  const ErrorView({super.key, required this.error, required this.onRetry});

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.cloud_off_rounded, size: 56, color: Brand.blue),
            const SizedBox(height: 12),
            Text(errorText(error), textAlign: TextAlign.center),
            const SizedBox(height: 16),
            FilledButton(onPressed: onRetry, child: const Text(S.retry)),
          ]),
        ),
      );
}

class EmptyView extends StatelessWidget {
  final IconData icon;
  final String text;
  final Widget? action;
  const EmptyView({super.key, required this.icon, required this.text, this.action});

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Container(
              padding: const EdgeInsets.all(24),
              decoration: const BoxDecoration(color: Brand.yellowSoft, shape: BoxShape.circle),
              child: Icon(icon, size: 48, color: Brand.blue),
            ),
            const SizedBox(height: 16),
            Text(text, textAlign: TextAlign.center, style: Theme.of(context).textTheme.bodyLarge),
            if (action != null) ...[const SizedBox(height: 20), action!],
          ]),
        ),
      );
}

/// Weekday selector, ISO days 1..7.
class DaysPicker extends StatelessWidget {
  final List<int> days;
  final ValueChanged<List<int>> onChanged;
  const DaysPicker({super.key, required this.days, required this.onChanged});

  @override
  Widget build(BuildContext context) => Wrap(
        spacing: 6,
        children: [
          for (var d = 1; d <= 7; d++)
            Tooltip(
              message: S.weekdaysLong[d - 1],
              child: FilterChip(
                showCheckmark: false,
                label: Text(S.weekdaysShort[d - 1]),
                selected: days.contains(d),
                onSelected: (sel) {
                  final next = {...days};
                  sel ? next.add(d) : next.remove(d);
                  onChanged(next.toList()..sort());
                },
              ),
            ),
        ],
      );
}

/// "HH:MM" ↔ TimeOfDay.
TimeOfDay parseHhMm(String s) {
  final p = s.split(':');
  return TimeOfDay(hour: int.tryParse(p.first) ?? 0, minute: p.length > 1 ? int.tryParse(p[1]) ?? 0 : 0);
}

String formatHhMm(TimeOfDay t) =>
    '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';

Future<String?> pickHhMm(BuildContext context, String current) async {
  final t = await showTimePicker(
    context: context,
    initialTime: parseHhMm(current),
    builder: (ctx, child) =>
        MediaQuery(data: MediaQuery.of(ctx).copyWith(alwaysUse24HourFormat: true), child: child!),
  );
  return t == null ? null : formatHhMm(t);
}

String relativeTime(DateTime? t) {
  if (t == null) return '—';
  final now = DateTime.now();
  final d = now.difference(t);
  if (d.inMinutes < 1) return S.justNow;
  if (d.inMinutes < 60) return S.minutesAgo(d.inMinutes);
  if (d.inHours < 24 && now.day == t.day) return S.todayAt(DateFormat.Hm('it').format(t));
  return DateFormat('d MMM, HH:mm', 'it').format(t);
}

Future<bool> confirm(BuildContext context, String message, {String ok = S.ok, bool danger = false}) async {
  final r = await showDialog<bool>(
    context: context,
    builder: (ctx) => AlertDialog(
      content: Text(message),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text(S.cancel)),
        FilledButton(
          style: danger ? FilledButton.styleFrom(backgroundColor: Brand.danger) : null,
          onPressed: () => Navigator.pop(ctx, true),
          child: Text(ok),
        ),
      ],
    ),
  );
  return r ?? false;
}

Future<String?> promptText(BuildContext context,
    {required String title, String initial = '', String? hint}) async {
  final c = TextEditingController(text: initial);
  final r = await showDialog<String>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(title),
      content: TextField(
        controller: c,
        autofocus: true,
        decoration: InputDecoration(hintText: hint),
        onSubmitted: (v) => Navigator.pop(ctx, v.trim()),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx), child: const Text(S.cancel)),
        FilledButton(onPressed: () => Navigator.pop(ctx, c.text.trim()), child: const Text(S.save)),
      ],
    ),
  );
  c.dispose();
  return (r == null || r.isEmpty) ? null : r;
}
