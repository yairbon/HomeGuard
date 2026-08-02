import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

/// Local (on-device) notifications for security alerts. This is NOT remote
/// push — it only fires while the app process is alive (foreground or
/// backgrounded), not when fully closed. Wiring up real Firebase Cloud
/// Messaging is a separate follow-up that needs a Firebase project (see
/// CLAUDE.md).
class NotificationService {
  static final _plugin = FlutterLocalNotificationsPlugin();
  static bool _initialized = false;

  /// Initializes the underlying plugin (Android notification channel setup
  /// + runtime permission request) once per app run. Safe to call more
  /// than once — guarded by [_initialized] so repeated calls (e.g. from
  /// both `main()` and a lazy call inside [showAlert]) are no-ops after
  /// the first. Skipped on web, which this plugin doesn't support.
  static Future<void> init() async {
    if (_initialized || kIsWeb) return; // no local-notifications support on web
    const androidInit = AndroidInitializationSettings('@mipmap/ic_launcher');
    await _plugin.initialize(const InitializationSettings(android: androidInit));
    await _plugin
        .resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin>()
        ?.requestNotificationsPermission();
    _initialized = true;
  }

  /// Shows a local notification with [message] as the body (title is
  /// fixed to "HomeGuard alert"). Lazily calls [init] if it hasn't run
  /// yet, so callers don't need to sequence their own call to it first.
  /// The notification id is derived from the current Unix timestamp (in
  /// seconds) so consecutive alerts get distinct ids and each shows as
  /// its own notification instead of overwriting the previous one.
  static Future<void> showAlert(String message) async {
    if (kIsWeb) return;
    if (!_initialized) await init();
    const androidDetails = AndroidNotificationDetails(
      'homeguard_alerts',
      'Security alerts',
      channelDescription: 'Intrusion and system alerts from HomeGuard',
      importance: Importance.max,
      priority: Priority.high,
    );
    await _plugin.show(
      DateTime.now().millisecondsSinceEpoch ~/ 1000,
      'HomeGuard alert',
      message,
      const NotificationDetails(android: androidDetails),
    );
  }
}
