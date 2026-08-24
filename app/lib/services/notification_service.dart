import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

/// Local (on-device) notifications for security alerts. This is NOT remote
/// push — it only fires while the app process is alive (foreground or
/// backgrounded), not when fully closed. Notifications that have to arrive
/// with the app closed come from the backend over ntfy instead; Firebase
/// Cloud Messaging was rejected (see CLAUDE.md).
class NotificationService {
  static final _plugin = FlutterLocalNotificationsPlugin();
  static bool _initialized = false;

  /// Set once the plugin has proven unusable, so a broken platform channel
  /// costs one failed attempt rather than one per alert.
  static bool _unavailable = false;

  /// Initializes the underlying plugin (Android notification channel setup
  /// + runtime permission request) once per app run. Safe to call more
  /// than once — guarded by [_initialized] so repeated calls (e.g. from
  /// both `main()` and a lazy call inside [showAlert]) are no-ops after
  /// the first. Skipped on web, which this plugin doesn't support.
  static Future<void> init() async {
    if (_initialized || _unavailable || kIsWeb) return; // no web support
    try {
      const androidInit = AndroidInitializationSettings('@mipmap/ic_launcher');
      await _plugin.initialize(const InitializationSettings(android: androidInit));
      await _plugin
          .resolvePlatformSpecificImplementation<
              AndroidFlutterLocalNotificationsPlugin>()
          ?.requestNotificationsPermission();
      _initialized = true;
    } catch (error) {
      // No platform implementation registered — a widget test, or a host
      // this plugin doesn't support. Recorded and skipped rather than
      // rethrown; see showAlert for why throwing here is dangerous.
      _unavailable = true;
      debugPrint('Local notifications unavailable: $error');
    }
  }

  /// Shows a local notification with [message] as the body (title is
  /// fixed to "HomeGuard alert"). Lazily calls [init] if it hasn't run
  /// yet, so callers don't need to sequence their own call to it first.
  /// The notification id is derived from the current Unix timestamp (in
  /// seconds) so consecutive alerts get distinct ids and each shows as
  /// its own notification instead of overwriting the previous one.
  ///
  /// Every failure path is swallowed, deliberately. This is called from
  /// inside `AlarmService`'s stream listeners, and an exception thrown there
  /// does not reach the subscription's `onError` — it escapes into the zone,
  /// kills nothing visibly, and leaves the listener in an unknown state. The
  /// alarm state update is what actually matters; a notification that failed
  /// to display must never be able to take it down with it. (This is the same
  /// rule the backend applies to ntfy: `notify()` swallows everything so a
  /// push can't stall or break the alert path.)
  ///
  /// It is also what makes the entry-delay and triggered paths reachable from
  /// a widget test at all — there is no notification platform in the test
  /// binding, so this used to throw a LateInitializationError out of the
  /// listener on the first alarm.
  static Future<void> showAlert(String message) async {
    if (kIsWeb) return;
    if (!_initialized) await init();
    if (!_initialized) return; // init failed; nothing to show on
    const androidDetails = AndroidNotificationDetails(
      'homeguard_alerts',
      'Security alerts',
      channelDescription: 'Intrusion and system alerts from HomeGuard',
      importance: Importance.max,
      priority: Priority.high,
    );
    try {
      await _plugin.show(
        DateTime.now().millisecondsSinceEpoch ~/ 1000,
        'HomeGuard alert',
        message,
        const NotificationDetails(android: androidDetails),
      );
    } catch (error) {
      debugPrint('Could not show the alert notification: $error');
    }
  }
}
