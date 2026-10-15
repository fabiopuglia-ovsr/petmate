// Teleop sender — §4.2: `{"t":"teleop","v":..,"w":..}` at 10 Hz, deadman 300 ms.
//
// The robot stops if it receives no teleop for 300 ms. The app side mirrors
// that: it streams at 10 Hz only while the joystick is held AND the joystick
// has produced input within the deadman window; when released (or input goes
// stale, e.g. the UI thread stalls or the finger leaves the widget without an
// up-event) it sends a single explicit zero and stops sending.
//
// Pure Dart, time and timers injectable for unit tests.
import 'dart:async';

import '../models/commands.dart';
import '../models/json_utils.dart';

typedef TeleopSink = void Function(Json cmd);
typedef Clock = Duration Function();
typedef PeriodicFactory = Timer Function(Duration period, void Function(Timer) cb);

class TeleopController {
  static const Duration period = Duration(milliseconds: 100); // 10 Hz
  static const Duration deadman = Duration(milliseconds: 300);

  final TeleopSink _sink;
  final Clock _now;
  final PeriodicFactory _periodic;

  double _v = 0, _w = 0;
  Duration? _lastInput;
  Timer? _timer;
  bool _held = false;

  /// Whether a non-zero command was last sent (needs a trailing stop).
  bool _movingSent = false;

  TeleopController(
    this._sink, {
    Clock? now,
    PeriodicFactory? periodic,
  })  : _now = now ?? _stopwatchClock(),
        _periodic = periodic ?? Timer.periodic;

  static Clock _stopwatchClock() {
    final sw = Stopwatch()..start();
    return () => sw.elapsed;
  }

  bool get active => _held;
  double get v => _v;
  double get w => _w;

  /// Joystick input in normalized units: [x] right-positive, [y] up-positive,
  /// both in -1..1. Maps to v (forward) and w (left-positive, ROS REP-103).
  void input(double x, double y, {double speedScale = 1.0}) {
    final s = speedScale.clamp(0.0, 1.0);
    setVelocity(y.clamp(-1.0, 1.0) * Cmd.maxTeleopV * s, -x.clamp(-1.0, 1.0) * Cmd.maxTeleopW * s);
  }

  /// Direct velocity input (m/s, rad/s). Starts streaming if not already.
  void setVelocity(double v, double w) {
    _v = v;
    _w = w;
    _lastInput = _now();
    if (!_held) {
      _held = true;
      _tick(); // send immediately, don't wait a full period
      _timer = _periodic(period, (_) => _tick());
    }
  }

  /// Keeps the current command alive without changing it (finger held still).
  void keepAlive() {
    if (_held) _lastInput = _now();
  }

  /// Joystick released: one explicit zero, then silence.
  void release() {
    _timer?.cancel();
    _timer = null;
    final wasHeld = _held;
    _held = false;
    _v = 0;
    _w = 0;
    if (wasHeld || _movingSent) {
      _sink(Cmd.teleop(0, 0));
      _movingSent = false;
    }
  }

  void _tick() {
    final last = _lastInput;
    if (last == null || _now() - last > deadman) {
      // Input went stale: deadman trips on the app side too.
      release();
      return;
    }
    _sink(Cmd.teleop(_v, _w));
    _movingSent = _v != 0 || _w != 0;
  }

  void dispose() {
    _timer?.cancel();
    _timer = null;
    _held = false;
  }
}
