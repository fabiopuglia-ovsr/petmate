import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../app_router.dart' show liveLocation;
import '../../core/api/api_client.dart';
import '../../core/config/server_config.dart';
import '../../core/models/device.dart';
import '../../core/provisioning/prov_errors.dart';
import '../../core/signaling/signaling_client.dart';
import '../../core/widgets/theme.dart';
import '../../l10n/strings.dart';
import '../devices/devices_controller.dart';
import 'onboarding_controller.dart';

class OnboardingScreen extends StatelessWidget {
  const OnboardingScreen({super.key});

  @override
  Widget build(BuildContext context) => ChangeNotifierProvider(
        create: (ctx) {
          final server = ctx.read<ServerConfig>();
          final signaling = ctx.read<SignalingClient>();
          return OnboardingController(
            api: ctx.read<ApiClient>(),
            cloudUrl: () => server.base,
            presence: signaling.isOnline,
          );
        },
        child: const _OnboardingView(),
      );
}

class _OnboardingView extends StatefulWidget {
  const _OnboardingView();

  @override
  State<_OnboardingView> createState() => _OnboardingViewState();
}

class _OnboardingViewState extends State<_OnboardingView> {
  late final OnboardingController _c;
  bool _navigated = false;

  @override
  void initState() {
    super.initState();
    _c = context.read<OnboardingController>();
    _c.addListener(_onChange);
  }

  @override
  void dispose() {
    _c.removeListener(_onChange);
    super.dispose();
  }

  /// Robot online → refresh the list and open the live view (once).
  void _onChange() {
    final id = _c.deviceId;
    if (_c.step != OnbStep.online || _navigated || id == null) return;
    _navigated = true;
    unawaited(context.read<DevicesController>().refresh());
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) context.go(liveLocation(id));
    });
  }

  @override
  Widget build(BuildContext context) {
    final c = context.watch<OnboardingController>();
    final Widget body = switch (c.step) {
      OnbStep.intro => _Intro(onStart: c.startScan),
      OnbStep.preparing => const _Busy(text: S.onbPreparing),
      OnbStep.scanning => _ScanList(c: c),
      OnbStep.connecting => const _Busy(text: S.onbConnecting),
      OnbStep.wifi => _WifiForm(c: c),
      OnbStep.sending => _Progress(status: c.status?.state),
      OnbStep.waitingOnline => const _Busy(text: S.onbWaitingOnline),
      OnbStep.online => const _Busy(text: S.onbOnline),
      OnbStep.onlineTimeout => _OnlineTimeout(c: c),
      OnbStep.failed => _Failed(c: c),
    };
    return Scaffold(
      appBar: AppBar(title: const Text(S.onbTitle)),
      body: SafeArea(
        child: AnimatedSwitcher(
          duration: const Duration(milliseconds: 250),
          child: KeyedSubtree(key: ValueKey(c.step), child: body),
        ),
      ),
    );
  }
}

class _Intro extends StatelessWidget {
  final VoidCallback onStart;
  const _Intro({required this.onStart});

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.all(24),
        child: Column(children: [
          const Spacer(),
          Container(
            padding: const EdgeInsets.all(32),
            decoration: const BoxDecoration(color: Brand.yellowSoft, shape: BoxShape.circle),
            child: const Icon(Icons.bluetooth_searching_rounded, size: 72, color: Brand.blue),
          ),
          const SizedBox(height: 24),
          Text(S.onbIntro, textAlign: TextAlign.center, style: Theme.of(context).textTheme.bodyLarge),
          const Spacer(),
          SizedBox(width: double.infinity, child: FilledButton(onPressed: onStart, child: const Text(S.onbStartScan))),
        ]),
      );
}

class _ScanList extends StatelessWidget {
  final OnboardingController c;
  const _ScanList({required this.c});

  @override
  Widget build(BuildContext context) => Column(children: [
        if (!c.scanDone) const LinearProgressIndicator(),
        Padding(
          padding: const EdgeInsets.all(16),
          child: Text(c.scanDone && c.found.isEmpty ? S.onbNoneFound : S.onbScanning, textAlign: TextAlign.center),
        ),
        Expanded(
          child: ListView(children: [
            for (final r in c.found)
              Card(
                child: ListTile(
                  leading: const CircleAvatar(
                      backgroundColor: Brand.blue, child: Icon(Icons.pets_rounded, color: Brand.yellow)),
                  title: Text(r.name, style: const TextStyle(fontWeight: FontWeight.w600)),
                  subtitle: Text('${r.rssi} dBm'),
                  trailing: const Icon(Icons.chevron_right_rounded),
                  onTap: () => unawaited(c.select(r)),
                ),
              ),
          ]),
        ),
        if (c.scanDone)
          Padding(
            padding: const EdgeInsets.all(16),
            child: OutlinedButton.icon(
                onPressed: () => unawaited(c.startScan()),
                icon: const Icon(Icons.refresh),
                label: const Text(S.onbScanAgain)),
          ),
      ]);
}

class _Busy extends StatelessWidget {
  final String text;
  const _Busy({required this.text});
  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const CircularProgressIndicator(),
            const SizedBox(height: 16),
            Text(text, textAlign: TextAlign.center),
          ]),
        ),
      );
}

class _WifiForm extends StatefulWidget {
  final OnboardingController c;
  const _WifiForm({required this.c});
  @override
  State<_WifiForm> createState() => _WifiFormState();
}

class _WifiFormState extends State<_WifiForm> {
  WifiNetwork? _selected;
  bool _manual = false;
  final _ssid = TextEditingController();
  final _pw = TextEditingController();
  bool _obscure = true;

  @override
  void dispose() {
    _ssid.dispose();
    _pw.dispose();
    super.dispose();
  }

  bool get _needsPassword => _manual || (_selected?.secure ?? true);
  String get _ssidValue => _manual ? _ssid.text.trim() : (_selected?.ssid ?? '');

  /// SSID 1–32 bytes; WPA passphrase 8–63 chars. A hidden network typed by
  /// hand may be open (empty password).
  bool get _valid {
    final ssid = _ssidValue;
    if (ssid.isEmpty || ssid.length > 32) return false;
    if (!_needsPassword) return true;
    final pw = _pw.text;
    if (_manual && pw.isEmpty) return true;
    return pw.length >= 8 && pw.length <= 63;
  }

  void _pick(WifiNetwork n) => setState(() {
        _selected = n;
        _manual = false;
      });

  @override
  Widget build(BuildContext context) {
    final c = widget.c;
    final info = c.info;
    return ListView(padding: const EdgeInsets.only(bottom: 24), children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(20, 20, 8, 6),
        child: Row(children: [
          Expanded(
            child: Text(S.onbChooseWifi,
                style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700, color: Brand.blue)),
          ),
          IconButton(
              tooltip: S.onbRescanWifi, icon: const Icon(Icons.refresh), onPressed: () => unawaited(c.rescanWifi())),
        ]),
      ),
      if (info != null)
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 20),
          child: Text('${info.deviceId} · ${info.model} · fw ${info.fw}', style: const TextStyle(color: Colors.black54)),
        ),
      if (c.networks.isEmpty)
        const Padding(padding: EdgeInsets.all(20), child: Text(S.onbNoNetworks)),
      for (final n in c.networks)
        Card(
          child: ListTile(
            selected: !_manual && _selected?.ssid == n.ssid,
            leading: Icon(!_manual && _selected?.ssid == n.ssid
                ? Icons.radio_button_checked_rounded
                : Icons.radio_button_unchecked_rounded),
            title: Text(n.ssid),
            trailing: Row(mainAxisSize: MainAxisSize.min, children: [
              if (n.secure) const Icon(Icons.lock_outline, size: 16),
              const SizedBox(width: 4),
              Icon(Icons.wifi_rounded, color: switch (n.bars) { 4 || 3 => Brand.ok, 2 => Brand.yellow, _ => Brand.danger }),
            ]),
            onTap: () => _pick(n),
          ),
        ),
      Card(
        child: ListTile(
          selected: _manual,
          leading: Icon(_manual ? Icons.radio_button_checked_rounded : Icons.radio_button_unchecked_rounded),
          title: const Text(S.onbOtherNetwork),
          subtitle: const Text(S.onbHiddenHint),
          onTap: () => setState(() => _manual = true),
        ),
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          if (_manual) ...[
            TextField(
              controller: _ssid,
              autocorrect: false,
              enableSuggestions: false,
              decoration: const InputDecoration(labelText: S.onbSsid),
              onChanged: (_) => setState(() {}),
            ),
            const SizedBox(height: 12),
          ],
          if (_needsPassword)
            TextField(
              controller: _pw,
              obscureText: _obscure,
              autocorrect: false,
              enableSuggestions: false,
              keyboardType: TextInputType.visiblePassword,
              decoration: InputDecoration(
                labelText: S.onbWifiPassword,
                helperText: _manual ? S.onbOpenNetwork : S.onbPasswordRule,
                suffixIcon: IconButton(
                  tooltip: _obscure ? S.showPassword : S.hidePassword,
                  icon: Icon(_obscure ? Icons.visibility : Icons.visibility_off),
                  onPressed: () => setState(() => _obscure = !_obscure),
                ),
              ),
              onChanged: (_) => setState(() {}),
            ),
          const SizedBox(height: 8),
          const Text(S.onbBands, style: TextStyle(fontSize: 12, color: Colors.black54)),
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _valid
                ? () => unawaited(c.sendCredentials(ssid: _ssidValue, password: _needsPassword ? _pw.text : ''))
                : null,
            child: const Text(S.onbSend),
          ),
        ]),
      ),
    ]);
  }
}

class _Progress extends StatelessWidget {
  final String? status;
  const _Progress({required this.status});

  static const _order = ['receiving', 'connecting', 'wifi_ok', 'registering', 'done'];

  @override
  Widget build(BuildContext context) {
    final idx = _order.indexOf(status ?? 'receiving');
    final cur = idx < 0 ? 0 : idx;
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
        for (var i = 0; i < _order.length - 1; i++)
          ListTile(
            leading: i < cur
                ? const Icon(Icons.check_circle_rounded, color: Brand.ok)
                : i == cur
                    ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2.5))
                    : const Icon(Icons.radio_button_unchecked, color: Colors.black26),
            title: Text(S.provState(_order[i]),
                style: TextStyle(fontWeight: i == cur ? FontWeight.w700 : FontWeight.normal)),
          ),
        const SizedBox(height: 16),
        const Text(S.onbKeepClose, textAlign: TextAlign.center, style: TextStyle(color: Colors.black54)),
      ]),
    );
  }
}

class _OnlineTimeout extends StatelessWidget {
  final OnboardingController c;
  const _OnlineTimeout({required this.c});

  @override
  Widget build(BuildContext context) => ListView(padding: const EdgeInsets.all(24), children: [
        const Icon(Icons.cloud_off_rounded, size: 72, color: Brand.yellow),
        const SizedBox(height: 16),
        Text(S.onbOnlineTimeoutTitle,
            textAlign: TextAlign.center, style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 12),
        Text(S.onbOnlineTimeoutBody(context.read<ServerConfig>().base)),
        const SizedBox(height: 24),
        FilledButton(onPressed: c.retryOnlineWait, child: const Text(S.onbCheckAgain)),
        const SizedBox(height: 8),
        OutlinedButton(onPressed: () => context.go('/devices'), child: const Text(S.myRobots)),
      ]);
}

class _Failed extends StatelessWidget {
  final OnboardingController c;
  const _Failed({required this.c});

  @override
  Widget build(BuildContext context) {
    final info = c.errorInfo;
    final msg = info?.message ?? S.genericError;
    final label = switch (info?.retry) {
      ProvRetry.wifi => S.onbRetryWifi,
      ProvRetry.settings => S.openSettings,
      _ => S.retry,
    };
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(mainAxisAlignment: MainAxisAlignment.center, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const Icon(Icons.error_outline_rounded, size: 72, color: Brand.danger),
        const SizedBox(height: 16),
        Text(msg, textAlign: TextAlign.center, style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 24),
        FilledButton(onPressed: () => unawaited(c.retry()), child: Text(label)),
        const SizedBox(height: 8),
        TextButton(onPressed: () => unawaited(c.startScan()), child: const Text(S.onbStartOver)),
      ]),
    );
  }
}
