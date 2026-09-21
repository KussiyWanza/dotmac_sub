import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

import 'package:dotmac_portal/src/features/billing/invoices_screen.dart';
import 'package:dotmac_portal/src/models/invoice.dart';
import 'package:dotmac_portal/src/models/ledger.dart';
import 'package:dotmac_portal/src/models/page.dart' as models;
import 'package:dotmac_portal/src/providers/data_providers.dart';

models.Page<T> _emptyPage<T>() => models.Page<T>(
      items: const [],
      count: 0,
      limit: 50,
      offset: 0,
    );

Widget _app() {
  final router = GoRouter(
    initialLocation: '/billing',
    routes: [
      GoRoute(
        path: '/billing',
        builder: (_, __) => const InvoicesScreen(),
      ),
      GoRoute(
        path: '/topup',
        builder: (_, __) => const Scaffold(body: Text('Payment flow')),
      ),
    ],
  );

  return ProviderScope(
    overrides: [
      invoicesProvider.overrideWith((_) async => _emptyPage<Invoice>()),
      paymentsProvider.overrideWith((_) async => _emptyPage<Payment>()),
      ledgerProvider.overrideWith((_) async => _emptyPage<LedgerTxn>()),
    ],
    child: MaterialApp.router(routerConfig: router),
  );
}

void main() {
  testWidgets('shows a single payment action only on the Payments tab',
      (tester) async {
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();

    expect(find.byKey(const ValueKey('billing-add-funds')), findsNothing);
    expect(find.text('Add funds / Pay'), findsNothing);
    expect(find.text('Make payment'), findsNothing);

    await tester.tap(find.widgetWithText(Tab, 'Payments'));
    await tester.pumpAndSettle();

    expect(find.byKey(const ValueKey('billing-add-funds')), findsNothing);
    expect(find.text('Add funds / Pay'), findsNothing);
    expect(find.text('Make payment'), findsOneWidget);
    expect(find.byIcon(Icons.add_card_outlined), findsOneWidget);

    await tester.tap(find.widgetWithText(Tab, 'Activity'));
    await tester.pumpAndSettle();

    expect(find.byKey(const ValueKey('billing-add-funds')), findsNothing);
    expect(find.text('Add funds / Pay'), findsNothing);
    expect(find.text('Make payment'), findsNothing);
  });

  testWidgets('opens the existing payment flow from the Payments tab',
      (tester) async {
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(Tab, 'Payments'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Make payment'));
    await tester.pumpAndSettle();

    expect(find.text('Payment flow'), findsOneWidget);
  });
}
