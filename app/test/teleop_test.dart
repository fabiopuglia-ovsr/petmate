import 'dart:async';

import 'package:petmate/core/teleop/teleop_controller.dart';
import 'package:flutter_test/flutter_test.dart';

/// Deterministic fake time: a manual clock + periodic timers advanced by hand.
class FakeTime {
  Duration now = Duration.zero;
  final List<_FakeTimer> _timers = [];

  Duration clock() => now;

  Timer periodic(Duration period, void Function(Timer) cb) {
    final t = _FakeTimer(period, cb, now + period);
    _timers.add(t);
    return t;
  }

  /// Advances time in 1 ms steps firing due timers.
  void advance(Duration d) {
    final end = now + d;
    while (now < end) {
      now += const Duration(milliseconds: 1);
      for (final t in List.of(_timers)) {
        if (!t.isActive) {
          _timers.remove(t);
          continue;
        }
        if (now >= t.next) {
          t.next += t.period;
          t.tick++;
          t.cb(t);
        }
      }
    }
  }
}

class _FakeTimer implements Timer {
  final Duration period;
  final void Function(Timer) cb;
  Duration next;
  bool _active = true;
  @override
  int tick = 0;
  _FakeTimer(this.period, this.cb, this.next);
  @override
  void cancel() => _active = false;
  @override
  bool get isActive => _active;
}

void main() {
  late FakeTime ft;
  late List<Map<String, dynamic>> sent;
  late TeleopController tc;

  setUp(() {
    ft = FakeTime();
    sent = [];
    tc = TeleopController(sent.add, now: ft.clock, periodic: ft.periodic);
  });

  test('sends immediately, then at 10 Hz while input is fresh', () {
    tc.setVelocity(0.2, 0.5);
    expect(sent, hasLength(1));
    expect(sent.first, {'t': 'teleop', 'v': 0.2, 'w': 0.5});
    // Keep input alive (like the joystick heartbeat) for 1 s.
    for (var i = 0; i < 10; i++) {
      ft.advance(const Duration(milliseconds: 100));
      tc.keepAlive();
    }
    expect(sent, hasLength(11)); // 1 immediate + 10 periodic
    expect(sent.every((m) => m['v'] == 0.2 && m['w'] == 0.5), isTrue);
  });

  test('rate never exceeds 10 Hz even with rapid input', () {
    tc.setVelocity(0.1, 0);
    for (var i = 0; i < 100; i++) {
      ft.advance(const Duration(milliseconds: 10));
      tc.setVelocity(0.1 + i * 0.001, 0); // 100 Hz input
    }
    // 1 s of streaming: 1 immediate + 10 periodic.
    expect(sent, hasLength(11));
  });

  test('latest input value is what gets sent', () {
    tc.setVelocity(0.1, 0);
    ft.advance(const Duration(milliseconds: 50));
    tc.setVelocity(0.3, -1.0);
    ft.advance(const Duration(milliseconds: 50));
    expect(sent.last, {'t': 'teleop', 'v': 0.3, 'w': -1.0});
  });

  test('deadman: stale input (>300 ms) sends one zero and stops', () {
    tc.setVelocity(0.4, 0);
    ft.advance(const Duration(milliseconds: 300)); // ticks at 100,200,300 still fresh
    expect(sent.where((m) => m['v'] == 0.4), hasLength(4));
    ft.advance(const Duration(milliseconds: 100)); // tick at 400 → stale
    expect(sent.last, {'t': 'teleop', 'v': 0.0, 'w': 0.0});
    expect(tc.active, isFalse);
    final n = sent.length;
    ft.advance(const Duration(seconds: 2));
    expect(sent, hasLength(n), reason: 'no traffic after deadman trip');
  });

  test('release sends a single explicit stop and silences the stream', () {
    tc.setVelocity(0.3, 0.3);
    ft.advance(const Duration(milliseconds: 150));
    tc.release();
    expect(sent.last, {'t': 'teleop', 'v': 0.0, 'w': 0.0});
    final n = sent.length;
    ft.advance(const Duration(seconds: 1));
    expect(sent, hasLength(n));
    tc.release(); // idempotent: no extra stop
    expect(sent, hasLength(n));
  });

  test('restart after release streams again', () {
    tc.setVelocity(0.2, 0);
    tc.release();
    sent.clear();
    tc.setVelocity(-0.2, 0);
    ft.advance(const Duration(milliseconds: 100));
    expect(sent, hasLength(2));
    expect(sent.first['v'], -0.2);
  });

  test('joystick mapping: up = forward, right = clockwise (w<0), clamped to limits', () {
    tc.input(0, 1);
    expect(sent.last, {'t': 'teleop', 'v': 0.5, 'w': 0.0});
    tc.input(1, 0);
    ft.advance(const Duration(milliseconds: 100));
    expect(sent.last, {'t': 'teleop', 'v': 0.0, 'w': -2.0});
    tc.input(-3, -3, speedScale: 0.5);
    ft.advance(const Duration(milliseconds: 100));
    expect(sent.last, {'t': 'teleop', 'v': -0.25, 'w': 1.0});
  });

  test('dispose stops timer without sending', () {
    tc.setVelocity(0.2, 0);
    final n = sent.length;
    tc.dispose();
    ft.advance(const Duration(seconds: 1));
    expect(sent, hasLength(n));
  });
}
