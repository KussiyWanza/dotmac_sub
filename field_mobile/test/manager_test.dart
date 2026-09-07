import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
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

    await tester.tap(find.widgetWithText(FilledButton, 'Approve'));
    await tester.pumpAndSettle();

    expect(
      find.text('Expense approved; ERP sync needs attention'),
      findsOneWidget,
    );
    expect(find.text('Site transport'), findsNothing);
    expect(find.text('No expense approvals pending'), findsOneWidget);
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
}
