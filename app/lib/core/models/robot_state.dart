// Robot state — INTERFACES.md §4.3 (`{"t":"state", ...}` at 2 Hz).
import 'json_utils.dart';

class BatteryState {
  final int soc;
  final double voltage;
  final double current;
  final bool charging;
  final bool docked;

  /// False when the robot reports `soc: null` (no battery monitor, e.g. MVP on a bare Pi).
  final bool known;

  const BatteryState({
    required this.soc,
    required this.voltage,
    required this.current,
    required this.charging,
    required this.docked,
    this.known = true,
  });

  factory BatteryState.fromJson(Json j) => BatteryState(
        known: j['soc'] != null,
        soc: asInt(j['soc']),
        voltage: asDouble(j['voltage']),
        current: asDouble(j['current']),
        charging: asBool(j['charging']),
        docked: asBool(j['docked']),
      );

  Json toJson() =>
      {'soc': soc, 'voltage': voltage, 'current': current, 'charging': charging, 'docked': docked};
}

class Pose {
  final double x, y, th;
  final bool localized;

  const Pose({required this.x, required this.y, required this.th, required this.localized});

  factory Pose.fromJson(Json j) => Pose(
        x: asDouble(j['x']),
        y: asDouble(j['y']),
        th: asDouble(j['th']),
        localized: asBool(j['localized']),
      );

  Json toJson() => {'x': x, 'y': y, 'th': th, 'localized': localized};
}

class LastMeal {
  final double ts;
  final double grams;

  const LastMeal({required this.ts, required this.grams});

  factory LastMeal.fromJson(Json j) => LastMeal(ts: asDouble(j['ts']), grams: asDouble(j['grams']));

  Json toJson() => {'ts': ts, 'grams': grams};
}

class FeederState {
  final String state;
  final double? hopperG; // §9.11: null until the load cell is available
  final double todayG;
  final LastMeal? lastMeal;

  const FeederState({required this.state, this.hopperG, required this.todayG, this.lastMeal});

  factory FeederState.fromJson(Json j) => FeederState(
        state: asString(j['state'], 'idle'),
        hopperG: asDoubleOrNull(j['hopper_g']),
        todayG: asDouble(j['today_g']),
        lastMeal: j['last_meal'] is Map ? LastMeal.fromJson(asMap(j['last_meal'])) : null,
      );

  Json toJson() =>
      {'state': state, 'hopper_g': hopperG, 'today_g': todayG, 'last_meal': lastMeal?.toJson()};
}

class LauncherState {
  final String state;
  final int balls;

  const LauncherState({required this.state, required this.balls});

  factory LauncherState.fromJson(Json j) =>
      LauncherState(state: asString(j['state'], 'idle'), balls: asInt(j['balls']));

  Json toJson() => {'state': state, 'balls': balls};
}

class SafetyState {
  final bool estop;
  final int cliffMask;
  final int bumpMask;
  final bool wheelDrop;
  final bool motorFault;

  const SafetyState({
    required this.estop,
    required this.cliffMask,
    required this.bumpMask,
    required this.wheelDrop,
    required this.motorFault,
  });

  factory SafetyState.fromJson(Json j) => SafetyState(
        estop: asBool(j['estop']),
        cliffMask: asInt(j['cliff_mask']),
        bumpMask: asInt(j['bump_mask']),
        wheelDrop: asBool(j['wheel_drop']),
        motorFault: asBool(j['motor_fault']),
      );

  bool get anyAlarm => estop || cliffMask != 0 || bumpMask != 0 || wheelDrop || motorFault;

  Json toJson() => {
        'estop': estop,
        'cliff_mask': cliffMask,
        'bump_mask': bumpMask,
        'wheel_drop': wheelDrop,
        'motor_fault': motorFault,
      };
}

class NavState {
  final String status; // idle|active|succeeded|failed|canceled
  final Object? goal; // spec leaves the shape open (null in example)
  final bool mapping;

  const NavState({required this.status, this.goal, required this.mapping});

  factory NavState.fromJson(Json j) =>
      NavState(status: asString(j['status'], 'idle'), goal: j['goal'], mapping: asBool(j['mapping']));

  Json toJson() => {'status': status, 'goal': goal, 'mapping': mapping};
}

class PetSighting {
  final double? lastSeenTs;
  final String? camera;
  final double? bearingRad;

  const PetSighting({this.lastSeenTs, this.camera, this.bearingRad});

  factory PetSighting.fromJson(Json j) => PetSighting(
        lastSeenTs: asDoubleOrNull(j['last_seen_ts']),
        camera: asStringOrNull(j['camera']),
        bearingRad: asDoubleOrNull(j['bearing_rad']),
      );

  Json toJson() => {'last_seen_ts': lastSeenTs, 'camera': camera, 'bearing_rad': bearingRad};
}

class RobotState {
  final double ts;
  final BatteryState battery;
  final Pose pose;
  final String mode; // auto|manual|sleep
  final String activity;
  final FeederState feeder;
  final LauncherState launcher;
  final SafetyState safety;
  final NavState nav;
  final PetSighting pet;
  final List<String> pois;
  final String fw;

  const RobotState({
    required this.ts,
    required this.battery,
    required this.pose,
    required this.mode,
    required this.activity,
    required this.feeder,
    required this.launcher,
    required this.safety,
    required this.nav,
    required this.pet,
    required this.pois,
    required this.fw,
  });

  factory RobotState.fromJson(Json j) => RobotState(
        ts: asDouble(j['ts']),
        battery: BatteryState.fromJson(asMap(j['battery'])),
        pose: Pose.fromJson(asMap(j['pose'])),
        mode: asString(j['mode'], 'auto'),
        activity: asString(j['activity'], 'idle'),
        feeder: FeederState.fromJson(asMap(j['feeder'])),
        launcher: LauncherState.fromJson(asMap(j['launcher'])),
        safety: SafetyState.fromJson(asMap(j['safety'])),
        nav: NavState.fromJson(asMap(j['nav'])),
        pet: PetSighting.fromJson(asMap(j['pet'])),
        pois: asStringList(j['pois']),
        fw: asString(j['fw']),
      );

  Json toJson() => {
        't': 'state',
        'ts': ts,
        'battery': battery.toJson(),
        'pose': pose.toJson(),
        'mode': mode,
        'activity': activity,
        'feeder': feeder.toJson(),
        'launcher': launcher.toJson(),
        'safety': safety.toJson(),
        'nav': nav.toJson(),
        'pet': pet.toJson(),
        'pois': List<String>.of(pois),
        'fw': fw,
      };
}
