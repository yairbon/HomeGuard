import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../services/alarm_service.dart';
import '../services/backend_service.dart';
import '../theme/app_theme.dart';
import '../widgets/event_tile.dart';

/// Full-screen history of alarm events.
///
/// Shows a row of severity filter chips ("All" / "Critical" / "Warning" /
/// "Info") above a list of [EventTileCard]s. Events come from
/// [AlarmService] (via `context.watch`), and the chip row lets the user
/// narrow the list down to a single [EventSeverity] without leaving the
/// screen.
class EventLogScreen extends StatefulWidget {
  const EventLogScreen({super.key});

  @override
  State<EventLogScreen> createState() => _EventLogScreenState();
}

class _EventLogScreenState extends State<EventLogScreen> {
  EventSeverity? _filter; // null = All

  /// Builds the filter chip row and the (optionally filtered) event list.
  ///
  /// Both [allEvents] and the filtered [events] are computed here: the full
  /// list is kept around even when a filter is active so the empty-state
  /// text can distinguish "there are no events at all" from "there are
  /// events, just none matching this filter".
  @override
  Widget build(BuildContext context) {
    final allEvents = context.watch<AlarmService>().events;
    final events =
        _filter == null ? allEvents : allEvents.where((e) => e.severity == _filter).toList();

    return Scaffold(
      appBar: AppBar(title: const Text('EVENT LOG')),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
            child: Row(
              children: [
                _FilterChip(label: 'All', selected: _filter == null, onTap: () => setState(() => _filter = null)),
                const SizedBox(width: 8),
                _FilterChip(
                  label: 'Critical',
                  selected: _filter == EventSeverity.critical,
                  color: AppColors.alarm,
                  onTap: () => setState(() => _filter = EventSeverity.critical),
                ),
                const SizedBox(width: 8),
                _FilterChip(
                  label: 'Warning',
                  selected: _filter == EventSeverity.warning,
                  color: AppColors.transitional,
                  onTap: () => setState(() => _filter = EventSeverity.warning),
                ),
                const SizedBox(width: 8),
                _FilterChip(
                  label: 'Info',
                  selected: _filter == EventSeverity.info,
                  onTap: () => setState(() => _filter = EventSeverity.info),
                ),
              ],
            ),
          ),
          Expanded(
            child: events.isEmpty
                ? Center(
                    child: Text(
                      allEvents.isEmpty ? 'No events yet' : 'No events in this category',
                      style: TextStyle(color: AppColors.textMuted),
                    ),
                  )
                : ListView.separated(
                    padding: const EdgeInsets.all(16),
                    itemCount: events.length,
                    separatorBuilder: (_, _) => const SizedBox(height: 8),
                    itemBuilder: (context, index) => EventTileCard(event: events[index]),
                  ),
          ),
        ],
      ),
    );
  }
}

/// A single pill-shaped, tappable chip in the event log's filter row.
///
/// [color] is only used as the chip's fill when [selected] is true; it
/// defaults to [AppColors.system] because the "All" and "Info" chips don't
/// have a dedicated severity color of their own and fall back to the
/// neutral system color instead.
class _FilterChip extends StatelessWidget {
  const _FilterChip({
    required this.label,
    required this.selected,
    required this.onTap,
    this.color = AppColors.system,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;
  final Color color;

  /// Renders the chip filled with [color] when [selected], otherwise as a
  /// neutral raised surface; tapping invokes [onTap] so the parent screen
  /// can update its filter state.
  @override
  Widget build(BuildContext context) {
    return Material(
      color: selected ? color : AppColors.surfaceRaised,
      borderRadius: BorderRadius.circular(20),
      child: InkWell(
        borderRadius: BorderRadius.circular(20),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
          child: Text(
            label,
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: selected ? AppColors.background : AppColors.textMuted,
            ),
          ),
        ),
      ),
    );
  }
}
