// Robot events — INTERFACES.md §4.4. Same shape on the DataChannel
// (`{"t":"event", ...}`), in ZMQ `event` (`{"t": <type>, "ts": float, ...}`)
// and in the cloud event list.
import 'json_utils.dart';

/// Known event types (§4.4).
abstract final class EventTypes {
  static const mealDispensed = 'meal_dispensed';
  static const mealFailed = 'meal_failed';
  static const feederJam = 'feeder_jam';
  static const hopperLow = 'hopper_low';
  static const petDetected = 'pet_detected';
  static const playStarted = 'play_started';
  static const playEnded = 'play_ended';
  static const batteryLow = 'battery_low';
  static const docked = 'docked';
  static const undocked = 'undocked';
  static const stuck = 'stuck';
  static const cliff = 'cliff';
  static const estop = 'estop';
  static const navFailed = 'nav_failed';

  static const all = [
    mealDispensed, mealFailed, feederJam, hopperLow, petDetected, playStarted, playEnded,
    batteryLow, docked, undocked, stuck, cliff, estop, navFailed,
  ];
}

class RobotEvent {
  /// Event type, e.g. `meal_dispensed`.
  final String type;
  final double ts;

  /// Type-specific payload fields (grams, reason, camera, cls, game, …).
  final Json data;

  const RobotEvent({required this.type, required this.ts, this.data = const {}});

  DateTime? get time => tsToDate(ts);

  /// Parses an event object.
  ///
  /// The spec places the event type in `t` on ZMQ (`{"t": str, "ts": ...}`)
  /// but on the DataChannel the envelope is `{"t":"event", ...}` — so on the
  /// DataChannel the type cannot also live in `t`. We accept, in order:
  /// `event`, `type`, then `t` when it is not the literal "event".
  /// (Reported as a spec ambiguity.)
  factory RobotEvent.fromJson(Json j) {
    // DataChannel envelope (INTERFACES.md v0.2 §4.4): {"t":"event","event":{"t":<type>,...}}
    if (j['t'] == 'event' && j['event'] is Map) {
      return RobotEvent.fromJson(asMap(j['event']));
    }
    String type = asString(j['event']);
    if (type.isEmpty) type = asString(j['type']);
    if (type.isEmpty) {
      final t = asString(j['t']);
      if (t != 'event') type = t;
    }
    final data = Map<String, dynamic>.of(j)
      ..remove('t')
      ..remove('ts')
      ..remove('event')
      ..remove('type');
    // Cloud may wrap fields; flatten a nested "data" map.
    if (data['data'] is Map) {
      final nested = asMap(data.remove('data'));
      data.addAll(nested);
    }
    return RobotEvent(type: type.isEmpty ? 'unknown' : type, ts: asTs(j['ts']), data: data);
  }

  /// Canonical ZMQ/cloud form (`t` = event type).
  Json toJson() => {'t': type, 'ts': ts, ...data};

  num? get grams => data['grams'] is num ? data['grams'] as num : null;
  int? get soc => asIntOrNull(data['soc']);
  String? get reason => asStringOrNull(data['reason']);
  String? get camera => asStringOrNull(data['camera']);
  String? get cls => asStringOrNull(data['cls']);
  String? get game => asStringOrNull(data['game']);
  String? get target => asStringOrNull(data['target']);
  num? get durationS => data['duration_s'] is num ? data['duration_s'] as num : null;
  num? get activeS => data['active_s'] is num ? data['active_s'] as num : null;
}
