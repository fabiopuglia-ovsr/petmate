import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../app_router.dart' show deviceLocation, liveLocation;
import '../../core/auth/auth_state.dart';
import '../../core/config/server_config.dart';
import '../../core/env.dart';
import '../../core/models/device.dart';
import '../../core/signaling/signaling_client.dart';
import '../../core/widgets/common.dart';
import '../../core/widgets/theme.dart';
import '../../l10n/strings.dart';
import 'devices_controller.dart';

class DevicesScreen extends StatelessWidget {
  const DevicesScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final c = context.watch<DevicesController>();
    final sigStatus = context.select<SignalingClient, SignalingStatus>((s) => s.status);
    Widget body;
    if (!c.loadedOnce && c.loading) {
      body = const LoadingView();
    } else if (!c.loadedOnce && c.error != null) {
      body = ErrorView(error: c.error, onRetry: c.refresh);
    } else if (c.devices.isEmpty) {
      body = RefreshIndicator(
        onRefresh: c.refresh,
        child: ListView(physics: const AlwaysScrollableScrollPhysics(), children: [
          SizedBox(
            height: MediaQuery.of(context).size.height * 0.6,
            child: EmptyView(
              icon: Icons.smart_toy_outlined,
              text: S.noRobots,
              action: FilledButton.icon(
                onPressed: () => context.go('/devices/add'),
                icon: const Icon(Icons.add),
                label: const Text(S.addRobot),
              ),
            ),
          ),
        ]),
      );
    } else {
      body = RefreshIndicator(
        onRefresh: c.refresh,
        child: ListView.builder(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.only(top: 8, bottom: 96),
          itemCount: c.devices.length,
          itemBuilder: (_, i) => _DeviceCard(device: c.devices[i]),
        ),
      );
    }
    return Scaffold(
      appBar: AppBar(
        title: const Text(S.myRobots),
        actions: [
          if (sigStatus != SignalingStatus.connected)
            IconButton(
              tooltip: S.serverDisconnected,
              icon: const Icon(Icons.cloud_off_rounded, color: Brand.danger, size: 20),
              onPressed: () => showSnack(context, S.serverDisconnected),
            ),
          PopupMenuButton<String>(
            onSelected: (v) {
              if (v == 'logout') unawaited(context.read<AuthState>().signOut());
            },
            itemBuilder: (_) => [
              PopupMenuItem(enabled: false, child: Text(context.read<AuthState>().email ?? '')),
              PopupMenuItem(
                enabled: false,
                child: Text(context.read<ServerConfig>().base, style: const TextStyle(fontSize: 12)),
              ),
              const PopupMenuItem(value: 'logout', child: Text(S.logout)),
            ],
          ),
        ],
      ),
      body: body,
      floatingActionButton: c.devices.isEmpty
          ? null
          : FloatingActionButton.extended(
              onPressed: () => context.go('/devices/add'),
              icon: const Icon(Icons.add),
              label: const Text(S.addRobot),
            ),
    );
  }
}

class _DeviceCard extends StatelessWidget {
  final DeviceSummary device;
  const _DeviceCard({required this.device});

  Future<void> _rename(BuildContext context) async {
    final name = await promptText(context, title: S.rename, initial: device.name, hint: S.robotName);
    if (name == null || !context.mounted) return;
    try {
      await context.read<DevicesController>().rename(device.deviceId, name);
    } catch (e) {
      if (context.mounted) showSnack(context, errorText(e), error: true);
    }
  }

  Future<void> _remove(BuildContext context) async {
    final ok = await confirm(context, S.removeConfirm(device.displayName), ok: S.delete, danger: true);
    if (!ok || !context.mounted) return;
    try {
      await context.read<DevicesController>().remove(device.deviceId);
    } catch (e) {
      if (context.mounted) showSnack(context, errorText(e), error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final online = device.online;
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(20),
        onTap: () {
          if (Env.mvp && !online) {
            showSnack(context, S.robotOfflineTap);
            return;
          }
          unawaited(context.push(deviceLocation(device.deviceId)));
        },
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 14, 4, 14),
          child: Row(children: [
            Stack(children: [
              const CircleAvatar(
                radius: 28,
                backgroundColor: Brand.blue,
                child: Icon(Icons.pets_rounded, color: Brand.yellow, size: 30),
              ),
              Positioned(
                right: 0,
                bottom: 0,
                child: Container(
                  width: 16,
                  height: 16,
                  decoration: BoxDecoration(
                    color: online ? Brand.ok : Colors.grey,
                    shape: BoxShape.circle,
                    border: Border.all(color: Colors.white, width: 2),
                  ),
                ),
              ),
            ]),
            const SizedBox(width: 16),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(device.displayName,
                    style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                const SizedBox(height: 2),
                Text(
                  online ? S.online : S.lastSeen(relativeTime(device.lastSeen)),
                  style: TextStyle(color: online ? Brand.ok : Colors.black54),
                ),
                if (device.fw != null)
                  Text(S.firmware(device.fw!), style: const TextStyle(fontSize: 12, color: Colors.black45)),
              ]),
            ),
            if (online)
              IconButton.filledTonal(
                tooltip: S.live,
                icon: const Icon(Icons.videocam_rounded),
                onPressed: () => context.push(liveLocation(device.deviceId)),
              ),
            PopupMenuButton<String>(
              onSelected: (v) => v == 'rename' ? _rename(context) : _remove(context),
              itemBuilder: (_) => const [
                PopupMenuItem(value: 'rename', child: Text(S.rename)),
                PopupMenuItem(value: 'remove', child: Text(S.removeRobot)),
              ],
            ),
          ]),
        ),
      ),
    );
  }
}
