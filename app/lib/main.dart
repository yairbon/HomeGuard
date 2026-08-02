import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'screens/login_screen.dart';
import 'screens/main_shell.dart';
import 'services/alarm_service.dart';
import 'services/auth_service.dart';
import 'services/backend_service.dart';
import 'services/http_backend_service.dart';
import 'services/notification_service.dart';
import 'theme/app_theme.dart';

/// App entry point.
///
/// [WidgetsFlutterBinding.ensureInitialized] is required before
/// [NotificationService.init], since that call reaches across a platform
/// channel to native code and platform channels aren't available until the
/// binding exists. Local notifications are set up before [runApp] so the
/// service is ready to fire before any UI (and any alarm event that might
/// trigger a notification) can appear.
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  NotificationService.init();
  runApp(const HomeGuardApp());
}

/// Root widget of the app.
///
/// Sets up the app-wide [Provider]/[ChangeNotifierProvider] tree (backend,
/// auth, alarm) above a [MaterialApp] whose `home` is [_RootGate], which in
/// turn picks between [LoginScreen] and [MainShell] based on auth state.
class HomeGuardApp extends StatelessWidget {
  /// Creates the app. [backend] is normally left null so [build] falls back
  /// to a real [HttpBackendService]; tests pass a mock/fake here instead so
  /// they don't hit a real network connection that won't exist in a test run.
  const HomeGuardApp({super.key, this.backend});

  /// Backend implementation to inject, or null to use the default
  /// [HttpBackendService] pointed at the local dev server. See [backend]'s
  /// use in [build] for the injection point.
  final BackendService? backend;

  /// Builds the [MultiProvider] tree and the [MaterialApp] shell.
  @override
  Widget build(BuildContext context) {
    return MultiProvider(
      providers: [
        Provider<BackendService>(
          // localhost works for the emulator/desktop/web targets, since
          // the backend runs on that same machine. On a real Android
          // device, localhost means the phone itself, not your PC — the
          // LAN-IP approach (backend machine's real WiFi IP) was tried
          // here but this specific network setup couldn't route
          // phone-to-PC traffic (confirmed with a raw TCP test from the
          // phone — connection just timed out, no response at all,
          // pointing at router-level client isolation or a firewall layer
          // outside Windows' own). `adb reverse tcp:8000 tcp:8000` (run
          // BEFORE launching the app, so the very first connection
          // attempt succeeds instead of racing the tunnel setup) tunnels
          // the phone's own localhost:8000 to this PC over the USB
          // debugging connection instead — works regardless of WiFi
          // topology.
          create: (_) => backend ?? HttpBackendService(baseUrl: 'http://localhost:8000'),
          dispose: (_, backend) => backend.dispose(),
        ),
        ChangeNotifierProvider<AuthService>(
          create: (context) => AuthService(context.read<BackendService>()),
          // Eager like AlarmService below, for consistency between the two
          // top-level services. Unlike AlarmService, AuthService's
          // constructor does no async/stream setup, so this isn't strictly
          // required for correctness today — but it keeps both providers'
          // lifecycles predictable if that ever changes.
          lazy: false,
        ),
        ChangeNotifierProvider<AlarmService>(
          // Must be eager: AlarmService's constructor subscribes to the
          // backend's broadcast streams, which don't replay past events to
          // late subscribers. Lazy construction meant it didn't start
          // listening until HomeScreen first watched it — by which point
          // the very first login's initial sensor readings and "Logged in"
          // event had already been emitted into the void.
          create: (context) => AlarmService(context.read<BackendService>()),
          lazy: false,
        ),
      ],
      child: MaterialApp(
        title: 'HomeGuard',
        debugShowCheckedModeBanner: false,
        theme: AppTheme.dark,
        home: const _RootGate(),
      ),
    );
  }
}

/// Watches [AuthService.isAuthenticated] and swaps between [LoginScreen]
/// and [MainShell] accordingly, animating the swap instead of cutting
/// instantly between the two screens.
class _RootGate extends StatelessWidget {
  const _RootGate();

  /// Rebuilds whenever [AuthService] notifies (via [context.watch]) and
  /// renders whichever screen matches the current auth state inside an
  /// [AnimatedSwitcher].
  ///
  /// The transition is a combined fade + gentle upward slide (12% of the
  /// widget's own height via the 0.03 offset factor, over 350ms) rather than
  /// AnimatedSwitcher's default scale-fade, to feel like the next screen
  /// settling into place instead of just materializing.
  ///
  /// The distinct `ValueKey`s on the two children are load-bearing:
  /// AnimatedSwitcher decides "this is a new child, animate the swap" by
  /// comparing the outgoing and incoming widgets' `Key`s (falling back to
  /// type), so without unique keys here a login->home switch wouldn't be
  /// recognized as a change and would just snap.
  @override
  Widget build(BuildContext context) {
    final isAuthenticated = context.watch<AuthService>().isAuthenticated;
    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 350),
      transitionBuilder: (child, animation) => FadeTransition(
        opacity: animation,
        child: SlideTransition(
          position: Tween<Offset>(begin: const Offset(0, 0.03), end: Offset.zero)
              .animate(animation),
          child: child,
        ),
      ),
      child: isAuthenticated
          ? const MainShell(key: ValueKey('home'))
          : const LoginScreen(key: ValueKey('login')),
    );
  }
}
