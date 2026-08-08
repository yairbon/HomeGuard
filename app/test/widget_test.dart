import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_fonts/google_fonts.dart';

import 'package:homeguard_app/main.dart';
import 'package:homeguard_app/services/backend_service.dart';
import 'package:homeguard_app/services/mock_backend_service.dart';

/// Pumps the app with a mock backend and lets the mock's deferred initial
/// stream emissions land.
///
/// [MockBackendService] publishes its first values on a microtask (so
/// listeners have time to subscribe), so a single `pump()` isn't always
/// enough — this settles the initial frames deterministically.
Future<void> pumpApp(WidgetTester tester, {BackendService? backend}) async {
  // The default 800x600 test surface is wider and much shorter than any
  // phone, which pushes the dashboard's arm controls below the fold. Sizing
  // to a normal handset (360x800 logical) keeps the layout under test close
  // to the one that actually ships.
  tester.view.physicalSize = const Size(1080, 2400);
  tester.view.devicePixelRatio = 3.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(HomeGuardApp(backend: backend ?? MockBackendService()));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 50));
}

/// Scrolls [label] into view before tapping it.
///
/// The dashboard is a scroll view, and `tap` on an off-screen widget aims at
/// coordinates outside the viewport and silently hits nothing — so this is
/// what makes the arming tests deterministic regardless of surface size.
Future<void> tapLabel(WidgetTester tester, String label) async {
  await tester.ensureVisible(find.text(label));
  await tester.pump();
  await tester.tap(find.text(label));
}

/// Signs in through the real login screen, the way a user would.
Future<void> login(WidgetTester tester, {String password = 'homeguard'}) async {
  await tester.enterText(find.byType(TextFormField), password);
  await tester.tap(find.text('UNLOCK'));
  // Longer than MockBackendService's simulated 500ms network latency.
  await tester.pump(const Duration(milliseconds: 800));
  await tester.pump(const Duration(milliseconds: 400)); // route transition
}

void main() {
  setUpAll(() {
    // Fonts are bundled as assets (see pubspec.yaml), but the test asset
    // bundle doesn't serve them, and google_fonts would otherwise try a real
    // HTTP fetch — which fails slowly and noisily in a test run. Disabling
    // runtime fetching makes it fall straight back to the default font.
    GoogleFonts.config.allowRuntimeFetching = false;
  });

  group('login screen', () {
    testWidgets('shows the login screen on launch', (tester) async {
      await pumpApp(tester);

      expect(find.text('HOMEGUARD'), findsOneWidget);
      expect(find.byType(TextFormField), findsOneWidget);
      expect(find.text('UNLOCK'), findsOneWidget);
    });

    testWidgets('shows an error on wrong password', (tester) async {
      await pumpApp(tester);
      await login(tester, password: 'wrong-password');

      expect(find.textContaining('Wrong password'), findsOneWidget);
    });

    testWidgets('a correct password reaches the dashboard', (tester) async {
      await pumpApp(tester);
      await login(tester);

      expect(find.text('DISARMED'), findsWidgets);
      expect(find.text('ARM AWAY'), findsOneWidget);
      expect(find.text('ARM HOME'), findsOneWidget);
    });
  });

  group('dashboard', () {
    testWidgets('arming moves through a transitional state', (tester) async {
      await pumpApp(tester);
      await login(tester);

      await tapLabel(tester, 'ARM AWAY');
      await tester.pump();
      // Mid exit-delay: the disarm control has replaced the arm-mode cards.
      expect(find.text('DISARM'), findsOneWidget);
      expect(find.text('ARM AWAY'), findsNothing);

      // Let the mock's exit delay elapse.
      await tester.pump(const Duration(seconds: 4));
      expect(find.text('ARMED'), findsWidgets);
    });

    testWidgets('disarming returns to the armable state', (tester) async {
      await pumpApp(tester);
      await login(tester);

      await tapLabel(tester, 'ARM AWAY');
      await tester.pump(const Duration(seconds: 4));
      await tapLabel(tester, 'DISARM');
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('ARM AWAY'), findsOneWidget);
    });

    testWidgets('no offline-board warning while every board reports', (tester) async {
      await pumpApp(tester);
      await login(tester);

      // MockBackendService reports all three boards healthy.
      expect(find.text('SENSORS OFFLINE'), findsNothing);
      expect(find.text('SENSOR OFFLINE'), findsNothing);
    });
  });

  group('BoardStatus', () {
    test('maps backend board ids to readable labels', () {
      // The wire format uses the backend's identifiers; the dashboard must
      // not show a raw `door_controller` to the user.
      expect(
        const BoardStatus(name: 'door_controller', online: true).label,
        'Door controller',
      );
      expect(const BoardStatus(name: 'ultrasonic', online: true).label, 'Ultrasonic board');
      expect(const BoardStatus(name: 'vibration', online: true).label, 'Vibration board');
    });

    test('falls back to the raw name for an unrecognized board', () {
      // Forward compatibility: a fourth board added on the backend should
      // render as something rather than crash or show blank.
      expect(const BoardStatus(name: 'smoke', online: false).label, 'smoke');
    });
  });

  group('event severity', () {
    test('alerts are critical, arm/disarm is a warning, the rest is info', () {
      SecurityEvent event(EventCategory category) =>
          SecurityEvent(timestamp: DateTime(2026), message: 'x', category: category);

      expect(event(EventCategory.alert).severity, EventSeverity.critical);
      expect(event(EventCategory.armDisarm).severity, EventSeverity.warning);
      expect(event(EventCategory.auth).severity, EventSeverity.info);
      expect(event(EventCategory.faceId).severity, EventSeverity.info);
      expect(event(EventCategory.info).severity, EventSeverity.info);
    });
  });
}
