import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:homeguard_app/main.dart';
import 'package:homeguard_app/services/mock_backend_service.dart';

void main() {
  testWidgets('shows the login screen on launch', (WidgetTester tester) async {
    await tester.pumpWidget(HomeGuardApp(backend: MockBackendService()));
    await tester.pump();

    expect(find.text('HOMEGUARD'), findsOneWidget);
    expect(find.byType(TextFormField), findsOneWidget);
    expect(find.text('UNLOCK'), findsOneWidget);
  });

  testWidgets('shows an error on wrong password', (WidgetTester tester) async {
    await tester.pumpWidget(HomeGuardApp(backend: MockBackendService()));
    await tester.pump();

    await tester.enterText(find.byType(TextFormField), 'wrong-password');
    await tester.tap(find.text('UNLOCK'));
    await tester.pump(const Duration(milliseconds: 600));

    expect(find.textContaining('Wrong password'), findsOneWidget);
  });
}
