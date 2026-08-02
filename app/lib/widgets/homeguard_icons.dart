import 'dart:math' as math;

import 'package:flutter/material.dart';

/// HomeGuard's custom icon language — every glyph here is drawn from the
/// same visual grammar as [AlarmStatusIndicator]'s circuit-ring motif
/// (thin uniform stroke, round caps/joins, small filled "via-dot" accents
/// at a few line endpoints) instead of reaching for stock Material icons.
/// The point isn't decoration — a whole coherent icon system built from
/// one visual idea reads as designed, where mixing in generic icons
/// alongside a custom motif reads as assembled.
enum HGIcon {
  dashboard,
  camera,
  faces,
  events,
  settings,
  ultrasonic,
  door,
  vibration,
  armHome,
  armAway,
  emergency,
  nightMode,
  logout,
  addFace,
  alert,
  armDisarm,
  auth,
  info,
}

class HomeGuardIcon extends StatelessWidget {
  const HomeGuardIcon(this.icon, {super.key, this.size = 24, this.color = Colors.white});

  final HGIcon icon;
  final double size;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return CustomPaint(
      size: Size(size, size),
      painter: _HGIconPainter(icon: icon, color: color),
    );
  }
}

class _HGIconPainter extends CustomPainter {
  _HGIconPainter({required this.icon, required this.color});

  final HGIcon icon;
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final stroke = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = w * 0.075
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    final dot = Paint()
      ..color = color
      ..style = PaintingStyle.fill;

    switch (icon) {
      case HGIcon.dashboard:
        _dashboard(canvas, w, stroke, dot);
      case HGIcon.camera:
        _camera(canvas, w, stroke, dot);
      case HGIcon.faces:
        _faces(canvas, w, stroke);
      case HGIcon.events:
        _events(canvas, w, stroke, dot);
      case HGIcon.settings:
        _settings(canvas, w, stroke, dot);
      case HGIcon.ultrasonic:
        _ultrasonic(canvas, w, stroke, dot);
      case HGIcon.door:
        _door(canvas, w, stroke, dot);
      case HGIcon.vibration:
        _vibration(canvas, w, stroke);
      case HGIcon.armHome:
        _armHome(canvas, w, stroke, dot);
      case HGIcon.armAway:
        _armAway(canvas, w, stroke);
      case HGIcon.emergency:
        _emergency(canvas, w, stroke);
      case HGIcon.nightMode:
        _nightMode(canvas, w, stroke);
      case HGIcon.logout:
        _logout(canvas, w, stroke);
      case HGIcon.addFace:
        _addFace(canvas, w, stroke, dot);
      case HGIcon.alert:
        _alert(canvas, w, stroke, dot);
      case HGIcon.armDisarm:
        _armDisarm(canvas, w, stroke, dot);
      case HGIcon.auth:
        _auth(canvas, w, stroke, dot);
      case HGIcon.info:
        _info(canvas, w, stroke, dot);
    }
  }

  // Rounded rectangle "screen" with a status-bar divider and a via-dot —
  // reads as a control-panel readout rather than a generic grid icon.
  void _dashboard(Canvas c, double w, Paint stroke, Paint dot) {
    final rect = Rect.fromLTWH(w * 0.16, w * 0.2, w * 0.68, w * 0.6);
    c.drawRRect(RRect.fromRectAndRadius(rect, Radius.circular(w * 0.08)), stroke);
    c.drawLine(Offset(w * 0.16, w * 0.4), Offset(w * 0.84, w * 0.4), stroke);
    c.drawCircle(Offset(w * 0.28, w * 0.3), w * 0.035, dot);
  }

  // Minimal camera body + lens, with a small "recording" via-dot — echoes
  // the REC dot used elsewhere in the camera tab.
  void _camera(Canvas c, double w, Paint stroke, Paint dot) {
    final body = Rect.fromLTWH(w * 0.14, w * 0.32, w * 0.72, w * 0.44);
    c.drawRRect(RRect.fromRectAndRadius(body, Radius.circular(w * 0.06)), stroke);
    final lensTop = Path()
      ..moveTo(w * 0.38, w * 0.32)
      ..lineTo(w * 0.46, w * 0.2)
      ..lineTo(w * 0.62, w * 0.2)
      ..lineTo(w * 0.68, w * 0.32);
    c.drawPath(lensTop, stroke);
    c.drawCircle(Offset(w * 0.5, w * 0.54), w * 0.14, stroke);
    c.drawCircle(Offset(w * 0.74, w * 0.4), w * 0.03, dot);
  }

  // A head (circle) + shoulders (open arc) — pure line-art person, no
  // fill, distinct from Material's solid person glyph.
  void _faces(Canvas c, double w, Paint stroke) {
    c.drawCircle(Offset(w * 0.5, w * 0.36), w * 0.16, stroke);
    final shoulders = Path()
      ..moveTo(w * 0.2, w * 0.82)
      ..quadraticBezierTo(w * 0.5, w * 0.56, w * 0.8, w * 0.82);
    c.drawPath(shoulders, stroke);
  }

  // Three list lines of decreasing length with a leading via-dot on the
  // first — reads as a log/event list rather than a generic hamburger.
  void _events(Canvas c, double w, Paint stroke, Paint dot) {
    final xs = [0.62, 0.5, 0.4];
    final ys = [0.3, 0.5, 0.7];
    for (var i = 0; i < 3; i++) {
      c.drawLine(Offset(w * 0.34, w * ys[i]), Offset(w * xs[i] + w * 0.24, w * ys[i]), stroke);
    }
    c.drawCircle(Offset(w * 0.22, w * 0.3), w * 0.035, dot);
  }

  // Three short "dial" sliders, each with a dot at a different position —
  // a control-panel take on settings instead of the ubiquitous gear.
  void _settings(Canvas c, double w, Paint stroke, Paint dot) {
    final ys = [0.28, 0.5, 0.72];
    final dotX = [0.62, 0.38, 0.7];
    for (var i = 0; i < 3; i++) {
      c.drawLine(Offset(w * 0.2, w * ys[i]), Offset(w * 0.8, w * ys[i]), stroke);
      c.drawCircle(Offset(w * dotX[i], w * ys[i]), w * 0.06, dot);
    }
  }

  // Two arcs fanning from a source dot — a directional sensor cone rather
  // than the generic full-circle WiFi glyph, matching the ultrasonic
  // sensor's actual directional sensing behavior.
  void _ultrasonic(Canvas c, double w, Paint stroke, Paint dot) {
    final origin = Offset(w * 0.22, w * 0.78);
    for (final r in [0.26, 0.44, 0.62]) {
      c.drawArc(
        Rect.fromCircle(center: origin, radius: w * r),
        -1.15,
        0.9,
        false,
        stroke,
      );
    }
    c.drawCircle(origin, w * 0.045, dot);
  }

  // A door-frame rectangle with a via-dot on the latch edge — the dot
  // stands in for the magnetic reed sensor's actual mounting point.
  void _door(Canvas c, double w, Paint stroke, Paint dot) {
    final rect = Rect.fromLTWH(w * 0.28, w * 0.16, w * 0.44, w * 0.68);
    c.drawRRect(RRect.fromRectAndRadius(rect, Radius.circular(w * 0.04)), stroke);
    c.drawLine(Offset(w * 0.28, w * 0.84), Offset(w * 0.72, w * 0.84), stroke);
    c.drawCircle(Offset(w * 0.62, w * 0.5), w * 0.04, dot);
  }

  // A jagged seismograph-style zigzag — immediately reads as vibration
  // without needing a label.
  void _vibration(Canvas c, double w, Paint stroke) {
    final path = Path()..moveTo(w * 0.12, w * 0.5);
    final points = [0.28, 0.4, 0.72, 0.6, 0.88, 0.5];
    final xs = [0.26, 0.4, 0.54, 0.68, 0.82, 0.9];
    for (var i = 0; i < xs.length; i++) {
      path.lineTo(w * xs[i], w * points[i]);
    }
    c.drawPath(path, stroke);
  }

  // A minimal roofline (peak + walls) with a via-dot at the apex — a
  // geometric house outline, not Material's filled home glyph.
  void _armHome(Canvas c, double w, Paint stroke, Paint dot) {
    final roof = Path()
      ..moveTo(w * 0.18, w * 0.48)
      ..lineTo(w * 0.5, w * 0.2)
      ..lineTo(w * 0.82, w * 0.48);
    c.drawPath(roof, stroke);
    final walls = Rect.fromLTWH(w * 0.28, w * 0.48, w * 0.44, w * 0.32);
    c.drawRect(walls, stroke);
    c.drawCircle(Offset(w * 0.5, w * 0.2), w * 0.04, dot);
  }

  // A square "zone" outline with an arrow breaking through the edge,
  // pointing outward — leaving the perimeter, for Away mode.
  void _armAway(Canvas c, double w, Paint stroke) {
    final zone = Rect.fromLTWH(w * 0.18, w * 0.22, w * 0.5, w * 0.56);
    c.drawRRect(RRect.fromRectAndRadius(zone, Radius.circular(w * 0.06)), stroke);
    final arrow = Path()
      ..moveTo(w * 0.5, w * 0.5)
      ..lineTo(w * 0.86, w * 0.5)
      ..moveTo(w * 0.72, w * 0.36)
      ..lineTo(w * 0.86, w * 0.5)
      ..lineTo(w * 0.72, w * 0.64);
    c.drawPath(arrow, stroke);
  }

  // A minimal handset — two rounded ends joined by a curve — the
  // classic phone silhouette reduced to its essential line.
  void _emergency(Canvas c, double w, Paint stroke) {
    final path = Path()
      ..moveTo(w * 0.24, w * 0.24)
      ..quadraticBezierTo(w * 0.2, w * 0.3, w * 0.28, w * 0.4)
      ..quadraticBezierTo(w * 0.4, w * 0.56, w * 0.58, w * 0.68)
      ..quadraticBezierTo(w * 0.68, w * 0.76, w * 0.74, w * 0.72)
      ..lineTo(w * 0.82, w * 0.64)
      ..quadraticBezierTo(w * 0.86, w * 0.6, w * 0.8, w * 0.54)
      ..lineTo(w * 0.7, w * 0.46)
      ..quadraticBezierTo(w * 0.64, w * 0.42, w * 0.6, w * 0.48)
      ..quadraticBezierTo(w * 0.5, w * 0.4, w * 0.42, w * 0.3)
      ..quadraticBezierTo(w * 0.48, w * 0.26, w * 0.44, w * 0.2)
      ..lineTo(w * 0.36, w * 0.14)
      ..quadraticBezierTo(w * 0.3, w * 0.1, w * 0.24, w * 0.24)
      ..close();
    c.drawPath(path, stroke);
  }

  // A crescent built from two overlapping circle arcs — matches the
  // ring-based construction language instead of a filled moon shape.
  void _nightMode(Canvas c, double w, Paint stroke) {
    c.drawArc(Rect.fromCircle(center: Offset(w * 0.46, w * 0.5), radius: w * 0.3), 2.0, 4.0, false, stroke);
    c.drawArc(
      Rect.fromCircle(center: Offset(w * 0.6, w * 0.42), radius: w * 0.22),
      -1.1,
      3.6,
      false,
      stroke,
    );
  }

  // A bracket with an arrow exiting through it — direct, no metaphor
  // needed for "log out."
  void _logout(Canvas c, double w, Paint stroke) {
    final bracket = Path()
      ..moveTo(w * 0.42, w * 0.2)
      ..lineTo(w * 0.22, w * 0.2)
      ..lineTo(w * 0.22, w * 0.8)
      ..lineTo(w * 0.42, w * 0.8);
    c.drawPath(bracket, stroke);
    c.drawLine(Offset(w * 0.36, w * 0.5), Offset(w * 0.82, w * 0.5), stroke);
    final arrowHead = Path()
      ..moveTo(w * 0.68, w * 0.36)
      ..lineTo(w * 0.82, w * 0.5)
      ..lineTo(w * 0.68, w * 0.64);
    c.drawPath(arrowHead, stroke);
  }

  // The faces glyph plus a small "+" — adding an approved face.
  void _addFace(Canvas c, double w, Paint stroke, Paint dot) {
    _faces(c, w, stroke);
    c.drawLine(Offset(w * 0.78, w * 0.16), Offset(w * 0.78, w * 0.32), stroke);
    c.drawLine(Offset(w * 0.7, w * 0.24), Offset(w * 0.86, w * 0.24), stroke);
  }

  // A triangle outline with an exclamation built from a line + dot rather
  // than Material's filled warning glyph — same idea, drawn in-language.
  void _alert(Canvas c, double w, Paint stroke, Paint dot) {
    final tri = Path()
      ..moveTo(w * 0.5, w * 0.16)
      ..lineTo(w * 0.86, w * 0.8)
      ..lineTo(w * 0.14, w * 0.8)
      ..close();
    c.drawPath(tri, stroke);
    c.drawLine(Offset(w * 0.5, w * 0.38), Offset(w * 0.5, w * 0.58), stroke);
    c.drawCircle(Offset(w * 0.5, w * 0.68), w * 0.035, dot);
  }

  // A miniature version of AlarmStatusIndicator's own circuit ring, closed,
  // with a center via-dot — arm/disarm log entries visually echo the
  // dashboard's main status motif instead of a generic shield.
  void _armDisarm(Canvas c, double w, Paint stroke, Paint dot) {
    final center = Offset(w * 0.5, w * 0.5);
    final radius = w * 0.34;
    const segmentCount = 10;
    final sweepPerSegment = (2 * math.pi) / segmentCount;
    final segmentSweep = sweepPerSegment * 0.65;
    final startAngle = -math.pi / 2;
    for (var i = 0; i < segmentCount; i++) {
      c.drawArc(
        Rect.fromCircle(center: center, radius: radius),
        startAngle + i * sweepPerSegment,
        segmentSweep,
        false,
        stroke,
      );
    }
    c.drawCircle(center, w * 0.05, dot);
  }

  // A minimal padlock — rounded body + arc shackle + keyhole via-dot — for
  // login/password events.
  void _auth(Canvas c, double w, Paint stroke, Paint dot) {
    final body = Rect.fromLTWH(w * 0.24, w * 0.44, w * 0.52, w * 0.4);
    c.drawRRect(RRect.fromRectAndRadius(body, Radius.circular(w * 0.06)), stroke);
    c.drawArc(
      Rect.fromLTWH(w * 0.3, w * 0.16, w * 0.4, w * 0.4),
      math.pi,
      math.pi,
      false,
      stroke,
    );
    c.drawCircle(Offset(w * 0.5, w * 0.64), w * 0.035, dot);
  }

  // A ring with a centered "i" (dot above, short line below) — info events.
  void _info(Canvas c, double w, Paint stroke, Paint dot) {
    c.drawCircle(Offset(w * 0.5, w * 0.5), w * 0.34, stroke);
    c.drawCircle(Offset(w * 0.5, w * 0.36), w * 0.035, dot);
    c.drawLine(Offset(w * 0.5, w * 0.46), Offset(w * 0.5, w * 0.66), stroke);
  }

  @override
  bool shouldRepaint(covariant _HGIconPainter oldDelegate) {
    return oldDelegate.icon != icon || oldDelegate.color != color;
  }
}
