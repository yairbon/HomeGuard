import 'package:flutter/material.dart';

import '../services/backend_service.dart';
import '../theme/app_theme.dart';
import 'homeguard_icons.dart';

/// Maps an event category to the (icon, color) pair used to badge it,
/// shared by [EventTileCompact] and [EventTileCard] so both tile styles
/// stay visually consistent for the same category.
(HGIcon, Color) eventVisual(EventCategory category) {
  switch (category) {
    case EventCategory.alert:
      return (HGIcon.alert, AppColors.alarm);
    case EventCategory.armDisarm:
      return (HGIcon.armDisarm, AppColors.system);
    case EventCategory.auth:
      return (HGIcon.auth, AppColors.safe);
    case EventCategory.faceId:
      return (HGIcon.faces, AppColors.system);
    case EventCategory.info:
      return (HGIcon.info, AppColors.textMuted);
  }
}

/// Formats [timestamp] as a coarse "time ago" string, picking the largest
/// unit that keeps the number small (seconds, then minutes, hours, days).
/// Anything under 5 seconds collapses to "just now" rather than "0s ago"/
/// "1s ago" — events are often rendered moments after they occur, so
/// without this floor the label would flicker through tiny second counts
/// due to render/network latency instead of reading as immediate.
String relativeTime(DateTime timestamp) {
  final diff = DateTime.now().difference(timestamp);
  if (diff.inSeconds < 5) return 'just now';
  if (diff.inMinutes < 1) return '${diff.inSeconds}s ago';
  if (diff.inHours < 1) return '${diff.inMinutes}m ago';
  if (diff.inDays < 1) return '${diff.inHours}h ago';
  return '${diff.inDays}d ago';
}

/// Formats [t] as a zero-padded 24-hour `HH:MM:SS` clock string, used
/// alongside [relativeTime] on the full event card so an exact time is
/// still available even though the headline text is relative.
String clockTime(DateTime t) {
  final h = t.hour.toString().padLeft(2, '0');
  final m = t.minute.toString().padLeft(2, '0');
  final s = t.second.toString().padLeft(2, '0');
  return '$h:$m:$s';
}

/// Compact single-line form, used in the dashboard's recent-activity
/// preview.
class EventTileCompact extends StatelessWidget {
  const EventTileCompact({super.key, required this.event});

  /// The event this tile summarizes.
  final SecurityEvent event;

  /// Lays out a single row: category icon, message (truncated with an
  /// ellipsis so long messages don't wrap or push the timestamp off-screen
  /// in the compact preview), and the relative timestamp.
  @override
  Widget build(BuildContext context) {
    final (icon, color) = eventVisual(event.category);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: [
          HomeGuardIcon(icon, size: 16, color: color),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              event.message,
              style: const TextStyle(color: AppColors.textPrimary, fontSize: 13),
              overflow: TextOverflow.ellipsis,
            ),
          ),
          const SizedBox(width: 8),
          Text(
            relativeTime(event.timestamp),
            style: const TextStyle(color: AppColors.textMuted, fontSize: 11),
          ),
        ],
      ),
    );
  }
}

/// Full card form with an icon badge, used in the full event log screen.
class EventTileCard extends StatelessWidget {
  const EventTileCard({super.key, required this.event});

  /// The event this card describes.
  final SecurityEvent event;

  /// Lays out an icon badge, the event message, and a combined relative +
  /// exact timestamp. Alert-category events get a distinct outlined red
  /// border and bold red message text so they stand out from routine
  /// (arm/disarm, auth, info) entries when scanning the log.
  @override
  Widget build(BuildContext context) {
    final (icon, color) = eventVisual(event.category);
    final isAlert = event.category == EventCategory.alert;

    return Card(
      shape: isAlert
          ? RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12),
              side: BorderSide(color: color.withValues(alpha: 0.4)),
            )
          : null,
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Row(
          children: [
            Container(
              width: 36,
              height: 36,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: color.withValues(alpha: 0.12),
              ),
              child: HomeGuardIcon(icon, color: color, size: 18),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    event.message,
                    style: TextStyle(
                      color: isAlert ? AppColors.alarm : AppColors.textPrimary,
                      fontWeight: isAlert ? FontWeight.w600 : FontWeight.normal,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    '${relativeTime(event.timestamp)} · ${clockTime(event.timestamp)}',
                    style: AppTheme.monoStyle(fontSize: 11, color: AppColors.textMuted),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
