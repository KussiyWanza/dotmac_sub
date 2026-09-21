import 'package:dotmac_portal/src/features/billing/invoices_screen.dart';
import 'package:dotmac_portal/src/models/invoice.dart';
import 'package:dotmac_portal/src/models/ledger.dart';
import 'package:dotmac_portal/src/models/page.dart' as pagination;
import 'package:dotmac_portal/src/providers/auth_controller.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

pagination.Page<T> _emptyPage<T>() =>
    pagination.Page<T>(items: const [], count: 0, limit: 50, offset: 0);

void main() {
  testWidgets(
    'Invoices empty state does not show a global payment action',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final router = GoRouter(
        initialLocation: '/billing',
        routes: [
          GoRoute(path: '/billing', builder: (_, __) => const InvoicesScreen()),
        ],
      );
      addTearDown(router.dispose);

      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            currentUserProvider.overrideWithValue(null),
            invoicesProvider.overrideWith((_) async => _emptyPage<Invoice>()),
            paymentsProvider.overrideWith((_) async => _emptyPage<Payment>()),
            ledgerProvider.overrideWith((_) async => _emptyPage<LedgerTxn>()),
            balanceProvider.overrideWith(
              (_) async => AccountBalance(creditBalance: 0, currency: 'NGN'),
            ),
          ],
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('No invoices yet.'), findsOneWidget);
      expect(find.byKey(const ValueKey('billing-add-funds')), findsNothing);
      expect(find.text('Add funds / Pay'), findsNothing);
      expect(find.text('Make payment'), findsNothing);
    },
  );

  testWidgets('Payment rows open the exact payment detail route',
      (tester) async {
    final payment = Payment(
      id: 'payment-1',
      amount: 2500,
      currency: 'NGN',
      status: 'succeeded',
    );
    final router = GoRouter(
      initialLocation: '/billing',
      routes: [
        GoRoute(path: '/billing', builder: (_, __) => const InvoicesScreen()),
        GoRoute(
          path: '/billing/payments/:id',
          builder: (_, state) =>
              Scaffold(body: Text('Payment ${state.pathParameters['id']}')),
        ),
      ],
    );
    addTearDown(router.dispose);

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          currentUserProvider.overrideWithValue(null),
          invoicesProvider.overrideWith((_) async => _emptyPage<Invoice>()),
          paymentsProvider.overrideWith(
            (_) async => pagination.Page(
              items: [payment],
              count: 1,
              limit: 50,
              offset: 0,
            ),
          ),
          ledgerProvider.overrideWith((_) async => _emptyPage<LedgerTxn>()),
          balanceProvider.overrideWith(
            (_) async => AccountBalance(creditBalance: 0, currency: 'NGN'),
          ),
        ],
        child: MaterialApp.router(routerConfig: router),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('Payments'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('payment-payment-1')));
    await tester.pumpAndSettle();

    expect(find.text('Payment payment-1'), findsOneWidget);
  });

  testWidgets('Activity rows open the exact activity detail route',
      (tester) async {
    final entry = LedgerTxn(
      id: 'ledger-1',
      entryType: 'credit',
      amount: 2500,
      currency: 'NGN',
      createdAt: DateTime(2026),
      source: 'payment',
      paymentId: 'payment-1',
    );
    final router = GoRouter(
      initialLocation: '/billing',
      routes: [
        GoRoute(path: '/billing', builder: (_, __) => const InvoicesScreen()),
        GoRoute(
          path: '/billing/activity/:id',
          builder: (_, state) =>
              Scaffold(body: Text('Activity ${state.pathParameters['id']}')),
        ),
      ],
    );
    addTearDown(router.dispose);

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          currentUserProvider.overrideWithValue(null),
          invoicesProvider.overrideWith((_) async => _emptyPage<Invoice>()),
          paymentsProvider.overrideWith((_) async => _emptyPage<Payment>()),
          ledgerProvider.overrideWith(
            (_) async => pagination.Page(
              items: [entry],
              count: 1,
              limit: 50,
              offset: 0,
            ),
          ),
          balanceProvider.overrideWith(
            (_) async => AccountBalance(creditBalance: 0, currency: 'NGN'),
          ),
        ],
        child: MaterialApp.router(routerConfig: router),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('Activity'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('activity-ledger-1')));
    await tester.pumpAndSettle();

    expect(find.text('Activity ledger-1'), findsOneWidget);
  });
}
