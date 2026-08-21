import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../services/alarm_service.dart';
import '../services/backend_service.dart';
import '../theme/app_theme.dart';
import '../widgets/alarm_status_indicator.dart';
import '../widgets/event_tile.dart';
import '../widgets/homeguard_icons.dart';
import '../widgets/sensor_status_panel.dart';
import 'event_log_screen.dart';

/// The "Dashboard" tab: the app's main at-a-glance screen. Shows the current
/// alarm state, live sensor status, arm/disarm controls, and a preview of
/// recent activity, all driven by the watched [AlarmService].
class HomeScreen extends StatelessWidget {
  const HomeScreen({super.key});

  /// Builds the dashboard layout. The arm/disarm control area switches
  /// between a single DISARM button and a pair of arm-mode cards depending
  /// on [canDisarm]/[canArm] (see the `canDisarm ? ... : ...` branch below).
  @override
  Widget build(BuildContext context) {
    final alarm = context.watch<AlarmService>();
    // The system only has two logical states for this UI's purposes: fully
    // disarmed (armable) or anything else (armed/triggered — disarmable).
    // canArm and canDisarm are deliberately kept as separate named booleans
    // (rather than negating one inline) since they gate two different UI
    // branches below and read more clearly at each call site.
    final canArm = alarm.state == AlarmState.disarmed;
    final canDisarm = alarm.state != AlarmState.disarmed;
    // Read once; it used to be evaluated twice in this build (and recomputed
    // by filtering the whole board list each time).
    final offlineBoards = alarm.offlineBoards;
    // Everything below the app bar is a readout of what the backend last
    // said. Without a live link that's history, not status, so it gets said
    // out loud and shown dimmed rather than presented as current.
    final linked = alarm.isBackendLinked;

    return Scaffold(
      appBar: AppBar(
        title: const Text('HOMEGUARD'),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: Center(
              child: _ConnectivityBadge(isOnline: alarm.isOnline, linked: linked),
            ),
          ),
        ],
      ),
      body: SafeArea(
        child: SingleChildScrollView(
          // One consistent horizontal inset for the whole screen — the
          // hero ring, the control panel card, and the activity list all
          // align to the same edge instead of each picking their own
          // padding, which is what made the old layout feel like several
          // separately-designed sections stacked together rather than one
          // screen.
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
          child: Column(
            children: [
              if (!linked) ...[
                const _NoLinkBanner(),
                const SizedBox(height: 18),
              ],
              // Dimmed together as one block: the ring and the headline are
              // the two things a glance actually lands on, and when they're
              // stale they should look it. The controls below stay at full
              // strength — a disarm attempt with no link fails loudly (see
              // AlarmService.lastError) rather than silently, so there's no
              // reason to discourage trying.
              Opacity(
                opacity: linked ? 1 : 0.45,
                child: Column(
                  children: [
                    Center(
                      child: AlarmStatusIndicator(
                        state: alarm.state,
                        sensors: alarm.sensors,
                        armingSecondsRemaining: alarm.armingSecondsRemaining,
                      ),
                    ),
                    const SizedBox(height: 18),
                    _StatusHeadline(
                      state: alarm.state,
                      armingSecondsRemaining: alarm.armingSecondsRemaining,
                      entryDelaySecondsRemaining: alarm.entryDelaySecondsRemaining,
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 28),
              // A single control-panel card groups sensor status and the
              // arm/disarm control together — they're one decision ("what's
              // the system's loop doing, and what do I do about it"), so
              // they read as one unit instead of two floating rows.
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(20),
                decoration: BoxDecoration(
                  color: AppColors.surfaceRaised,
                  borderRadius: BorderRadius.circular(14),
                ),
                child: Column(
                  children: [
                    SensorStatusPanel(sensors: alarm.sensors),
                    const SizedBox(height: 20),
                    // Primary call-to-action: a single full-width red
                    // DISARM button whenever the system is armed or
                    // triggered, otherwise a side-by-side pair of arm-mode
                    // cards (HOME vs AWAY) so the user picks how to arm
                    // rather than jumping straight to one default mode.
                    canDisarm
                        ? SizedBox(
                            width: double.infinity,
                            child: ElevatedButton(
                              style: ElevatedButton.styleFrom(
                                backgroundColor: AppColors.alarm,
                                foregroundColor: Colors.white,
                              ),
                              onPressed: alarm.disarm,
                              child: const Text('DISARM'),
                            ),
                          )
                        : Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Expanded(
                                child: _ArmModeCard(
                                  icon: HGIcon.armHome,
                                  title: 'ARM HOME',
                                  subtitle: 'Perimeter only — move around inside',
                                  onTap: canArm ? () => alarm.arm(ArmMode.home) : null,
                                ),
                              ),
                              const SizedBox(width: 12),
                              Expanded(
                                child: _ArmModeCard(
                                  icon: HGIcon.armAway,
                                  title: 'ARM AWAY',
                                  subtitle: 'Every sensor — nobody home',
                                  onTap: canArm ? () => alarm.arm(ArmMode.away) : null,
                                  // ARM AWAY is the more commonly used mode
                                  // (whole house covered), so it gets the
                                  // filled/accent styling to draw the eye,
                                  // while ARM HOME stays in the plain card
                                  // style.
                                  highlighted: true,
                                ),
                              ),
                            ],
                          ),
                  ],
                ),
              ),
              // Surfaced above recent activity, not buried in it: a board
              // that stopped reporting is a hole in the system's coverage,
              // and the previous single ONLINE badge could never show it —
              // it latched on after the first heartbeat and stayed on even
              // when a board lost power.
              if (offlineBoards.isNotEmpty) ...[
                const SizedBox(height: 20),
                _OfflineBoardsBanner(boards: offlineBoards),
              ],
              const SizedBox(height: 28),
              _RecentEvents(events: alarm.events),
              // Demo-only escape hatch: lets anyone see the full alert flow
              // (notification, state change, event log entry) without real
              // sensor hardware attached. Only offered while armed (not
              // already triggered) since triggering an already-triggered
              // alarm again wouldn't demonstrate anything new. See
              // AlarmService.simulateIntrusion for what it actually does.
              if (alarm.state == AlarmState.armed)
                TextButton(
                  onPressed: alarm.simulateIntrusion,
                  child: Text(
                    'Simulate intrusion (demo)',
                    style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Warning strip naming the ESP32 boards the backend has stopped hearing
/// from.
///
/// Deliberately styled as a warning rather than an error: the system is still
/// running and the other boards still report, but the named ones are no
/// longer covering anything — which is exactly the failure an alarm system
/// must not hide. Only rendered when at least one board is offline, so a
/// healthy system shows nothing here.
class _OfflineBoardsBanner extends StatelessWidget {
  const _OfflineBoardsBanner({required this.boards});

  final List<BoardStatus> boards;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      decoration: BoxDecoration(
        color: AppColors.transitional.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppColors.transitional.withValues(alpha: 0.4)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          HomeGuardIcon(HGIcon.armAway, size: 18, color: AppColors.transitional),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  boards.length == 1 ? 'SENSOR OFFLINE' : 'SENSORS OFFLINE',
                  style: AppTheme.displayBanner.copyWith(color: AppColors.transitional),
                ),
                const SizedBox(height: 4),
                Text(
                  '${boards.map((b) => b.label).join(', ')} '
                  '${boards.length == 1 ? 'has' : 'have'} stopped reporting',
                  style: AppTheme.monoDetail.copyWith(color: AppColors.textMuted),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Large, high-contrast status announcement shown below the ring.
///
/// The ring communicates state visually (closed/broken loop, color,
/// pulse); this headline says it in words, at a size and contrast that
/// actually reads at a glance — a pill-badge word plus a one-line
/// subtitle, instead of a small caption squeezed inside the ring's own
/// bounding box where it used to compete with the glow for contrast.
class _StatusHeadline extends StatelessWidget {
  const _StatusHeadline({
    required this.state,
    required this.armingSecondsRemaining,
    required this.entryDelaySecondsRemaining,
  });

  final AlarmState state;
  final int armingSecondsRemaining;
  final int entryDelaySecondsRemaining;

  /// One-line plain-language explanation of [state], shown under the
  /// badge. The transitional case is dynamic — it counts down alongside
  /// the ring's own countdown digit rather than just saying "arming".
  String get _subtitle {
    switch (state) {
      case AlarmState.disarmed:
        return 'Not monitoring';
      case AlarmState.armed:
        return 'Monitoring for intrusion';
      case AlarmState.triggered:
        return 'Intrusion detected';
      case AlarmState.transitional:
        return armingSecondsRemaining > 0
            ? 'Exiting — arms in ${armingSecondsRemaining}s'
            : 'Exiting now';
      case AlarmState.entryDelay:
        // The most time-critical line in the app: somebody just opened the
        // door on an armed system and this is the countdown they're racing.
        return entryDelaySecondsRemaining > 0
            ? 'Door opened — alarm in ${entryDelaySecondsRemaining}s'
            : 'Door opened — enter your code';
    }
  }

  /// Deliberately not a rounded "status chip" pill — that shape is the
  /// generic dark-dashboard status-badge component almost verbatim, and a
  /// design review called it out as reinforcing the generic read rather
  /// than escaping it. This is a plain, large, letter-spaced headline in
  /// the display face plus a short underline rule (a nameplate, not a
  /// chip), so the single most consequential word in the app finally gets
  /// real typographic priority instead of a bigger version of the eyebrow
  /// recipe used for muted list headers.
  @override
  Widget build(BuildContext context) {
    final color = alarmStateColor(state);
    return Column(
      children: [
        Text(
          alarmStateLabel(state),
          style: AppTheme.displayHeadline.copyWith(color: color),
        ),
        const SizedBox(height: 6),
        Container(width: 36, height: 3, color: color),
        const SizedBox(height: 10),
        Text(_subtitle, style: TextStyle(color: AppColors.textMuted, fontSize: 13)),
      ],
    );
  }
}

/// One tappable option in the arm-mode picker on [HomeScreen] (e.g. "ARM
/// HOME" or "ARM AWAY") — an icon, a title, and a short explanatory
/// subtitle over a card background.
class _ArmModeCard extends StatelessWidget {
  const _ArmModeCard({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.onTap,
    this.highlighted = false,
  });

  /// Icon shown at the top of the card.
  final HGIcon icon;

  /// Short mode name, e.g. "ARM HOME".
  final String title;

  /// One-line explanation of what the mode covers.
  final String subtitle;

  /// Tap handler that triggers this arm mode, or null to render the card
  /// disabled (dimmed, non-interactive) — passed as null by [HomeScreen]
  /// when the system can't currently be armed.
  final VoidCallback? onTap;

  /// Whether to use the filled/accent-colored variant (used for the
  /// preferred/primary mode) instead of the plain surface-colored card.
  final bool highlighted;

  /// Renders the card, dimming it via [Opacity] when [onTap] is null and
  /// swapping in accent colors when [highlighted] is true.
  @override
  Widget build(BuildContext context) {
    final disabled = onTap == null;
    final fg = highlighted ? AppColors.background : AppColors.textPrimary;
    final bg = highlighted ? AppColors.system : AppColors.surfaceRaised;

    return Opacity(
      opacity: disabled ? 0.4 : 1,
      child: Material(
        color: bg,
        borderRadius: BorderRadius.circular(10),
        child: InkWell(
          borderRadius: BorderRadius.circular(10),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                HomeGuardIcon(icon, color: fg, size: 20),
                const SizedBox(height: 8),
                Text(
                  title,
                  style: AppTheme.displayCardTitle.copyWith(color: fg),
                ),
                const SizedBox(height: 4),
                Text(
                  subtitle,
                  style: TextStyle(
                    fontSize: 11,
                    color: highlighted ? AppColors.background.withValues(alpha: 0.7) : AppColors.textMuted,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Strip across the top of the dashboard for when the app has lost its live
/// link to the backend.
///
/// Deliberately a full-width, worded statement rather than a subtler cue: the
/// failure it describes is invisible by nature. Every number on the screen
/// still looks perfectly plausible — it just stopped changing — so the only
/// thing that can distinguish "the house is quiet" from "the app went deaf"
/// is the app saying which one it is.
class _NoLinkBanner extends StatelessWidget {
  const _NoLinkBanner();

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      decoration: BoxDecoration(
        color: AppColors.transitional.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppColors.transitional.withValues(alpha: 0.4)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          HomeGuardIcon(HGIcon.info, size: 18, color: AppColors.transitional),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'NO LIVE CONNECTION',
                  style: AppTheme.displayBanner.copyWith(color: AppColors.transitional),
                ),
                const SizedBox(height: 4),
                Text(
                  'Showing the last state received — retrying',
                  style: AppTheme.monoDetail.copyWith(color: AppColors.textMuted),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Small AppBar indicator (dot + status word) for the system's connectivity.
///
/// Three states, not two. [linked] is whether *this app* is hearing the
/// backend; [isOnline] is the backend's own view of whether any ESP32 board
/// is reporting. Only the second one used to be shown, which meant a dropped
/// WebSocket left the last-received `esp32_online: true` on screen as a green
/// ONLINE dot for as long as the reconnects kept failing — the dashboard
/// frozen underneath it, and nothing anywhere saying so.
class _ConnectivityBadge extends StatelessWidget {
  const _ConnectivityBadge({required this.isOnline, required this.linked});

  /// The backend's report on the ESP32 boards. Meaningless while [linked] is
  /// false, since it can only be as fresh as the last frame received.
  final bool isOnline;

  /// Whether the app currently has a live link to the backend.
  final bool linked;

  /// Renders the dot + label pair: amber and hollow when there's no link at
  /// all, then safe-green/alarm-red for the boards' own status.
  @override
  Widget build(BuildContext context) {
    final color = !linked
        ? AppColors.transitional
        : (isOnline ? AppColors.safe : AppColors.alarm);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 8,
          height: 8,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            // Hollow while unlinked, so the difference reads even in a
            // grayscale screenshot or to someone who can't separate the amber
            // from the green.
            color: linked ? color : Colors.transparent,
            border: linked ? null : Border.all(color: color, width: 1.5),
          ),
        ),
        const SizedBox(width: 6),
        Text(
          !linked ? 'NO LINK' : (isOnline ? 'ONLINE' : 'OFFLINE'),
          style: AppTheme.monoBadge.copyWith(color: color),
        ),
      ],
    );
  }
}

/// Dashboard preview of the most recent activity, with a link through to
/// the full [EventLogScreen].
class _RecentEvents extends StatelessWidget {
  const _RecentEvents({required this.events});

  /// Full event list (newest first, per [AlarmService.events]); only the
  /// first few are actually shown here — see [build].
  final List<SecurityEvent> events;

  /// Shows up to the 3 most recent events (or an empty-state message when
  /// there are none) under a header with a "View all" button that opens
  /// [EventLogScreen] for the complete list.
  @override
  Widget build(BuildContext context) {
    // Only a short preview belongs on the dashboard; the full history lives
    // one tap away in EventLogScreen via the "View all" button below.
    final preview = events.take(3).toList();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(
              'RECENT ACTIVITY',
              style: AppTheme.displayEyebrow.copyWith(color: AppColors.textMuted),
            ),
            TextButton(
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => const EventLogScreen()),
              ),
              child: const Text('View all'),
            ),
          ],
        ),
        if (preview.isEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 16),
            child: Text('No activity yet', style: TextStyle(color: AppColors.textMuted)),
          )
        else
          ...preview.map((event) => EventTileCompact(event: event)),
      ],
    );
  }
}
