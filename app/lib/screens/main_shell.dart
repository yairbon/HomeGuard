import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../services/alarm_service.dart';
import '../theme/app_theme.dart';
import '../widgets/homeguard_icons.dart';
import 'camera_screen.dart';
import 'event_log_screen.dart';
import 'faces_screen.dart';
import 'home_screen.dart';
import 'settings_screen.dart';

/// Post-login app shell: hosts the five main tabs (Dashboard, Camera,
/// Faces, Events, Settings) behind a bottom [NavigationBar].
///
/// This is what [_RootGate] in `main.dart` swaps in once
/// `AuthService.isAuthenticated` is true.
class MainShell extends StatefulWidget {
  const MainShell({super.key});

  @override
  State<MainShell> createState() => _MainShellState();
}

/// State for [MainShell]. Tracks which tab is selected and renders the
/// corresponding screen plus the bottom navigation bar.
class _MainShellState extends State<MainShell> {
  /// Index of the currently selected tab, in the same order as [_screens]
  /// and the `destinations` list in [build].
  int _index = 0;

  // Built once and kept alive for the lifetime of this state (rather than
  // constructed per-build) so IndexedStack below can preserve each screen's
  // state — scroll position, in-progress camera feed, etc. — when the user
  // switches tabs, instead of tearing it down and rebuilding from scratch.
  static const _screens = [
    HomeScreen(),
    CameraScreen(),
    FacesScreen(),
    EventLogScreen(),
    SettingsScreen(),
  ];

  /// One (icon, label) pair per tab, in the same order as [_screens] — kept
  /// as data rather than inlined in [build] so the selected-color logic
  /// below can be written once and mapped over this list.
  static const _tabs = [
    (HGIcon.dashboard, 'Dashboard'),
    (HGIcon.camera, 'Camera'),
    (HGIcon.faces, 'Faces'),
    (HGIcon.events, 'Events'),
    (HGIcon.settings, 'Settings'),
  ];

  /// Renders the currently selected screen inside an [IndexedStack] (all
  /// five screens stay mounted; only the visible one changes) under a
  /// [NavigationBar] for switching tabs.
  ///
  /// [HomeGuardIcon] takes its color as an explicit parameter rather than
  /// reading it from [IconTheme] (unlike the stock [Icon] widget it
  /// replaces), so the selected/unselected accent color that used to come
  /// from `NavigationBarThemeData.iconTheme` is now computed per-tab here
  /// instead.
  ///
  /// Also the single place infrastructural failures surface. [AlarmService]
  /// holds the most recent one as [AlarmService.lastError]; this shows it as
  /// a SnackBar and immediately clears it, so the same message can't be
  /// re-shown on every unrelated rebuild. It's done here rather than per
  /// screen because these failures aren't tied to whichever tab is open — a
  /// disarm that never reached the backend matters regardless of where the
  /// user happens to be looking.
  ///
  /// The dispatch is deferred to a post-frame callback because showing a
  /// SnackBar mutates the ScaffoldMessenger, which cannot be done during a
  /// build.
  @override
  Widget build(BuildContext context) {
    final error = context.select<AlarmService, String?>((a) => a.lastError);
    if (error != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(
            SnackBar(
              content: Text(error),
              backgroundColor: AppColors.alarm,
              behavior: SnackBarBehavior.floating,
              duration: const Duration(seconds: 5),
            ),
          );
        context.read<AlarmService>().clearError();
      });
    }

    return Scaffold(
      body: IndexedStack(index: _index, children: _screens),
      bottomNavigationBar: NavigationBarTheme(
        data: NavigationBarThemeData(
          backgroundColor: AppColors.surface,
          indicatorColor: AppColors.system.withValues(alpha: 0.18),
          labelTextStyle: WidgetStateProperty.resolveWith((states) {
            final selected = states.contains(WidgetState.selected);
            return TextStyle(
              fontSize: 11,
              color: selected ? AppColors.system : AppColors.textMuted,
            );
          }),
        ),
        child: NavigationBar(
          selectedIndex: _index,
          onDestinationSelected: (i) => setState(() => _index = i),
          destinations: [
            for (var i = 0; i < _tabs.length; i++)
              NavigationDestination(
                icon: HomeGuardIcon(
                  _tabs[i].$1,
                  size: 22,
                  color: i == _index ? AppColors.system : AppColors.textMuted,
                ),
                label: _tabs[i].$2,
              ),
          ],
        ),
      ),
    );
  }
}
