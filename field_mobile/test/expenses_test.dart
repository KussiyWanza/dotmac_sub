import 'dart:ffi' hide Size;
import 'dart:io' as io;

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/draft_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/expenses/expense_models.dart';
import 'package:dotmac_field/features/expenses/expenses_providers.dart';
import 'package:dotmac_field/features/expenses/expenses_screen.dart';
import 'package:drift/native.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';

void main() {
  late ProviderContainer container;
  late FakeHttpAdapter adapter;
  late ApiClient client;

  setUpAll(() {
    open.overrideFor(
      OperatingSystem.linux,
      () => DynamicLibrary.open('libsqlite3.so.0'),
    );
  });

  setUp(() async {
    adapter = FakeHttpAdapter();
    final store = InMemoryTokenStore();
    await store.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'refresh',
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    client = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
    );
    container = ProviderContainer(
      overrides: [apiClientProvider.overrideWithValue(client)],
    );
  });

  tearDown(() => container.dispose());

  test('fetchRequests reads paginated expense request items', () async {
    adapter.on('GET', '/api/v1/field/expense-requests', (options) {
      expect(options.queryParameters['limit'], 100);
      expect(options.queryParameters['status'], isNull);
      return (
        200,
        {
          'items': [
            {
              'id': 'exp-1',
              'number': 'EXP-0001',
              'status': 'submitted',
              'purpose': 'Fuel for generator',
              'total_amount': '150.00',
            },
          ],
          'count': 1,
          'limit': 100,
          'offset': 0,
        },
      );
    });
    final requests = await container
        .read(expensesRepositoryProvider)
        .fetchRequests();

    expect(requests.single.number, 'EXP-0001');
    expect(requests.single.status, 'submitted');
    expect(requests.single.totalAmount, 150.0);
  });

  test('fetchRequests can filter by status', () async {
    adapter.on('GET', '/api/v1/field/expense-requests', (options) {
      expect(options.queryParameters['status'], 'submitted');
      return (200, {'items': <Object>[]});
    });

    final requests = await container
        .read(expensesRepositoryProvider)
        .fetchRequests(status: 'submitted');

    expect(requests, isEmpty);
  });

  test('fetchRequests accepts nested response envelopes', () async {
    adapter.on('GET', '/api/v1/field/expense-requests', (_) {
      return (
        200,
        {
          'data': {
            'items': [
              {'id': 'exp-2', 'number': 'EXP-0002', 'status': 'approved'},
            ],
          },
        },
      );
    });

    final requests = await container
        .read(expensesRepositoryProvider)
        .fetchRequests();

    expect(requests.single.number, 'EXP-0002');
  });

  test('fetchRequests skips malformed rows instead of crashing', () async {
    adapter.on('GET', '/api/v1/field/expense-requests', (_) {
      return (
        200,
        {
          'items': [
            null,
            'bad-row',
            {'id': 'exp-3', 'number': 3003, 'status': 'submitted'},
          ],
        },
      );
    });

    final requests = await container
        .read(expensesRepositoryProvider)
        .fetchRequests();

    expect(requests, hasLength(1));
    expect(requests.single.id, 'exp-3');
    expect(requests.single.number, '3003');
  });

  test('fetchRequest reads a single expense request', () async {
    adapter.on('GET', '/api/v1/field/expense-requests/exp-1', (_) {
      return (
        200,
        {
          'id': 'exp-1',
          'number': 'EXP-0001',
          'status': 'approved',
          'purpose': 'Fuel for generator',
          'erp_claim_number': 'EC-77',
        },
      );
    });

    final request = await container
        .read(expensesRepositoryProvider)
        .fetchRequest('exp-1');

    expect(request.status, 'approved');
    expect(request.erpClaimNumber, 'EC-77');
  });

  test('createRequest posts payload with items and work order', () async {
    adapter.on('POST', '/api/v1/field/expense-requests/submit', (options) {
      final data = (options.data as Map).cast<String, dynamic>();
      expect(data['purpose'], 'Site logistics');
      expect(data['work_order_id'], 'wo-1');
      expect(data['expense_date'], '2026-07-06');
      expect(data['client_ref'], 'expense-client-ref-1');
      expect(data.containsKey('project_id'), isFalse);
      expect(data['items'], [
        {
          'category_code': 'TRANSPORT',
          'category_name': 'Transport',
          'description': 'Taxi from depot',
          'amount': '2500.00',
          'vendor_name': 'City Cabs',
          'receipt_url': 'https://receipts.test/taxi.jpg',
        },
      ]);
      return (
        201,
        {
          'id': 'exp-9',
          'number': 'EXP-0009',
          'status': 'submitted',
          'purpose': 'Site logistics',
          'total_amount': '2500.00',
          'items': [
            {
              'id': 'line-1',
              'category_code': 'TRANSPORT',
              'category_name': 'Transport',
              'description': 'Taxi from depot',
              'amount': '2500.00',
            },
          ],
        },
      );
    });

    final request = await container
        .read(expensesRepositoryProvider)
        .createRequest(
          purpose: 'Site logistics',
          clientRef: 'expense-client-ref-1',
          workOrderId: 'wo-1',
          expenseDate: '2026-07-06',
          items: [
            const ExpenseItemDraft(
              categoryCode: 'TRANSPORT',
              categoryName: 'Transport',
              description: 'Taxi from depot',
              amount: 2500,
              vendorName: 'City Cabs',
              receiptUrl: 'https://receipts.test/taxi.jpg',
            ),
          ],
        );

    expect(request.number, 'EXP-0009');
    expect(request.items.single.categoryLabel, 'Transport');
    expect(request.totalAmount, 2500.0);
  });

  test('buildExpenseRequestPayload includes client ref and receipt urls', () {
    final payload = buildExpenseRequestPayload(
      purpose: 'Fuel',
      clientRef: 'expense-client-ref-2',
      items: const [
        ExpenseItemDraft(
          categoryCode: 'FUEL',
          description: 'Diesel',
          amount: 5000,
          receiptUrl: 'https://receipts.test/fuel.jpg',
        ),
      ],
    );

    expect(payload['client_ref'], 'expense-client-ref-2');
    expect(payload['items'], [
      {
        'category_code': 'FUEL',
        'description': 'Diesel',
        'amount': '5000.00',
        'receipt_url': 'https://receipts.test/fuel.jpg',
      },
    ]);
  });

  test('cancelRequest posts to the cancel endpoint', () async {
    adapter.on('POST', '/api/v1/field/expense-requests/exp-1/cancel', (_) {
      return (200, {'id': 'exp-1', 'status': 'canceled'});
    });

    final request = await container
        .read(expensesRepositoryProvider)
        .cancelRequest('exp-1');

    expect(request.status, 'canceled');
  });

  test('fetchCategories reads a bare category list', () async {
    adapter.on('GET', '/api/v1/field/expense-requests/categories', (_) {
      return (
        200,
        [
          {
            'category_code': 'FUEL',
            'category_name': 'Fuel',
            'requires_receipt': true,
            'max_amount_per_claim': '50000.00',
          },
          {
            'category_code': 'TRANSPORT',
            'category_name': 'Transport',
            'requires_receipt': false,
            'max_amount_per_claim': null,
          },
        ],
      );
    });

    final categories = await container
        .read(expensesRepositoryProvider)
        .fetchCategories();

    expect(categories, hasLength(2));
    expect(categories.first.categoryCode, 'FUEL');
    expect(categories.first.requiresReceipt, isTrue);
    expect(categories.first.maxAmountPerClaim, 50000.0);
    expect(categories.last.maxAmountPerClaim, isNull);
  });

  test('fetchVendors reads vendor pick list labels', () async {
    adapter.on('GET', '/api/v1/field/expense-requests/vendors', (options) {
      expect(options.queryParameters['limit'], 25);
      return (
        200,
        {
          'items': [
            {'id': 'vendor-1', 'label': 'City Cabs'},
            {'id': 'vendor-2', 'label': 'Diesel Depot'},
          ],
        },
      );
    });

    final vendors = await container
        .read(expensesRepositoryProvider)
        .fetchVendors();

    expect(vendors, ['City Cabs', 'Diesel Depot']);
  });

  test(
    'uploadReceipt posts multipart receipt and returns download path',
    () async {
      final dir = await io.Directory.systemTemp.createTemp('receipt-test');
      final file = io.File('${dir.path}/receipt.jpg');
      await file.writeAsBytes([0xff, 0xd8, 0xff, 0xd9]);
      addTearDown(() => dir.delete(recursive: true));

      adapter.on('POST', '/api/v1/field/expense-requests/receipts', (options) {
        final form = options.data as FormData;
        expect(
          form.fields.any(
            (entry) => entry.key == 'work_order_id' && entry.value == 'wo-1',
          ),
          isTrue,
        );
        expect(
          form.fields.any(
            (entry) => entry.key == 'client_ref' && entry.value == 'ref-1',
          ),
          isTrue,
        );
        expect(form.files.single.key, 'file');
        return (
          201,
          {
            'id': 'attachment-1',
            'download_path': '/api/v1/field/attachments/attachment-1/content',
          },
        );
      });

      final path = await container
          .read(expensesRepositoryProvider)
          .uploadReceipt(
            workOrderId: 'wo-1',
            filePath: file.path,
            fileName: 'receipt.jpg',
            clientRef: 'ref-1',
          );

      expect(path, '/api/v1/field/attachments/attachment-1/content');
    },
  );

  test('ExpenseRequest parses status, ERP fields and items', () {
    final request = ExpenseRequest.fromJson({
      'id': 'exp-1',
      'number': 'EXP-0001',
      'status': 'rejected',
      'purpose': 'Taxi to site',
      'expense_date': '2026-07-01',
      'currency': 'NGN',
      'total_amount': '80.00',
      'rejection_reason': 'Missing receipt',
      'erp_claim_number': 'EC-12',
      'erp_claim_status': 'rejected',
      'erp_sync_status': 'failed',
      'erp_sync_error': 'timeout',
      'submitted_at': '2026-07-01T08:00:00Z',
      'rejected_at': '2026-07-02T09:00:00Z',
      'items': [
        null,
        'bad-row',
        {
          'id': 'line-1',
          'category_code': 'TRANSPORT',
          'category_name': 'Transport',
          'description': 'Taxi',
          'amount': '80.00',
          'vendor_name': 'City Cabs',
        },
      ],
    });

    expect(request.displayNumber, 'EXP-0001');
    expect(request.statusLabel, 'rejected');
    expect(request.totalAmount, 80.0);
    expect(request.rejectionReason, 'Missing receipt');
    expect(request.erpClaimNumber, 'EC-12');
    expect(request.erpSyncStatus, 'failed');
    expect(request.items, hasLength(1));
    expect(request.items.single.amount, 80.0);
    expect(request.items.single.vendorName, 'City Cabs');
  });

  test('ExpenseRequest falls back to short id and summed items', () {
    final request = ExpenseRequest.fromJson({
      'id': 'abcdef12-3456-7890-abcd-ef1234567890',
      'status': 'draft',
      'items': [
        {'id': 'line-1', 'category_code': 'FUEL', 'amount': '10.50'},
        {'id': 'line-2', 'category_code': 'FUEL', 'amount': 4.5},
      ],
    });

    expect(request.displayNumber, 'abcdef12');
    expect(request.totalAmount, 15.0);
  });

  testWidgets('expenses screen lists submitted and rejected requests', (
    tester,
  ) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          expenseRequestsProvider.overrideWith(
            (ref) async => [
              ExpenseRequest.fromJson({
                'id': 'exp-1',
                'number': 'EXP-0001',
                'status': 'submitted',
                'purpose': 'Fuel for generator',
                'currency': 'NGN',
                'total_amount': '150.00',
              }),
              ExpenseRequest.fromJson({
                'id': 'exp-2',
                'number': 'EXP-0002',
                'status': 'rejected',
                'purpose': 'Taxi to site',
                'currency': 'NGN',
                'total_amount': '80.00',
                'rejection_reason': 'Missing receipt',
              }),
            ],
          ),
        ],
        child: const MaterialApp(home: ExpensesScreen()),
      ),
    );
    await tester.pump();

    expect(find.text('Expense requests'), findsOneWidget);
    expect(find.text('Fuel for generator'), findsOneWidget);
    expect(find.text('Taxi to site'), findsOneWidget);
    expect(find.text('submitted'), findsOneWidget);
    expect(find.text('rejected'), findsOneWidget);
    expect(find.text('NGN 150.00'), findsOneWidget);
    expect(find.text('NGN 80.00'), findsOneWidget);
  });

  testWidgets('expenses screen shows an empty state', (tester) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          expenseRequestsProvider.overrideWith((ref) async => const []),
        ],
        child: const MaterialApp(home: ExpensesScreen()),
      ),
    );
    await tester.pump();

    expect(find.text('No expense requests yet'), findsOneWidget);
  });

  testWidgets('new expense request validates lines and purpose then submits', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(800, 1600));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    Map<String, dynamic>? posted;
    adapter.on('POST', '/api/v1/field/expense-requests/submit', (options) {
      posted = (options.data as Map).cast<String, dynamic>();
      return (
        201,
        {
          'id': 'exp-9',
          'number': 'EXP-0009',
          'status': 'submitted',
          'purpose': 'Site logistics',
          'total_amount': '2500.00',
        },
      );
    });
    adapter.on(
      'GET',
      '/api/v1/field/expense-requests',
      (_) => (200, {'items': <Object>[]}),
    );

    final router = GoRouter(
      initialLocation: '/expenses/new',
      routes: [
        GoRoute(
          path: '/expenses/new',
          builder: (_, _) => const NewExpenseRequestScreen(),
        ),
        GoRoute(
          path: '/expenses',
          builder: (_, _) => const Scaffold(body: Text('Expenses list')),
        ),
      ],
    );

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          apiClientProvider.overrideWithValue(client),
          expenseCategoriesProvider.overrideWith(
            (ref) async => const [
              ExpenseCategory(categoryCode: 'FUEL', categoryName: 'Fuel'),
              ExpenseCategory(
                categoryCode: 'TRANSPORT',
                categoryName: 'Transport',
              ),
            ],
          ),
        ],
        child: MaterialApp.router(routerConfig: router),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('New expense request'), findsOneWidget);
    expect(find.text('Submit request'), findsOneWidget);
    expect(find.text('Save draft'), findsOneWidget);

    // Pick a category and describe the line, but leave the amount empty.
    await tester.tap(find.byKey(const Key('expense-category')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Transport').last);
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const Key('expense-description')),
      'Taxi from depot',
    );
    await tester.tap(find.byKey(const Key('add-expense-line')));
    await tester.pump();

    expect(find.text('Enter an amount greater than zero.'), findsOneWidget);
    expect(find.text('Total NGN 0.00'), findsNothing);

    // Now provide a valid amount and add the line.
    await tester.enterText(find.byKey(const Key('expense-amount')), '2500');
    await tester.tap(find.byKey(const Key('add-expense-line')));
    await tester.pump();

    expect(find.text('Taxi from depot'), findsOneWidget);
    expect(find.text('Total NGN 2500.00'), findsOneWidget);

    // Submitting without a purpose is blocked.
    await tester.tap(find.text('Submit request'));
    await tester.pump();
    expect(find.text('Purpose is required.'), findsOneWidget);
    expect(posted, isNull);

    // With a purpose the request posts and navigates back to the list.
    await tester.enterText(
      find.byKey(const Key('expense-purpose')),
      'Site logistics',
    );
    await tester.tap(find.text('Submit request'));
    await tester.pumpAndSettle();

    expect(posted, isNotNull);
    expect(posted!['purpose'], 'Site logistics');
    expect(posted!['work_order_id'], isNull);
    expect(posted!['items'], [
      {
        'category_code': 'TRANSPORT',
        'category_name': 'Transport',
        'description': 'Taxi from depot',
        'amount': '2500.00',
      },
    ]);
    expect(find.text('Expenses list'), findsOneWidget);

    // Let the confirmation SnackBar timer expire.
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();
  });

  testWidgets('new expense request falls back to a category code field', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(360, 640));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          expenseCategoriesProvider.overrideWith((ref) async => const []),
        ],
        child: const MaterialApp(home: NewExpenseRequestScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('expense-category-code')), findsOneWidget);
    expect(find.byKey(const Key('expense-category')), findsNothing);
  });

  testWidgets('new expense request requires receipt for receipt categories', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(800, 1200));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          expenseCategoriesProvider.overrideWith(
            (ref) async => const [
              ExpenseCategory(
                categoryCode: 'FUEL',
                categoryName: 'Fuel',
                requiresReceipt: true,
              ),
            ],
          ),
        ],
        child: const MaterialApp(home: NewExpenseRequestScreen()),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('expense-category')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Fuel').last);
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const Key('expense-description')),
      'Diesel',
    );
    await tester.enterText(find.byKey(const Key('expense-amount')), '5000');
    await tester.tap(find.byKey(const Key('add-expense-line')));
    await tester.pump();

    expect(find.text('Fuel requires a receipt.'), findsOneWidget);

    await tester.enterText(
      find.byKey(const Key('expense-receipt-url')),
      'https://receipts.test/fuel.jpg',
    );
    await tester.tap(find.byKey(const Key('add-expense-line')));
    await tester.pump();

    expect(find.text('Fuel requires a receipt.'), findsNothing);
    expect(find.text('Diesel'), findsOneWidget);
    expect(find.textContaining('receipt attached'), findsOneWidget);
  });

  test(
    'DraftStore saves, loads and deletes an expense request draft',
    () async {
      final db = AppDatabase(NativeDatabase.memory());
      addTearDown(db.close);
      final store = DraftStore(db);

      await store.save(
        id: expenseRequestDraftId,
        type: 'expense_request',
        payload: {
          'purpose': 'Fuel for generator',
          'expense_date': '2026-07-06',
          'items': [
            {'category_code': 'FUEL', 'description': 'Diesel', 'amount': 150.0},
          ],
        },
      );

      final draft = await store.load(expenseRequestDraftId);
      expect(draft?['purpose'], 'Fuel for generator');
      expect(draft?['expense_date'], '2026-07-06');

      await store.delete(expenseRequestDraftId);
      expect(await store.load(expenseRequestDraftId), isNull);
    },
  );
}
