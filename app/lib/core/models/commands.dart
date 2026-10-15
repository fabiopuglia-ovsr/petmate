// DataChannel "ctl" commands — INTERFACES.md §4.2. Builders clamp to the
// documented ranges so UI bugs can't send out-of-contract values.
import 'json_utils.dart';

abstract final class Cmd {
  static Json teleop(double v, double w) => {
        't': 'teleop',
        'v': _round3(v.clamp(-maxTeleopV, maxTeleopV)),
        'w': _round3(w.clamp(-maxTeleopW, maxTeleopW)),
      };

  /// §1: teleop v_max 0.50 m/s, ω_max 2.0 rad/s.
  static const double maxTeleopV = 0.5;
  static const double maxTeleopW = 2.0;

  static Json feed(int grams) => {'t': 'feed', 'grams': grams.clamp(1, 60)};
  static Json feedAbort() => {'t': 'feed_abort'};

  /// power clamp 20–70, count 1–4 (§2 LAUNCH).
  static Json launch({int power = 40, int count = 1}) =>
      {'t': 'launch', 'power': power.clamp(20, 70), 'count': count.clamp(1, 4)};

  static Json intake(String mode) {
    assert(const ['in', 'out', 'off'].contains(mode));
    return {'t': 'intake', 'mode': mode};
  }

  static const teasePatterns = ['wiggle', 'sweep', 'random', 'stop'];
  static Json tease(String pattern, {int durationS = 20}) {
    assert(teasePatterns.contains(pattern));
    return {'t': 'tease', 'pattern': pattern, 'duration_s': durationS};
  }

  static const ledModes = ['solid', 'breathe', 'rainbow', 'chase', 'off'];
  static Json led(String mode, String colorHex) {
    assert(ledModes.contains(mode));
    return {'t': 'led', 'mode': mode, 'color': colorHex};
  }

  static Json gotoDock() => {'t': 'goto', 'target': 'dock'};
  static Json gotoPet() => {'t': 'goto', 'target': 'pet'};
  static Json gotoPoi(String poi) => {'t': 'goto', 'target': 'poi', 'poi': poi};

  static Json mode(String mode) {
    assert(const ['auto', 'manual', 'sleep'].contains(mode));
    return {'t': 'mode', 'mode': mode};
  }

  static Json estop(bool engage) => {'t': 'estop', 'engage': engage};

  static Json play(String game, {int durationS = 300}) =>
      {'t': 'play', 'game': game, 'duration_s': durationS};

  static Json savePoi(String name) => {'t': 'save_poi', 'name': name};

  static Json map(String action) {
    assert(const ['start_mapping', 'stop_mapping', 'save', 'clear'].contains(action));
    return {'t': 'map', 'action': action};
  }

  // ---- DataChannel-only (pm-core) ----
  static Json camera(String id) {
    assert(id == 'front' || id == 'rear');
    return {'t': 'camera', 'id': id};
  }

  static Json display(String mode, {String? game}) =>
      {'t': 'display', 'mode': mode, if (game != null) 'game': game};

  static Json volume(int speaker) => {'t': 'volume', 'speaker': speaker.clamp(0, 100)};
  static Json snapshot() => {'t': 'snapshot'};
  static Json recordClip(int seconds) => {'t': 'record_clip', 'seconds': seconds};
  static Json configGet() => {'t': 'config_get'};
  static Json configSet(Json config) => {'t': 'config_set', 'config': config};

  static double _round3(num x) => (x * 1000).round() / 1000;
}
