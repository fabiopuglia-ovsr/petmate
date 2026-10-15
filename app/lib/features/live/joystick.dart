import 'dart:async';
import 'dart:math';

import 'package:flutter/material.dart';

import '../../core/widgets/theme.dart';

/// Virtual joystick. Reports normalized (x right+, y up+) in -1..1 while the
/// finger is down, and calls [onRelease] on up/cancel. The deadman logic lives
/// in TeleopController; this widget only reports input.
class Joystick extends StatefulWidget {
  final double size;
  final void Function(double x, double y) onMove;
  final VoidCallback onRelease;
  final bool enabled;

  const Joystick({
    super.key,
    this.size = 168,
    required this.onMove,
    required this.onRelease,
    this.enabled = true,
  });

  @override
  State<Joystick> createState() => _JoystickState();
}

class _JoystickState extends State<Joystick> {
  Offset _knob = Offset.zero;
  bool _active = false;
  double _x = 0, _y = 0;

  /// While the finger rests without moving there are no pointer events; this
  /// heartbeat re-reports the held value so the teleop deadman (300 ms) only
  /// trips on a real stall (UI frozen, pointer lost), not on a steady thumb.
  Timer? _heartbeat;

  double get _radius => widget.size / 2;
  double get _travel => _radius - 28;

  void _update(Offset local) {
    var d = local - Offset(_radius, _radius);
    if (d.distance > _travel) d = Offset.fromDirection(d.direction, _travel);
    setState(() => _knob = d);
    // Small dead zone avoids drift from a resting thumb.
    double norm(double v) {
      final n = v / _travel;
      return n.abs() < 0.08 ? 0.0 : n.clamp(-1.0, 1.0);
    }

    _x = norm(d.dx);
    _y = -norm(d.dy);
    widget.onMove(_x, _y);
    _heartbeat ??= Timer.periodic(const Duration(milliseconds: 100), (_) {
      if (_active) widget.onMove(_x, _y);
    });
  }

  void _end() {
    _heartbeat?.cancel();
    _heartbeat = null;
    if (!_active) return;
    setState(() {
      _knob = Offset.zero;
      _active = false;
    });
    widget.onRelease();
  }

  @override
  void didUpdateWidget(Joystick old) {
    super.didUpdateWidget(old);
    if (!widget.enabled && _active) _end();
  }

  @override
  void dispose() {
    _heartbeat?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Listener(
      onPointerDown: widget.enabled
          ? (e) {
              _active = true;
              _update(e.localPosition);
            }
          : null,
      onPointerMove: widget.enabled ? (e) => _active ? _update(e.localPosition) : null : null,
      onPointerUp: (_) => _end(),
      onPointerCancel: (_) => _end(),
      child: SizedBox(
        width: widget.size,
        height: widget.size,
        child: CustomPaint(
          painter: _JoyPainter(knob: _knob, active: _active, enabled: widget.enabled),
        ),
      ),
    );
  }
}

class _JoyPainter extends CustomPainter {
  final Offset knob;
  final bool active;
  final bool enabled;
  _JoyPainter({required this.knob, required this.active, required this.enabled});

  @override
  void paint(Canvas canvas, Size size) {
    final c = size.center(Offset.zero);
    final r = size.width / 2;
    canvas.drawCircle(c, r, Paint()..color = const Color(0x55FFFFFF));
    canvas.drawCircle(
        c,
        r - 1,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2
          ..color = const Color(0xAAFFFFFF));
    // Direction ticks
    final tick = Paint()
      ..color = const Color(0xAAFFFFFF)
      ..strokeWidth = 3
      ..strokeCap = StrokeCap.round;
    for (var i = 0; i < 4; i++) {
      final a = i * pi / 2;
      final p1 = c + Offset.fromDirection(a, r - 14);
      final p2 = c + Offset.fromDirection(a, r - 6);
      canvas.drawLine(p1, p2, tick);
    }
    canvas.drawCircle(
      c + knob,
      26,
      Paint()..color = !enabled ? Colors.grey : (active ? Brand.yellow : Colors.white),
    );
  }

  @override
  bool shouldRepaint(_JoyPainter old) => old.knob != knob || old.active != active || old.enabled != enabled;
}
