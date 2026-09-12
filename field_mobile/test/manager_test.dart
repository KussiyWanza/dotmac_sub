import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:dotmac_field/features/manager/manager_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

void main() {
  testWidgets('successful approval survives a failed list refresh', (
    tester,
  ) async {
    final adapter = FakeHttpAdapter();
    final store = InMemoryTokenStore();
    await store.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'refresh',
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    final client = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
    );
    var approved = false;

    adapter.on('GET', '/api/v1/field/manager/expenses', (_) {
      if (approved) return (500, {'detail': 'temporary read failure'});
      return (
        200,
        {
          'items': [
            {
              'id': 'exp-1',
              'number': 'EXP-0001',
              'status': 'submitted',
              'purpose': 'Site transport',
              'requested_by_name': 'Ada Technician',
              'total_amount': '2500.00',
            },
          ],
        },
      );
    });
    adapter.on('POST', '/api/v1/field/manager/expenses/exp-1/approve', (_) {
      approved = true;
      return (200, {'id': 'exp-1', 'status': 'approved'});
    });

    await tester.pumpWidget(
      ProviderScope(
        overrides: [apiClientProvider.overrideWithValue(client)],
        child: const MaterialApp(home: ManagerExpenseReviewScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Raised by Ada Technician'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, 'Approve'));
    await tester.pumpAndSettle();

    expect(
      find.text('Expense approved; ERP sync needs attention'),
      findsOneWidget,
    );
    expect(find.text('Site transport'), findsNothing);
    expect(find.text('No team expenses'), findsOneWidget);
    expect(
      find.text(
        'Could not refresh approvals. Showing the last loaded results.',
      ),
      findsOneWidget,
    );
    expect(find.text('Retry'), findsOneWidget);
  });

  testWidgets('rejecting an expense closes the focused dialog safely', (
    tester,
  ) async {
    final adapter = FakeHttpAdapter();
    final store = InMemoryTokenStore();
    await store.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'refresh',
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    final client = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
    );

    adapter.on(
      'GET',
      '/api/v1/field/manager/expenses',
      (_) => (
        200,
        {
          'items': [
            {
              'id': 'exp-1',
              'number': 'EXP-0001',
              'status': 'submitted',
              'purpose': 'Site transport',
              'total_amount': '2500.00',
            },
          ],
        },
      ),
    );
    adapter.on('POST', '/api/v1/field/manager/expenses/exp-1/reject', (
      options,
    ) {
      expect(options.data, {'reason': 'Missing receipt'});
      return (200, <String, Object?>{});
    });

    await tester.pumpWidget(
      ProviderScope(
        overrides: [apiClientProvider.overrideWithValue(client)],
        child: const MaterialApp(home: ManagerExpenseReviewScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Raised by unavailable'), findsOneWidget);
    await tester.tap(find.widgetWithText(OutlinedButton, 'Reject'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'Missing receipt');
    await tester.tap(find.widgetWithText(FilledButton, 'Reject'));

    // Exercise the dialog's reverse transition while the focused text field
    // and Android-style keyboard dependencies are being deactivated.
    await tester.pump();
    expect(tester.takeException(), isNull);
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.text('Expense rejected'), findsOneWidget);
  });

  testWidgets('authorized manager can queue payment for an approved expense', (
    tester,
  ) async {
    final adapter = FakeHttpAdapter();
    final store = InMemoryTokenStore();
    await store.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'refresh',
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    final client = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
    );
    var paymentQueued = false;

    adapter.on('GET', '/api/v1/field/manager/expenses', (_) {
      return (
        200,
        {
          'items': [
            {
              'id': 'exp-approved',
              'number': 'EXP-0002',
              'status': 'approved',
              'purpose': 'Replacement router transport',
              'currency': 'NGN',
              'total_amount': '7500.00',
              if (paymentQueued) 'payment_status': 'queued',
            },
          ],
        },
      );
    });
    adapter.on('POST', '/api/v1/field/manager/expenses/exp-approved/pay', (
      options,
    ) {
      expect(options.headers['X-Request-ID'], isNotEmpty);
      paymentQueued = true;
      return (
        200,
        {
          'id': 'exp-approved',
          'status': 'approved',
          'payment_status': 'queued',
          'payment_command_id': 'command-1',
          'erp_sync_event_id': 'event-1',
        },
      );
    });

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          apiClientProvider.overrideWithValue(client),
          managerProfileProvider.overrideWith(
            (ref) async => const ManagerProfile(
              name: 'Manager',
              roles: [],
              permissions: ['operations:expense_request:pay'],
              isManager: true,
            ),
          ),
        ],
        child: const MaterialApp(home: ManagerExpenseReviewScreen()),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Pay expense'));
    await tester.pumpAndSettle();
    expect(find.text('Pay expense?'), findsOneWidget);
    await tester.tap(
      find.descendant(
        of: find.byType(AlertDialog),
        matching: find.widgetWithText(FilledButton, 'Pay expense'),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Payment queued securely in ERP'), findsOneWidget);
    expect(find.text('Payment in progress'), findsOneWidget);
  });
}
