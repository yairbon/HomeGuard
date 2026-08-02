import 'package:shared_preferences/shared_preferences.dart';

/// Persists the user-editable backend URL (see [SettingsScreen]'s
/// "Backend connection" section) so it survives app restarts.
///
/// Split out from [HttpBackendService] because it has to be readable from
/// `main()` *before* that service (or anything in the widget tree) exists —
/// [main] awaits [load] to pick the URL [HomeGuardApp] constructs
/// [HttpBackendService] with.
class BackendConfig {
  BackendConfig._();

  static const _prefsKey = 'backend_url';

  /// Same default `main.dart` always used before this became configurable:
  /// correct for the emulator/desktop/web targets, wrong for a physical
  /// device (see the historical note in `main.dart`).
  static const defaultUrl = 'http://localhost:8000';

  /// Reads the saved backend URL, or [defaultUrl] if none was ever saved.
  static Future<String> load() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString(_prefsKey) ?? defaultUrl;
  }

  /// Persists [url] as the backend URL for future launches. Doesn't affect
  /// the *current* run — [HttpBackendService.baseUrl] is `final`, so this
  /// only takes effect after the app is restarted (see the settings-screen
  /// note shown next to the save button).
  static Future<void> save(String url) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_prefsKey, url.trim());
  }
}
