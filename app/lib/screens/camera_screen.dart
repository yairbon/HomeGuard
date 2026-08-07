import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../services/alarm_service.dart';
import '../theme/app_theme.dart';
import '../widgets/event_tile.dart';
import '../widgets/homeguard_icons.dart';

/// Camera tab: shows the (placeholder) front-entrance feed, current Face ID
/// and camera-online status, and the most recent face-identification
/// events.
///
/// There is no real video stream wired up — [_FeedPlaceholder] just shows a
/// static icon plus a live/offline badge driven by [AlarmService].
class CameraScreen extends StatelessWidget {
  const CameraScreen({super.key});

  /// Builds the camera screen: feed placeholder, status cards, and the
  /// recent-identifications list.
  ///
  /// Only the 5 most recent face-ID events are shown here (see
  /// [recentIds]) so this tab stays a quick-glance summary; the full
  /// history is available in the event log screen.
  @override
  Widget build(BuildContext context) {
    final alarm = context.watch<AlarmService>();
    final recentIds = alarm.faceIdEvents.take(5).toList();

    return Scaffold(
      appBar: AppBar(title: const Text('CAMERA')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          _FeedPlaceholder(isOnline: alarm.isCameraOnline),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: _StatusCard(
                  icon: HGIcon.faces,
                  label: 'FACE ID',
                  value: alarm.faceIdEnabled ? 'ENABLED' : 'DISABLED',
                  color: alarm.faceIdEnabled ? AppColors.safe : AppColors.textMuted,
                  // Face ID is a user-controlled setting, so this card is
                  // tappable and toggles it directly.
                  onTap: () => context.read<AlarmService>().setFaceIdEnabled(!alarm.faceIdEnabled),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _StatusCard(
                  icon: HGIcon.camera,
                  label: 'CAMERA',
                  value: alarm.isCameraOnline ? 'STREAMING' : 'OFFLINE',
                  color: alarm.isCameraOnline ? AppColors.safe : AppColors.alarm,
                  // No onTap: unlike Face ID, camera online/offline reflects
                  // actual hardware connectivity and isn't user-toggleable.
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
          _FaceDisarmToggle(
            enabled: alarm.faceDisarmEnabled,
            onChanged: (value) => context.read<AlarmService>().setFaceDisarmEnabled(value),
          ),
          const SizedBox(height: 24),
          Text(
            'RECENT IDENTIFICATIONS',
            style: AppTheme.displayStyle(
              fontSize: 14,
              fontWeight: FontWeight.w600,
              color: AppColors.textMuted,
              letterSpacing: 1.5,
            ),
          ),
          const SizedBox(height: 8),
          if (recentIds.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 16),
              child: Text('No identifications yet', style: TextStyle(color: AppColors.textMuted)),
            )
          else
            ...recentIds.map((e) => EventTileCompact(event: e)),
          const SizedBox(height: 16),
          Center(
            child: TextButton(
              onPressed: () => context.read<AlarmService>().simulateFaceRecognition(),
              child: Text(
                'Simulate recognition (demo)',
                style: TextStyle(color: AppColors.textMuted, fontSize: 12),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Opt-in switch for letting a recognized face disarm the system.
///
/// Given a full card with an explicit warning rather than a plain row,
/// because this is the one setting in the app that *lowers* what it takes to
/// turn the alarm off. The recognizer compares face embeddings and has no
/// liveness detection, so a printed photo or a picture on a phone screen may
/// pass — that makes this convenience, not security, and the UI has to say
/// so rather than let someone enable it assuming otherwise.
class _FaceDisarmToggle extends StatelessWidget {
  const _FaceDisarmToggle({required this.enabled, required this.onChanged});

  final bool enabled;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: AppColors.surfaceRaised,
      borderRadius: BorderRadius.circular(10),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 6, 14, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              value: enabled,
              activeThumbColor: AppColors.system,
              onChanged: onChanged,
              title: const Text(
                'Disarm with Face ID',
                style: TextStyle(color: AppColors.textPrimary, fontSize: 14),
              ),
              subtitle: Text(
                'A recognized face turns the alarm off',
                style: TextStyle(color: AppColors.textMuted, fontSize: 12),
              ),
            ),
            Text(
              'No liveness check — a photo of an approved face may be enough. '
              'Treat this as convenience, not security.',
              style: TextStyle(color: AppColors.transitional, fontSize: 11),
            ),
          ],
        ),
      ),
    );
  }
}

/// Stand-in for the live camera feed, since no real video stream is wired
/// up: a bordered box with a camera icon, a LIVE/OFFLINE status badge, and
/// a caption naming the camera and its connection state.
class _FeedPlaceholder extends StatelessWidget {
  const _FeedPlaceholder({required this.isOnline});

  final bool isOnline;

  /// Renders the placeholder box, switching its badge and caption text
  /// based on [isOnline].
  @override
  Widget build(BuildContext context) {
    return AspectRatio(
      aspectRatio: 4 / 3,
      child: Container(
        decoration: BoxDecoration(
          color: AppColors.surface,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: AppColors.surfaceRaised),
        ),
        child: Stack(
          children: [
            Center(
              child: HomeGuardIcon(
                HGIcon.camera,
                size: 48,
                color: AppColors.textMuted.withValues(alpha: 0.4),
              ),
            ),
            Positioned(
              top: 10,
              right: 10,
              child: _Badge(
                text: isOnline ? 'LIVE' : 'OFFLINE',
                color: isOnline ? AppColors.alarm : AppColors.textMuted,
                dot: isOnline,
              ),
            ),
            Positioned(
              bottom: 10,
              left: 10,
              right: 10,
              child: Text(
                isOnline ? 'ESP32-CAM · front entrance' : 'ESP32-CAM · waiting for connection',
                style: AppTheme.monoStyle(fontSize: 11, color: AppColors.textMuted),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Small pill-shaped badge (e.g. "LIVE" / "OFFLINE") overlaid on the feed
/// placeholder, with an optional colored dot when [dot] is true.
class _Badge extends StatelessWidget {
  const _Badge({required this.text, required this.color, this.dot = false});

  final String text;
  final Color color;
  final bool dot;

  /// Renders [text] in [color], preceded by a colored dot when [dot] is
  /// true.
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: AppColors.background.withValues(alpha: 0.8),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (dot) ...[
            Container(
              width: 6,
              height: 6,
              decoration: BoxDecoration(shape: BoxShape.circle, color: color),
            ),
            const SizedBox(width: 5),
          ],
          Text(text, style: AppTheme.monoStyle(fontSize: 10, color: color, letterSpacing: 1)),
        ],
      ),
    );
  }
}

/// Reusable status tile showing an icon, a label (e.g. "FACE ID"), and a
/// value (e.g. "ENABLED") in [color]. [onTap] is optional because not
/// every status shown via this widget is user-toggleable (compare the
/// Face ID and Camera cards in [CameraScreen]).
class _StatusCard extends StatelessWidget {
  const _StatusCard({
    required this.icon,
    required this.label,
    required this.value,
    required this.color,
    this.onTap,
  });

  final HGIcon icon;
  final String label;
  final String value;
  final Color color;
  final VoidCallback? onTap;

  /// Renders the icon/label/value row; tappable only if [onTap] is
  /// provided.
  @override
  Widget build(BuildContext context) {
    return Material(
      color: AppColors.surfaceRaised,
      borderRadius: BorderRadius.circular(10),
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Row(
            children: [
              HomeGuardIcon(icon, color: color, size: 20),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(label, style: TextStyle(fontSize: 11, color: AppColors.textMuted)),
                    Text(
                      value,
                      style: AppTheme.monoStyle(fontSize: 12, color: color, letterSpacing: 0.5),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
