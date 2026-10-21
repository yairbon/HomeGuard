import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_fonts/google_fonts.dart';

import 'package:homeguard_app/main.dart';
import 'package:homeguard_app/services/backend_service.dart';
import 'package:homeguard_app/services/mock_backend_service.dart';
import 'package:homeguard_app/widgets/sensor_status_panel.dart';

/// Pumps the app with a mock backend and lets the mock's deferred initial
/// stream emissions land.
///
/// [MockBackendService] publishes its first values on a microtask (so
/// listeners have time to subscribe), so a single `pump()` isn't always
/// enough â€” this settles the initial frames deterministically.
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
/// coordinates outside the viewport and silently hits nothing â€” so this is
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
    // HTTP fetch â€” which fails slowly and noisily in a test run. Disabling
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

  group('SensorStatusPanel', () {
    testWidgets('says NOT CONNECTED instead of OK for a sensor whose board is offline',
        (tester) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: SensorStatusPanel(
            sensors: const [
              SensorReading(type: SensorType.door, label: 'Door sensor', ok: true),
              SensorReading(type: SensorType.ultrasonic, label: 'Ultrasonic sensor', ok: true),
              SensorReading(type: SensorType.vibration, label: 'Vibration sensor', ok: true),
            ],
            offlineBoards: const [BoardStatus(name: 'ultrasonic', online: false)],
          ),
        ),
      ));
      // The ultrasonic board carries both the ultrasonic and the vibration sensor.
      expect(find.text('NOT CONNECTED'), findsNWidgets(2));
      expect(find.text('OK'), findsOneWidget);
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

  // ---- Paths the mock could not reach before it grew test seams ----------
  //
  // Each of these covers a branch that shipped untested purely because
  // MockBackendService had no way to produce the state: an infrastructure
  // error, a lockout, or an entry delay. Those are three of the four screens
  // that matter most when something is actually wrong.

  group('failure paths', () {
    testWidgets('an infrastructure error surfaces as a SnackBar', (tester) async {
      final backend = MockBackendService();
      await pumpApp(tester, backend: backend);
      await login(tester);

      backend.emitError('Backend unreachable');
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Backend unreachable'), findsOneWidget);
    });

    testWidgets('three wrong passwords reach the locked-out screen', (tester) async {
      await pumpApp(tester);

      for (var i = 0; i < 3; i++) {
        await tester.enterText(find.byType(TextFormField), 'wrong$i');
        await tester.tap(find.text('UNLOCK'));
        await tester.pump(const Duration(milliseconds: 800));
      }

      // The backend is the lockout authority and the app only relays it, so
      // what's asserted is that the relayed state is actually rendered: the
      // countdown replaces the error line, and the button stops offering to
      // unlock.
      expect(find.textContaining('Locked out'), findsOneWidget);
      expect(find.text('LOCKED'), findsOneWidget);
      expect(find.text('UNLOCK'), findsNothing);
    });

    testWidgets('a correct password is still refused while locked out', (tester) async {
      await pumpApp(tester);

      for (var i = 0; i < 3; i++) {
        await tester.enterText(find.byType(TextFormField), 'wrong$i');
        await tester.tap(find.text('UNLOCK'));
        await tester.pump(const Duration(milliseconds: 800));
      }
      // There is no UNLOCK button left to press â€” which is the point. A
      // lockout the right password walks straight through is not a lockout,
      // and the UI enforces that by refusing to offer the action at all.
      await tester.enterText(find.byType(TextFormField), 'homeguard');
      await tester.pump(const Duration(milliseconds: 800));

      expect(find.text('LOCKED'), findsOneWidget);
      expect(find.text('ARM AWAY'), findsNothing, reason: 'must not reach the dashboard');
    });
  });

  group('entry delay', () {
    testWidgets('shows the countdown when the door opens on an armed system',
        (tester) async {
      final backend = MockBackendService();
      await pumpApp(tester, backend: backend);
      await login(tester);

      backend.simulateEntryDelay(seconds: 20);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      // The number itself is what the person walking in is racing; asserting
      // on the phrase rather than the exact value keeps this robust to the
      // countdown having ticked once.
      expect(find.textContaining('Door opened'), findsOneWidget);
    });

    testWidgets('the countdown actually counts down', (tester) async {
      final backend = MockBackendService();
      await pumpApp(tester, backend: backend);
      await login(tester);

      backend.simulateEntryDelay(seconds: 20);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.textContaining('alarm in 20s'), findsOneWidget);

      // The backend pushes this number exactly once (`begin_entry_delay`
      // broadcasts and nothing re-sends), so without a local ticker the
      // display froze at the seeded value until the alarm fired â€” on the one
      // screen where the number is the whole point.
      await tester.pump(const Duration(seconds: 1));
      expect(find.textContaining('alarm in 19s'), findsOneWidget);

      await tester.pump(const Duration(seconds: 2));
      expect(find.textContaining('alarm in 17s'), findsOneWidget);
    });
  });
}
