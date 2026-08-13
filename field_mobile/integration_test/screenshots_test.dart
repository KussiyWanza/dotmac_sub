import 'dart:io' show Platform;

import 'package:dotmac_field/main.dart' as app;
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

/// App Store / Play Store screenshot harness.
///
/// Drives a real login against the configured backend (default selfcare.dotmac.io)
/// and captures each primary tab. Run via `tool/screenshots.sh`, which supplies
/// the demo credentials and selects the device. Requires a working technician
/// demo account, passed as dart-defines:
///   --dart-define=DEMO_USERNAME=... --dart-define=DEMO_PASSWORD=...
/// Optionally --dart-define=API_BASE_URL=... to target staging.
///
/// Screenshots are named `NN_<tab>`; the driver (test_driver/screenshot_driver.dart)
/// writes them to build/screenshots/.
void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  const username = String.fromEnvironment('DEMO_USERNAME');
  const password = String.fromEnvironment('DEMO_PASSWORD');

  // Primary tabs to capture, matched by their bottom-nav icon (see app/router.dart).
  const shots = <(IconData, String)>[
    (Icons.assignment_outlined, '01_today'),
    (Icons.map_outlined, '02_map'),
    (Icons.calendar_today_outlined, '03_schedule'),
    (Icons.inventory_2_outlined, '04_materials'),
    (Icons.people_alt_outlined, '05_customers'),
  ];

  testWidgets('capture store screenshots', (tester) async {
    // Android renders to a native surface; convert it so screenshots capture.
    if (Platform.isAndroid) {
      await binding.convertFlutterSurfaceToImage();
    }

    // Reuse the exact production provider graph (drift, sync, photo, FCM).
    await tester.pumpWidget(await app.buildFieldAppRoot());
    await _settle(tester);

    // --- Sign in -------------------------------------------------------------
    expect(
      username.isNotEmpty && password.isNotEmpty,
      isTrue,
      reason:
          'Pass --dart-define=DEMO_USERNAME/DEMO_PASSWORD (see tool/screenshots.sh).',
    );

    await tester.enterText(_fieldByLabel('Email or username'), username);
    await tester.enterText(_fieldByLabel('Password'), password);
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, 'Sign in'));

    // Wait for the authenticated home shell (Today tab icon) to appear.
    await _pumpUntil(tester, find.byIcon(Icons.assignment_outlined));

    // --- Capture each tab ----------------------------------------------------
    for (final (icon, name) in shots) {
      await tester.tap(find.byIcon(icon));
      await _settle(tester);
      await binding.takeScreenshot(name);
    }
  });
}

/// Matches a Material text field (TextField, or the inner field of a
/// TextFormField) by its InputDecoration label — robust to either widget.
Finder _fieldByLabel(String label) => find.byWidgetPredicate(
  (w) => w is TextField && w.decoration?.labelText == label,
);

/// pumpAndSettle, but tolerant of never-settling animations (spinners, live
/// location updates) — falls back to fixed pumps instead of throwing.
Future<void> _settle(WidgetTester tester) async {
  try {
    await tester.pumpAndSettle(
      const Duration(milliseconds: 200),
      EnginePhase.sendSemanticsUpdate,
      const Duration(seconds: 8),
    );
  } catch (_) {
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 300));
    }
  }
}

/// Pump until [finder] matches, or ~30s elapses (for async post-login nav).
/// Uses a fixed iteration count rather than wall-clock so it's independent of
/// the test binding's clock.
Future<void> _pumpUntil(
  WidgetTester tester,
  Finder finder, {
  int tries = 100,
}) async {
  for (var i = 0; i < tries; i++) {
    await tester.pump(const Duration(milliseconds: 300));
    if (finder.evaluate().isNotEmpty) return;
  }
  throw StateError(
    'Timed out waiting for $finder — login failed? Check demo creds/backend.',
  );
}
