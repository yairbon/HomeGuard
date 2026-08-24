import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../services/backend_service.dart';
import '../theme/app_theme.dart';
import 'homeguard_icons.dart';

/// Maps an alarm state to its status color — the single source of truth
/// used by [AlarmStatusIndicator] and by [HomeScreen]'s large status
/// headline, so the hero and the headline can never disagree about what
/// color a state is.
Color alarmStateColor(AlarmState state) {
  switch (state) {
    case AlarmState.disarmed:
      return AppColors.safe;
    case AlarmState.armed:
      return AppColors.alarm;
    case AlarmState.triggered:
      return AppColors.alarm;
    case AlarmState.transitional:
    // Entry delay shares the transitional amber deliberately: like arming,
    // it's a "the clock is running, act now" state rather than a settled
    // one. Painting it alarm-red would be indistinguishable from the alarm
    // having already gone off.
    case AlarmState.entryDelay:
      return AppColors.transitional;
  }
}

/// Maps an alarm state to its short status word, shared the same way as
/// [alarmStateColor].
String alarmStateLabel(AlarmState state) {
  switch (state) {
    case AlarmState.disarmed:
      return 'DISARMED';
    case AlarmState.armed:
      return 'ARMED';
    case AlarmState.triggered:
      return 'ALERT';
    case AlarmState.transitional:
      return 'ARMING';
    case AlarmState.entryDelay:
      return 'DISARM NOW';
  }
}

/// The 3 real sensor nodes drawn around the hero's center panel, and the
/// fixed angle each sits at (12 o'clock, then 120° apart) — shared between
/// the painter (which draws the traces) and the widget (which positions the
/// icon overlay for each node), so the two can never drift out of sync.
const Map<SensorType, double> _nodeAngles = {
  SensorType.ultrasonic: -math.pi / 2,
  SensorType.door: -math.pi / 2 + 2 * math.pi / 3,
  SensorType.vibration: -math.pi / 2 + 4 * math.pi / 3,
};

/// Maps a sensor type to the custom glyph drawn at its node.
HGIcon _nodeIconFor(SensorType type) {
  switch (type) {
    case SensorType.ultrasonic:
      return HGIcon.ultrasonic;
    case SensorType.door:
      return HGIcon.door;
    case SensorType.vibration:
      return HGIcon.vibration;
  }
}

/// HomeGuard's signature visual element: a literal schematic of the 3 real
/// ESP32 sensor boards (ultrasonic, door, vibration), wired by straight
/// dashed traces into a center panel pad — replacing an earlier version
/// that used one abstract glowing ring for the whole system. That version
/// couldn't show which sensor actually tripped; this one can, because each
/// node's color comes from that specific sensor's own live ok/bypassed
/// state, not just the aggregate alarm state.
class AlarmStatusIndicator extends StatefulWidget {
  const AlarmStatusIndicator({
    super.key,
    required this.state,
    required this.sensors,
    this.size = 240,
    this.armingSecondsRemaining,
  });

  /// Current alarm state to render — drives the center panel's color/label
  /// and is the fallback color for any node that's monitored and healthy.
  final AlarmState state;

  /// Live per-sensor readings. Each of the 3 known [SensorType]s gets its
  /// own node; a type missing from this list (e.g. before the first
  /// backend sync) renders as an idle/unknown node rather than crashing.
  final List<SensorReading> sensors;

  /// Overall square footprint of the widget in logical pixels; every visual
  /// element (nodes, traces, panel, glow) is sized as a fraction of this
  /// value.
  final double size;

  /// Seconds left in the arming exit delay (see [AlarmService]). Only
  /// rendered while [state] is [AlarmState.transitional] and this is
  /// greater than zero — shown as a countdown digit on the center panel.
  final int? armingSecondsRemaining;

  @override
  State<AlarmStatusIndicator> createState() => _AlarmStatusIndicatorState();
}

/// Owns the animation controller that drives the triggered strobe, the
/// transitional half-pulse, and a subtle continuous "loop is live" breathe
/// on the traces while armed.
class _AlarmStatusIndicatorState extends State<AlarmStatusIndicator>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;

  @override
  void initState() {
    super.initState();
    // 900ms reverse-repeat gives a steady heartbeat-like pulse rather than a
    // hard reset-to-zero flash on every cycle.
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 900),
    );
    _syncAnimation();
  }

  @override
  void didUpdateWidget(AlarmStatusIndicator oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.state != widget.state) _syncAnimation();
  }

  /// Runs the pulse only in the states that actually use it, and stops it
  /// everywhere else.
  ///
  /// It used to `repeat(reverse: true)` from [initState] and never stop. In
  /// `disarmed` — the state the app sits in almost all the time, and the one
  /// it's showing whenever someone opens it to check on a quiet house — the
  /// builder computes `pulse = 0.0` and `tracePulse = null`, so every one of
  /// those 60 ticks a second rebuilt an identical widget tree and scheduled
  /// another frame, for a screen that was not changing at all.
  void _syncAnimation() {
    final shouldPulse = switch (widget.state) {
      AlarmState.triggered || AlarmState.transitional || AlarmState.armed => true,
      AlarmState.disarmed || AlarmState.entryDelay => false,
    };
    if (shouldPulse) {
      if (!_controller.isAnimating) _controller.repeat(reverse: true);
    } else if (_controller.isAnimating) {
      _controller.stop();
      // Park at 0 so the resting frame is the same one every time, rather
      // than wherever the pulse happened to be when the state changed.
      _controller.value = 0;
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  /// Color used for the center panel border/label and as the fallback
  /// color for a healthy, non-bypassed node — see [alarmStateColor].
  Color get _color => alarmStateColor(widget.state);

  /// Finds this sensor's live reading, or null if the backend hasn't
  /// reported one yet (e.g. very first frame before the initial sync).
  SensorReading? _sensorOf(SensorType type) {
    for (final s in widget.sensors) {
      if (s.type == type) return s;
    }
    return null;
  }

  /// Per-node color: a genuinely breached sensor is always flagged alarm-red
  /// regardless of the overall state (so the schematic can point at exactly
  /// which board tripped), a bypassed sensor renders dim/ghosted (Home
  /// mode's "ignore this one" made visible), and everything else follows
  /// the overall state color once armed, or the muted trace-ink color while
  /// disarmed (an idle, unpowered loop).
  Color _nodeColorFor(SensorReading? sensor) {
    if (sensor == null) return AppColors.trace;
    if (sensor.bypassed) return AppColors.trace.withValues(alpha: 0.5);
    if (!sensor.ok) return AppColors.alarm;
    return widget.state == AlarmState.disarmed ? AppColors.trace : _color;
  }

  /// Positions one sensor's icon-in-circle node at its fixed angle, offset
  /// out from the Stack's center via [Transform.translate] rather than
  /// manual [Positioned] pixel math.
  Widget _buildNode(SensorType type, Color color) {
    final angle = _nodeAngles[type]!;
    final radius = widget.size * 0.38;
    final bgSize = widget.size * 0.24;
    return Transform.translate(
      offset: Offset(math.cos(angle), math.sin(angle)) * radius,
      child: Container(
        width: bgSize,
        height: bgSize,
        alignment: Alignment.center,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: color.withValues(alpha: 0.14),
          border: Border.all(color: color, width: widget.size * 0.008),
        ),
        child: HomeGuardIcon(_nodeIconFor(type), size: bgSize * 0.5, color: color),
      ),
    );
  }

  /// Layout is a centered [Stack]: an optional glow bloom (triggered only — a
  /// steady-state glow on every screen view was itself one of the "generic AI
  /// dashboard" tells), the [_SchematicPainter] drawing the 3 traces and the
  /// center panel pad, the 3 node icon overlays, and the countdown digit when
  /// arming.
  ///
  /// Only the first two of those depend on the animation. The 3 sensor nodes,
  /// the colours they're built from, and the countdown digit are functions of
  /// the widget's fields alone, yet they sat inside the builder and were
  /// rebuilt 60 times a second along with everything else — the countdown's
  /// text style even re-resolved a Google font on every tick. They're built
  /// once here and handed to [AnimatedBuilder] as its `child`, which is
  /// exactly what that parameter is for and which the previous version
  /// ignored.
  @override
  Widget build(BuildContext context) {
    final nodeColors = {
      for (final type in SensorType.values) type: _nodeColorFor(_sensorOf(type)),
    };
    final countdown = widget.armingSecondsRemaining;
    final showCountdown =
        widget.state == AlarmState.transitional && countdown != null && countdown > 0;

    final overlay = Stack(
      alignment: Alignment.center,
      children: [
        for (final type in SensorType.values) _buildNode(type, nodeColors[type]!),
        if (showCountdown)
          Text(
            '$countdown',
            style: AppTheme.monoCountdown.copyWith(
              fontSize: widget.size * 0.11,
              color: _color,
            ),
          ),
      ],
    );

    return SizedBox(
      width: widget.size,
      height: widget.size,
      child: AnimatedBuilder(
        animation: _controller,
        child: overlay,
        builder: (context, child) {
          final isTriggered = widget.state == AlarmState.triggered;
          final pulse = isTriggered
              ? _controller.value
              : (widget.state == AlarmState.transitional ? _controller.value * 0.5 : 0.0);
          // A slow continuous breathe on the traces while armed — "the loop
          // is live and being polled" — distinct from the triggered strobe.
          final tracePulse = widget.state == AlarmState.armed ? _controller.value : null;

          return Stack(
            alignment: Alignment.center,
            children: [
              if (isTriggered)
                Container(
                  width: widget.size * (0.7 + pulse * 0.3),
                  height: widget.size * (0.7 + pulse * 0.3),
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    gradient: RadialGradient(
                      colors: [
                        _color.withValues(alpha: 0.35 + pulse * 0.25),
                        _color.withValues(alpha: 0.0),
                      ],
                    ),
                  ),
                ),
              CustomPaint(
                size: Size(widget.size, widget.size),
                painter: _SchematicPainter(
                  nodeColors: nodeColors,
                  panelColor: _color,
                  panelFill: AppColors.surfaceRaised,
                  tracePulse: tracePulse,
                ),
              ),
              child!,
            ],
          );
        },
      ),
    );
  }
}

/// Draws the 3 dashed straight-line traces (node -> center) and the center
/// PANEL pad on top of them, so each trace's end disappears cleanly under
/// the pad rather than needing exact-angle geometry to stop short of it.
class _SchematicPainter extends CustomPainter {
  _SchematicPainter({
    required this.nodeColors,
    required this.panelColor,
    required this.panelFill,
    this.tracePulse,
  });

  /// This node's current color, one per [SensorType] — see
  /// [_AlarmStatusIndicatorState._nodeColorFor].
  final Map<SensorType, Color> nodeColors;

  /// Border/label color for the center panel pad — always the overall
  /// [alarmStateColor], independent of any single node's state.
  final Color panelColor;

  /// Fill color for the center panel pad.
  final Color panelFill;

  /// `null` when steady. When non-null (only while armed), a 0..1 value
  /// used to breathe the traces' opacity, signaling "the loop is live"
  /// without the constant glow bloom the earlier ring design used.
  final double? tracePulse;

  /// Draws a single trace as short straight dashes with small gaps —
  /// intentionally a straight line, not an arc: real PCB traces are
  /// polylines, and an earlier version of this widget drew smooth arcs
  /// that (per a design review) read as a generic dashed progress ring
  /// rather than anything resembling actual circuit-trace routing.
  void _drawDashedLine(Canvas canvas, Offset from, Offset to, Paint paint, double refSize) {
    final total = (to - from).distance;
    if (total <= 0) return;
    final direction = (to - from) / total;
    final dashLen = refSize * 0.045;
    final gapLen = refSize * 0.03;
    var covered = 0.0;
    while (covered < total) {
      final segStart = from + direction * covered;
      final segEndDist = math.min(covered + dashLen, total);
      final segEnd = from + direction * segEndDist;
      canvas.drawLine(segStart, segEnd, paint);
      covered += dashLen + gapLen;
    }
  }

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final radius = size.width * 0.38;
    final baseOpacity = tracePulse != null ? 0.75 + tracePulse! * 0.25 : 0.85;

    for (final entry in _nodeAngles.entries) {
      final color = nodeColors[entry.key] ?? AppColors.trace;
      final nodePos = center + Offset(math.cos(entry.value), math.sin(entry.value)) * radius;
      final paint = Paint()
        ..color = color.withValues(alpha: baseOpacity)
        ..style = PaintingStyle.stroke
        ..strokeWidth = size.width * 0.018
        ..strokeCap = StrokeCap.round;
      _drawDashedLine(canvas, nodePos, center, paint, size.width);
    }

    // Center PANEL pad, drawn last so every trace's end tucks cleanly
    // underneath it regardless of the exact angle it arrives at.
    final panelRect = Rect.fromCenter(
      center: center,
      width: size.width * 0.28,
      height: size.width * 0.28,
    );
    final panelRRect = RRect.fromRectAndRadius(panelRect, Radius.circular(size.width * 0.035));
    canvas.drawRRect(panelRRect, Paint()..color = panelFill);
    canvas.drawRRect(
      panelRRect,
      Paint()
        ..color = panelColor
        ..style = PaintingStyle.stroke
        ..strokeWidth = size.width * 0.014,
    );
  }

  bool _nodeColorsEqual(Map<SensorType, Color> other) {
    for (final type in SensorType.values) {
      if (nodeColors[type] != other[type]) return false;
    }
    return true;
  }

  @override
  bool shouldRepaint(covariant _SchematicPainter oldDelegate) {
    return !oldDelegate._nodeColorsEqual(nodeColors) ||
        oldDelegate.panelColor != panelColor ||
        oldDelegate.panelFill != panelFill ||
        oldDelegate.tracePulse != tracePulse;
  }
}
