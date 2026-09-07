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
    'Billing keeps the account-level payment action in the empty state',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final router = GoRouter(
        initialLocation: '/billing',
        routes: [
          GoRoute(path: '/billing', builder: (_, __) => const InvoicesScreen()),
          GoRoute(
            path: '/topup',
            builder: (_, __) =>
                const Scaffold(body: Text('Top-up destination')),
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
      expect(find.text('Add funds / Pay'), findsOneWidget);

      await tester.tap(find.byKey(const ValueKey('billing-add-funds')));
      await tester.pumpAndSettle();

      expect(find.text('Top-up destination'), findsOneWidget);
    },
  );
}
