import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../theme/app_theme.dart';

/// HomeGuard's static brand mark: a closed circuit-trace ring with four
/// via-leads bridging in to a center node — the same construction
/// language as [AlarmStatusIndicator]'s live status ring, frozen (no
/// pulse, no armed/disarmed meaning) so it reads as a logo rather than a
/// status readout. Used on the login screen instead of a generic shield
/// icon.
class HomeGuardLogo extends StatelessWidget {
  const HomeGuardLogo({super.key, this.size = 72, this.color = AppColors.system});

  final double size;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: size,
      height: size,
      child: CustomPaint(painter: _LogoPainter(color: color)),
    );
  }
}

class _LogoPainter extends CustomPainter {
  _LogoPainter({required this.color});

  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final ringRadius = size.width / 2 * 0.82;

    final ringPaint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = size.width * 0.05
      ..strokeCap = StrokeCap.round;

    // A dashed ring built the same way as the live status ring (short
    // arcs with small gaps), always drawn fully closed here — the logo
    // has no "disarmed" state to represent.
    const segmentCount = 18;
    final sweepPerSegment = (2 * math.pi) / segmentCount;
    final segmentSweep = sweepPerSegment * 0.7;
    final startAngle = -math.pi / 2;
    for (var i = 0; i < segmentCount; i++) {
      canvas.drawArc(
        Rect.fromCircle(center: center, radius: ringRadius),
        startAngle + i * sweepPerSegment,
        segmentSweep,
        false,
        ringPaint,
      );
    }

    // Four via-leads bridging the ring inward to the center node, like
    // traces converging on a component — reinforces "circuit" over
    // "generic badge outline".
    final leadPaint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = size.width * 0.035
      ..strokeCap = StrokeCap.round;
    for (final angle in [0.0, math.pi / 2, math.pi, 3 * math.pi / 2]) {
      final direction = Offset(math.cos(angle), math.sin(angle));
      canvas.drawLine(
        center + direction * (size.width * 0.17),
        center + direction * (ringRadius * 0.7),
        leadPaint,
      );
    }

    canvas.drawCircle(center, size.width * 0.1, Paint()..color = color);
  }

  @override
  bool shouldRepaint(covariant _LogoPainter oldDelegate) => oldDelegate.color != color;
}
