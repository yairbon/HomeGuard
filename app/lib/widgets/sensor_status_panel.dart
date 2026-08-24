import 'package:flutter/material.dart';

import '../services/backend_service.dart';
import '../theme/app_theme.dart';
import 'homeguard_icons.dart';

/// Horizontal row of per-sensor status badges (icon + label + OK/BREACH/
/// BYPASSED state), one per entry in [sensors]. Used on the dashboard to
/// give an at-a-glance read of every sensor in the current loop.
class SensorStatusPanel extends StatelessWidget {
  const SensorStatusPanel({super.key, required this.sensors});

  /// Sensor readings to render, in display order.
  final List<SensorReading> sensors;

  /// Maps a sensor type to the icon used for its badge.
  HGIcon _iconFor(SensorType type) {
    switch (type) {
      case SensorType.ultrasonic:
        return HGIcon.ultrasonic;
      case SensorType.door:
        return HGIcon.door;
      case SensorType.vibration:
        return HGIcon.vibration;
    }
  }

  /// Builds one badge per sensor. Renders nothing (rather than an empty
  /// [Row]) when there are no sensors, so the panel doesn't leave behind
  /// stray layout space on screens where sensors haven't loaded yet.
  @override
  Widget build(BuildContext context) {
    if (sensors.isEmpty) return const SizedBox.shrink();

    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
      children: sensors.map((sensor) {
        // Bypassed takes priority over ok/breach: a bypassed sensor's raw
        // ok/breach reading is meaningless since it's been intentionally
        // excluded from monitoring, so it always renders muted regardless
        // of its underlying state.
        final color = sensor.bypassed
            ? AppColors.textMuted
            : (sensor.ok ? AppColors.safe : AppColors.alarm);

        return Column(
          children: [
            Container(
              width: 48,
              height: 48,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: color.withValues(alpha: 0.12),
                border: Border.all(color: color, width: 1.5),
              ),
              child: HomeGuardIcon(_iconFor(sensor.type), color: color, size: 22),
            ),
            const SizedBox(height: 8),
            Text(
              sensor.label,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 11, color: AppColors.textMuted),
            ),
            Text(
              sensor.bypassed ? 'BYPASSED' : (sensor.ok ? 'OK' : 'BREACH'),
              style: AppTheme.monoBadge.copyWith(color: color),
            ),
          ],
        );
      }).toList(),
    );
  }
}
