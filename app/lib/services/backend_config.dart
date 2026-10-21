import 'package:flutter/foundation.dart' show kIsWeb;
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

  /// True for the web build the backend itself serves
  /// (`flutter build web --dart-define=HOMEGUARD_SAME_ORIGIN=true`, see
  /// `scripts/build-web.ps1`). The page and the API then share one origin, so
  /// the right backend address is simply the address the page was loaded from
  /// — which is what makes the same build work from the laptop, a phone on
  /// the WiFi, or a Tailscale address without anyone typing a URL.
  static const _sameOrigin = bool.fromEnvironment('HOMEGUARD_SAME_ORIGIN');

  /// Reads the saved backend URL; failing that, the page's own origin for the
  /// backend-served web build; failing that, [defaultUrl].
  static Future<String> load() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString(_prefsKey);
    if (saved != null) return saved;
    if (kIsWeb && _sameOrigin) return Uri.base.origin;
    return defaultUrl;
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
