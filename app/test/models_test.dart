import 'dart:convert';

import 'package:petmate/core/models/commands.dart';
import 'package:petmate/core/models/config.dart';
import 'package:petmate/core/models/device.dart';
import 'package:petmate/core/models/robot_event.dart';
import 'package:petmate/core/models/robot_state.dart';
import 'package:flutter_test/flutter_test.dart';

/// Normalizes JSON for comparison: numbers compared numerically (25 == 25.0).
Object? norm(Object? v) => switch (v) {
      Map() => {for (final e in v.entries) e.key.toString(): norm(e.value)},
      List() => v.map(norm).toList(),
      num() => v.toDouble(),
      _ => v,
    };

// INTERFACES.md §5 example, verbatim.
const configJson = '''
{
  "version": 1,
  "pet": {"name": "Micio", "species": "cat", "weight_kg": 4.5},
  "feeding": {
    "schedule": [{"time": "08:00", "grams": 25, "days": [1,2,3,4,5,6,7]}],
    "portion_max_g": 60, "daily_max_g": 120, "feeding_poi": "cucina"
  },
  "play": {
    "auto_play": true,
    "sessions": [{"time": "14:00", "duration_min": 10, "days": [1,2,3,4,5,6,7]}],
    "launch_power": 40, "games": ["chase_ball", "teaser", "screen_birds"],
    "quiet_hours": {"start": "22:00", "end": "07:00"}
  },
  "patrol": {"enabled": false, "interval_min": 120},
  "notifications": {"meals": true, "activity": true, "battery": true, "pet_detected": false},
  "privacy": {"stream_led": true, "rear_camera_streaming": true},
  "audio": {"speaker_volume": 70},
  "timezone": "Europe/Rome"
}''';

// INTERFACES.md §4.3 example, verbatim.
const stateJson = '''
{"t":"state","ts":1760000000.0,
 "battery":{"soc":82,"voltage":15.6,"current":-1.2,"charging":false,"docked":true},
 "pose":{"x":1.2,"y":-0.4,"th":0.3,"localized":true},
 "mode":"auto","activity":"idle",
 "feeder":{"state":"idle","hopper_g":210.0,"today_g":50.0,"last_meal":{"ts":1760000000.0,"grams":25.0}},
 "launcher":{"state":"idle","balls":3},
 "safety":{"estop":false,"cliff_mask":0,"bump_mask":0,"wheel_drop":false,"motor_fault":false},
 "nav":{"status":"idle","goal":null,"mapping":false},
 "pet":{"last_seen_ts":1760000000.0,"camera":"front","bearing_rad":0.1},
 "pois":["cucina","salotto"],
 "fw":"0.1.0"}''';

void main() {
  group('DeviceConfig §5', () {
    test('round-trip of spec example is lossless', () {
      final src = jsonDecode(configJson) as Map<String, dynamic>;
      final cfg = DeviceConfig.fromJson(src);
      expect(cfg.pet.name, 'Micio');
      expect(cfg.feeding.schedule.single.grams, 25);
      expect(cfg.play.quietHours.end, '07:00');
      expect(norm(cfg.toJson()), norm(src));
      // And through a string encode/decode cycle.
      expect(norm(DeviceConfig.fromJson(jsonDecode(jsonEncode(cfg.toJson())) as Map<String, dynamic>).toJson()),
          norm(src));
    });

    test('unknown top-level keys are preserved', () {
      final src = jsonDecode(configJson) as Map<String, dynamic>..['future_feature'] = {'x': 1};
      expect(DeviceConfig.fromJson(src).toJson()['future_feature'], {'x': 1});
    });

    test('spec example validates clean', () {
      expect(DeviceConfig.fromJson(jsonDecode(configJson) as Map<String, dynamic>).validate(), isEmpty);
    });

    test('daily max is checked per weekday', () {
      final cfg = DeviceConfig.fromJson(jsonDecode(configJson) as Map<String, dynamic>);
      final over = cfg.copyWith(
        feeding: cfg.feeding.copyWith(schedule: [
          const MealSlot(time: '08:00', grams: 60, days: [1, 2]),
          const MealSlot(time: '20:00', grams: 60, days: [1]),
          const MealSlot(time: '13:00', grams: 10, days: [1]),
        ]),
      );
      expect(over.feeding.gramsForDay(1), 130);
      expect(over.feeding.gramsForDay(2), 60);
      expect(over.validate(), contains('daily_max_exceeded'));
    });

    test('invalid values flagged', () {
      final cfg = DeviceConfig.fromJson(jsonDecode(configJson) as Map<String, dynamic>);
      final bad = cfg.copyWith(
        feeding: cfg.feeding.copyWith(schedule: [const MealSlot(time: '25:00', grams: 0, days: [])]),
        play: cfg.play.copyWith(launchPower: 90),
      );
      expect(bad.validate(), containsAll(['meal_time', 'meal_grams', 'meal_days', 'launch_power_range']));
    });

    test('clearing feeding_poi serializes null', () {
      final cfg = DeviceConfig.fromJson(jsonDecode(configJson) as Map<String, dynamic>);
      final c2 = cfg.copyWith(feeding: cfg.feeding.copyWith(clearFeedingPoi: true));
      expect((c2.toJson()['feeding'] as Map)['feeding_poi'], isNull);
    });
  });

  group('RobotState §4.3', () {
    test('round-trip of spec example', () {
      final src = jsonDecode(stateJson) as Map<String, dynamic>;
      final s = RobotState.fromJson(src);
      expect(s.battery.soc, 82);
      expect(s.battery.docked, isTrue);
      expect(s.feeder.lastMeal!.grams, 25.0);
      expect(s.pois, ['cucina', 'salotto']);
      expect(s.safety.anyAlarm, isFalse);
      expect(norm(s.toJson()), norm(src));
    });

    test('tolerates missing optional sections', () {
      final s = RobotState.fromJson({'t': 'state', 'ts': 1, 'battery': {'soc': 10}});
      expect(s.battery.soc, 10);
      expect(s.feeder.lastMeal, isNull);
      expect(s.pois, isEmpty);
    });
  });

  group('RobotEvent §4.4', () {
    test('ZMQ/cloud form: t = event type', () {
      final e = RobotEvent.fromJson({'t': 'meal_dispensed', 'ts': 1760000000.5, 'grams': 25});
      expect(e.type, EventTypes.mealDispensed);
      expect(e.grams, 25);
      expect(e.toJson(), {'t': 'meal_dispensed', 'ts': 1760000000.5, 'grams': 25});
      expect(RobotEvent.fromJson(e.toJson()).toJson(), e.toJson());
    });

    test('DataChannel envelope {"t":"event","event":...}', () {
      final e = RobotEvent.fromJson({'t': 'event', 'event': 'play_ended', 'ts': 5, 'game': 'teaser', 'duration_s': 300, 'active_s': 120});
      expect(e.type, 'play_ended');
      expect(e.game, 'teaser');
      expect(e.activeS, 120);
      expect(e.data.containsKey('t'), isFalse);
    });

    test('DataChannel envelope {"t":"event","type":...} and nested data', () {
      final e = RobotEvent.fromJson({'t': 'event', 'type': 'battery_low', 'ts': 5, 'data': {'soc': 14}});
      expect(e.type, 'battery_low');
      expect(e.soc, 14);
    });

    test('every spec event type is recognized', () {
      for (final t in EventTypes.all) {
        expect(RobotEvent.fromJson({'t': t, 'ts': 0}).type, t);
      }
    });
  });

  group('Cloud / BLE models', () {
    test('DeviceSummary round-trip', () {
      final src = {'device_id': 'PM-0001', 'name': 'Petmate cucina', 'online': true, 'last_seen': '2026-10-01T10:00:00.000Z', 'fw': '0.1.0'};
      final d = DeviceSummary.fromJson(src);
      expect(d.online, isTrue);
      expect(d.lastSeen!.toUtc(), DateTime.utc(2026, 10, 1, 10));
      expect(d.toJson(), src);
    });

    test('DeviceSummary accepts epoch-seconds last_seen', () {
      final d = DeviceSummary.fromJson({'device_id': 'x', 'name': '', 'online': false, 'last_seen': 1760000000});
      expect(d.lastSeen!.toUtc(), DateTime.fromMillisecondsSinceEpoch(1760000000000, isUtc: true));
      expect(d.displayName, 'x');
    });

    test('Clip round-trip', () {
      final src = {'clip_id': 'c1', 'ts': 1760000000.0, 'duration_s': 15.0, 'thumb_url': '/v1/clips/c1.jpg', 'url': '/v1/clips/c1'};
      expect(RobotClip.fromJson(src).toJson(), src);
    });

    test('IceServer accepts string or list urls, omits null creds', () {
      expect(IceServer.fromJson({'urls': 'stun:x'}).toJson(), {'urls': ['stun:x']});
      final t = {'urls': ['turn:a?transport=udp'], 'username': 'u', 'credential': 'p'};
      expect(IceServer.fromJson(t).toJson(), t);
    });

    test('BLE device_info / wifi_scan / status', () {
      final info = {'device_id': 'PM-000123', 'model': 'PM-EVT1', 'fw': '0.1.0', 'pubkey': 'AAAA'};
      expect(BleDeviceInfo.fromJson(info).toJson(), info);
      final w = {'ssid': 'Casa', 'rssi': -52, 'secure': true};
      expect(WifiNetwork.fromJson(w).toJson(), w);
      expect(WifiNetwork.fromJson(w).bars, 4);
      final st = {'state': 'wifi_ok', 'error': null, 'ip': '192.168.1.40'};
      expect(ProvisioningStatus.fromJson(st).toJson(), st);
      expect(ProvisioningStatus.fromJson({'state': 'done'}).isTerminal, isTrue);
    });
  });

  group('Commands §4.2', () {
    test('shapes and clamping', () {
      expect(Cmd.feed(100), {'t': 'feed', 'grams': 60});
      expect(Cmd.feed(0), {'t': 'feed', 'grams': 1});
      expect(Cmd.launch(power: 99, count: 9), {'t': 'launch', 'power': 70, 'count': 4});
      expect(Cmd.teleop(2, -9), {'t': 'teleop', 'v': 0.5, 'w': -2.0});
      expect(Cmd.gotoPoi('cucina'), {'t': 'goto', 'target': 'poi', 'poi': 'cucina'});
      expect(Cmd.led('solid', '#F0A830'), {'t': 'led', 'mode': 'solid', 'color': '#F0A830'});
      expect(Cmd.tease('wiggle'), {'t': 'tease', 'pattern': 'wiggle', 'duration_s': 20});
      expect(Cmd.display('owner'), {'t': 'display', 'mode': 'owner'});
      expect(Cmd.volume(150), {'t': 'volume', 'speaker': 100});
      expect(Cmd.configSet({'version': 1}), {'t': 'config_set', 'config': {'version': 1}});
    });
  });
}
