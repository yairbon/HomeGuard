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

  /// Cleans up a hand-typed backend URL into the exact shape
  /// [HttpBackendService] needs.
  ///
  /// This field is typed on a phone keyboard — usually a Tailscale address
  /// copied off another screen — and both of the mistakes it invites were
  /// previously silent, because the two ways the URL gets used are string
  /// concatenation (`'$baseUrl/login'`) and a `replaceFirst('http', 'ws')`,
  /// neither of which validates anything:
  ///
  ///  * a trailing slash produced `http://host:8000//login`, which FastAPI
  ///    404s on — every REST call fails while the app just shows "offline";
  ///  * a missing scheme (`100.64.0.1:8000`) produced a URI with no host,
  ///    so both the REST calls and the WebSocket failed to resolve.
  ///
  /// Neither is distinguishable from "backend is down" in the UI, so
  /// normalizing here is what keeps the settings screen's promise that
  /// entering an address is all it takes. Empty input is left empty so the
  /// caller can fall back to [defaultUrl] instead of storing a bare
  /// `http://`.
  static String normalize(String url) {
    var cleaned = url.trim();
    if (cleaned.isEmpty) return '';
    if (!cleaned.contains('://')) cleaned = 'http://$cleaned';
    while (cleaned.endsWith('/')) {
      cleaned = cleaned.substring(0, cleaned.length - 1);
    }
    return cleaned;
  }

  /// Reads the saved backend URL, or [defaultUrl] if none was ever saved.
  /// Normalized on the way out as well as in, so a value stored by an
  /// older build (before [normalize] existed) is repaired rather than
  /// silently breaking every request.
  static Future<String> load() async {
    final prefs = await SharedPreferences.getInstance();
    final stored = normalize(prefs.getString(_prefsKey) ?? '');
    return stored.isEmpty ? defaultUrl : stored;
  }

  /// Persists [url] as the backend URL for future launches, after running
  /// it through [normalize]; returns the value actually stored so the
  /// caller can show the user what was saved. Doesn't affect the *current*
  /// run — [HttpBackendService.baseUrl] is `final`, so this only takes
  /// effect after the app is restarted (see the settings-screen note shown
  /// next to the save button).
  static Future<String> save(String url) async {
    final prefs = await SharedPreferences.getInstance();
    final cleaned = normalize(url);
    final toStore = cleaned.isEmpty ? defaultUrl : cleaned;
    await prefs.setString(_prefsKey, toStore);
    return toStore;
  }
}
