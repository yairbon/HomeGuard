import 'package:flutter_test/flutter_test.dart';

import 'package:homeguard_app/services/backend_config.dart';

/// [BackendConfig.normalize] is pure, so it can be tested without the
/// SharedPreferences plugin (which isn't available under `flutter test`) —
/// [BackendConfig.load]/[BackendConfig.save] can't be, which is exactly why
/// the normalization lives in its own function.
///
/// What's being pinned down here is that a hand-typed backend URL can't
/// silently produce a broken one. Both failure modes below used to reach
/// [HttpBackendService] unchanged and were indistinguishable from "the
/// backend is down", because the URL is only ever used via string
/// concatenation and a scheme swap — neither of which validates anything.
void main() {
  group('BackendConfig.normalize', () {
    test('strips surrounding whitespace', () {
      expect(BackendConfig.normalize('  http://10.0.0.5:8000 '), 'http://10.0.0.5:8000');
    });

    test('drops a trailing slash that would produce a // path', () {
      expect(BackendConfig.normalize('http://10.0.0.5:8000/'), 'http://10.0.0.5:8000');
      expect(BackendConfig.normalize('http://10.0.0.5:8000///'), 'http://10.0.0.5:8000');
    });

    test('adds the scheme when the user typed a bare host:port', () {
      expect(BackendConfig.normalize('100.64.0.1:8000'), 'http://100.64.0.1:8000');
    });

    test('leaves an explicit scheme alone, including https', () {
      expect(BackendConfig.normalize('https://guard.example:8000'), 'https://guard.example:8000');
    });

    test('handles both problems at once', () {
      expect(BackendConfig.normalize('  100.64.0.1:8000/  '), 'http://100.64.0.1:8000');
    });

    test('returns empty for blank input so callers can fall back to the default', () {
      expect(BackendConfig.normalize(''), '');
      expect(BackendConfig.normalize('   '), '');
    });

    test('the normalized form survives the ws:// scheme swap unbroken', () {
      // How HttpBackendService derives its WebSocket URL — the point of
      // normalizing is that this stays a valid URL with a real host.
      final base = BackendConfig.normalize('100.64.0.1:8000');
      final ws = Uri.parse('$base/ws'.replaceFirst('http', 'ws'));
      expect(ws.scheme, 'ws');
      expect(ws.host, '100.64.0.1');
      expect(ws.port, 8000);
      expect(ws.path, '/ws');
    });
  });
}
