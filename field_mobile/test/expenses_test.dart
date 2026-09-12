import 'dart:ffi' hide Size;
import 'dart:io' as io;

import 'package:dio/dio.dart';
import 'package:dotmac_field/app/widgets/primary_action_button.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/draft_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/expenses/expense_models.dart';
import 'package:dotmac_field/features/expenses/expenses_providers.dart';
import 'package:dotmac_field/features/expenses/expenses_screen.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';
import 'helpers/secure_store.dart';

const _testFormContext = ExpenseFormContext(
  approvers: [
    ExpenseApprover(
      erpEmployeeId: '00000000-0000-0000-0000-000000000001',
      systemUserId: '00000000-0000-0000-0000-000000000002',
      displayName: 'Expense Approver',
      email: 'approver@example.com',
    ),
  ],
  banks: [ExpenseBank(bankCode: '058', bankName: 'Test Bank')],
  profileDestination: ExpenseProfileDestination(
    available: true,
    bankCode: '058',
    bankName: 'Test Bank',
    maskedAccountNumber: '******6789',
    beneficiaryName: 'Field Technician',
  ),
);

VerifiedExpenseDestination _testVerifiedDestination({
  ExpensePaymentMode mode = ExpensePaymentMode.erpProfile,
}) => VerifiedExpenseDestination(
  destinationToken: 'enc:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
  mode: mode,
  bankCode: '058',
  bankName: 'Test Bank',
  maskedAccountNumber: '******6789',
  verifiedBeneficiaryName: 'Field Technician',
  verifiedAt: DateTime.parse('2099-09-10T10:00:00Z'),
  expiresAt: DateTime.parse('2099-09-10T10:30:00Z'),
);

JobList _testAssignedJobs() => JobList([
  JobSummary(
    id: 'wo-1',
    title: 'Campus fiber repair',
    status: 'dispatched',
    workType: 'repair',
    priority: 'normal',
  ),
  JobSummary(
    id: 'wo-2',
    title: 'Library router installation',
    status: 'scheduled',
    workType: 'install',
    priority: 'normal',
  ),
]);

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

  test('submitRequest posts the complete live API payload', () async {
    adapter.on('POST', '/api/v1/field/expense-requests/submit', (options) {
      final data = (options.data as Map).cast<String, dynamic>();
      expect(data['purpose'], 'Site logistics');
      expect(data['work_order_id'], 'wo-1');
      expect(data['expense_date'], '2026-07-06');
      expect(data['client_ref'], '00000000-0000-0000-0000-000000000011');
      expect(
        data['selected_approver'],
        _testFormContext.approvers.single.toJson(),
      );
      expect(data['payment_destination'], _testVerifiedDestination().toJson());
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
        .submitRequest(
          purpose: 'Site logistics',
          clientRef: '00000000-0000-0000-0000-000000000011',
          workOrderId: 'wo-1',
          expenseDate: '2026-07-06',
          selectedApprover: _testFormContext.approvers.single,
          paymentDestination: _testVerifiedDestination(),
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
      clientRef: '00000000-0000-0000-0000-000000000012',
      workOrderId: 'wo-2',
      selectedApprover: _testFormContext.approvers.single,
      paymentDestination: _testVerifiedDestination(),
      items: const [
        ExpenseItemDraft(
          categoryCode: 'FUEL',
          description: 'Diesel',
          amount: 5000,
          receiptUrl: 'https://receipts.test/fuel.jpg',
        ),
      ],
    );

    expect(payload['client_ref'], '00000000-0000-0000-0000-000000000012');
    expect(payload['work_order_id'], 'wo-2');
    expect(
      payload['selected_approver'],
      _testFormContext.approvers.single.toJson(),
    );
    expect(payload['payment_destination'], _testVerifiedDestination().toJson());
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

  test(
    'fetchFormContext reads approvers, banks, and the ERP profile',
    () async {
      adapter.on('GET', '/api/v1/field/expense-requests/form-context', (_) {
        return (
          200,
          {
            'approvers': [
              {
                'erp_employee_id': '00000000-0000-0000-0000-000000000001',
                'system_user_id': '00000000-0000-0000-0000-000000000002',
                'display_name': 'Expense Approver',
                'email': 'approver@example.com',
              },
            ],
            'banks': [
              {'bank_code': '058', 'bank_name': 'Test Bank'},
            ],
            'profile_destination': {
              'available': true,
              'bank_code': '058',
              'bank_name': 'Test Bank',
              'masked_account_number': '******6789',
              'beneficiary_name': 'Field Technician',
            },
          },
        );
      });

      final context = await container
          .read(expensesRepositoryProvider)
          .fetchFormContext();

      expect(context.approvers.single.displayName, 'Expense Approver');
      expect(context.banks.single.bankCode, '058');
      expect(context.profileDestination.maskedAccountNumber, '******6789');
    },
  );

  test(
    'verifyDestination posts custom details and returns a typed token',
    () async {
      adapter.on(
        'POST',
        '/api/v1/field/expense-requests/payment-destination/verify',
        (options) {
          expect(options.data, {
            'source_claim_id': '00000000-0000-0000-0000-000000000013',
            'mode': 'expense_override',
            'bank_code': '058',
            'account_number': '0123456789',
            'beneficiary_name': 'Field Technician',
          });
          return (
            200,
            {
              'destination_token': 'enc:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
              'mode': 'expense_override',
              'bank_code': '058',
              'bank_name': 'Test Bank',
              'masked_account_number': '******6789',
              'verified_beneficiary_name': 'Field Technician',
              'verified_at': '2099-09-10T10:00:00Z',
              'expires_at': '2099-09-10T10:30:00Z',
            },
          );
        },
      );

      final destination = await container
          .read(expensesRepositoryProvider)
          .verifyDestination(
            sourceClaimId: '00000000-0000-0000-0000-000000000013',
            mode: ExpensePaymentMode.expenseOverride,
            bankCode: '058',
            accountNumber: '0123456789',
            beneficiaryName: 'Field Technician',
          );

      expect(destination.mode, ExpensePaymentMode.expenseOverride);
      expect(destination.destinationToken, startsWith('enc:'));
      expect(destination.maskedAccountNumber, '******6789');
    },
  );

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
    'uploadReceipt posts multipart receipt and returns its typed result',
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

      final result = await container
          .read(expensesRepositoryProvider)
          .uploadReceipt(
            workOrderId: 'wo-1',
            filePath: file.path,
            fileName: 'receipt.jpg',
            clientRef: 'ref-1',
          );

      expect(result.attachmentId, 'attachment-1');
      expect(
        result.downloadPath,
        '/api/v1/field/attachments/attachment-1/content',
      );
    },
  );

  test('receipt upload attachment ID is used to submit the expense', () async {
    final dir = await io.Directory.systemTemp.createTemp('receipt-contract');
    final file = io.File('${dir.path}/receipt.jpg');
    await file.writeAsBytes([0xff, 0xd8, 0xff, 0xd9]);
    addTearDown(() => dir.delete(recursive: true));

    adapter.on('POST', '/api/v1/field/expense-requests/receipts', (_) {
      return (
        201,
        {
          'id': 'attachment-9',
          'download_path': '/api/v1/field/attachments/attachment-9/content',
        },
      );
    });
    adapter.on('POST', '/api/v1/field/expense-requests/submit', (options) {
      final data = (options.data as Map).cast<String, dynamic>();
      final item = ((data['items'] as List).single as Map)
          .cast<String, dynamic>();
      expect(item['receipt_attachment_id'], 'attachment-9');
      expect(item.containsKey('receipt_url'), isFalse);
      return (
        201,
        {
          'id': 'expense-9',
          'number': 'EXP-0009',
          'status': 'submitted',
          'purpose': 'Generator fuel',
        },
      );
    });

    final upload = await container
        .read(expensesRepositoryProvider)
        .uploadReceipt(
          workOrderId: 'wo-9',
          filePath: file.path,
          fileName: 'receipt.jpg',
        );
    final request = await container
        .read(expensesRepositoryProvider)
        .submitRequest(
          purpose: 'Generator fuel',
          clientRef: '00000000-0000-0000-0000-000000000019',
          workOrderId: 'wo-9',
          selectedApprover: _testFormContext.approvers.single,
          paymentDestination: _testVerifiedDestination(),
          items: [
            ExpenseItemDraft(
              categoryCode: 'FUEL',
              description: 'Diesel',
              amount: 5000,
              receiptAttachmentId: upload.attachmentId,
            ),
          ],
        );

    expect(request.id, 'expense-9');
    expect(upload.downloadPath, contains('attachment-9/content'));
  });

  test('expense draft round trip preserves receipt attachment ID', () {
    const item = ExpenseItemDraft(
      categoryCode: 'FUEL',
      description: 'Diesel',
      amount: 5000,
      receiptAttachmentId: 'attachment-11',
    );

    final restored = ExpenseItemDraft.fromDraftJson(item.toDraftJson());

    expect(restored.receiptAttachmentId, 'attachment-11');
    expect(restored.toJson()['receipt_attachment_id'], 'attachment-11');
    expect(restored.toJson().containsKey('receipt_url'), isFalse);
  });

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
      'payment_status': 'processing',
      'payment_intent_id': 'payment-12',
      'payment_error': 'Awaiting provider confirmation',
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
    expect(request.paymentStatus, 'processing');
    expect(request.paymentIntentId, 'payment-12');
    expect(request.paymentError, 'Awaiting provider confirmation');
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
    await tester.binding.setSurfaceSize(const Size(320, 640));
    addTearDown(() => tester.binding.setSurfaceSize(null));

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
        child: MaterialApp(
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: const TextScaler.linear(2)),
            child: child!,
          ),
          home: const ExpensesScreen(),
        ),
      ),
    );
    await tester.pump();

    expect(find.text('Expense requests'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Request'), findsNothing);
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

  testWidgets('queued expense uses a friendly label instead of its UUID', (
    tester,
  ) async {
    const clientRef = '12345678-1234-1234-1234-123456789abc';
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          expenseRequestsProvider.overrideWith(
            (ref) async => [
              ExpenseRequest.fromJson({
                'id': clientRef,
                'number': 'Queued expense',
                'status': 'queued',
                'purpose': 'Fuel for site visit',
              }),
            ],
          ),
        ],
        child: const MaterialApp(home: ExpensesScreen()),
      ),
    );
    await tester.pump();

    expect(find.text('Fuel for site visit'), findsOneWidget);
    expect(find.text('Queued expense'), findsOneWidget);
    expect(find.text('queued'), findsOneWidget);
    expect(find.text(clientRef), findsNothing);
    expect(find.text('12345678'), findsNothing);
  });

  testWidgets('new expense request requires a selected work order', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(800, 1600));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    Map<String, dynamic>? posted;
    var managerExpenseLoads = 0;
    adapter.on(
      'POST',
      '/api/v1/field/expense-requests/payment-destination/verify',
      (options) {
        final data = (options.data as Map).cast<String, dynamic>();
        expect(data['source_claim_id'], isA<String>());
        expect((data['source_claim_id'] as String), isNotEmpty);
        expect(data['mode'], 'erp_profile');
        expect(data.containsKey('account_number'), isFalse);
        return (
          200,
          {
            'destination_token': 'enc:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            'mode': 'erp_profile',
            'bank_code': '058',
            'bank_name': 'Test Bank',
            'masked_account_number': '******6789',
            'verified_beneficiary_name': 'Field Technician',
            'verified_at': '2099-09-10T10:00:00Z',
            'expires_at': '2099-09-10T10:30:00Z',
          },
        );
      },
    );
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
          expenseFormContextProvider.overrideWith(
            (ref) async => _testFormContext,
          ),
          allAssignedJobsProvider.overrideWith(
            (ref) async => _testAssignedJobs(),
          ),
          managerExpensesProvider.overrideWith((ref) async {
            managerExpenseLoads += 1;
            return const [];
          }),
        ],
        child: Consumer(
          builder: (context, ref, _) {
            ref.watch(managerExpensesProvider);
            return MaterialApp.router(routerConfig: router);
          },
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(managerExpenseLoads, 1);

    expect(find.text('New expense request'), findsOneWidget);
    expect(find.text('Submit request'), findsOneWidget);
    expect(find.text('Save draft'), findsOneWidget);
    expect(find.text('Work order ID'), findsNothing);
    expect(
      find.byKey(const Key('expense-approver-payment-card')),
      findsOneWidget,
    );
    expect(find.text('Approver & payment'), findsOneWidget);
    expect(find.text('Use ERP payment details'), findsOneWidget);
    expect(find.text('Input custom payment details'), findsOneWidget);
    expect(find.byKey(const Key('expense-bank')), findsNothing);

    await tester.tap(find.byKey(const Key('expense-payment-custom')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('expense-bank')), findsOneWidget);
    expect(find.byKey(const Key('expense-account-number')), findsOneWidget);
    expect(find.byKey(const Key('expense-beneficiary-name')), findsOneWidget);
    expect(find.text('Account name'), findsOneWidget);

    await tester.tap(find.byKey(const Key('expense-payment-erp')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('expense-bank')), findsNothing);

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

    // A purpose is not enough without a selected assigned work order.
    await tester.enterText(
      find.byKey(const Key('expense-purpose')),
      'Site logistics',
    );
    await tester.tap(find.text('Submit request'));
    await tester.pump();
    expect(find.text('Select a work order.'), findsOneWidget);
    expect(posted, isNull);

    // Search the assigned-work list and select the matching job.
    await tester.ensureVisible(find.byKey(const Key('expense-work-order')));
    await tester.tap(find.byKey(const Key('expense-work-order')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('expense-work-order-search')), findsOneWidget);
    await tester.enterText(
      find.byKey(const Key('expense-work-order-search')),
      'fiber',
    );
    await tester.pumpAndSettle();
    expect(find.text('Campus fiber repair'), findsOneWidget);
    expect(find.text('Library router installation'), findsNothing);
    await tester.tap(find.byKey(const Key('expense-work-order-option-wo-1')));
    await tester.pumpAndSettle();

    // The required approver error is shown beside the visible selector.
    await tester.tap(find.text('Submit request'));
    await tester.pumpAndSettle();
    expect(find.text('Select an expense approver.'), findsWidgets);
    expect(find.byKey(const Key('expense-approver')), findsOneWidget);
    expect(posted, isNull);

    await tester.tap(find.byKey(const Key('expense-approver')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Expense Approver').last);
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Submit request'));
    await tester.tap(find.text('Submit request'));
    await tester.pumpAndSettle();

    expect(posted, isNotNull);
    expect(posted!['purpose'], 'Site logistics');
    expect(posted!['work_order_id'], 'wo-1');
    expect(
      (posted!['selected_approver'] as Map)['erp_employee_id'],
      '00000000-0000-0000-0000-000000000001',
    );
    expect((posted!['payment_destination'] as Map)['mode'], 'erp_profile');
    expect(posted!['items'], [
      {
        'category_code': 'TRANSPORT',
        'category_name': 'Transport',
        'description': 'Taxi from depot',
        'amount': '2500.00',
      },
    ]);
    expect(find.text('Expenses list'), findsOneWidget);
    expect(managerExpenseLoads, 2);

    // Let the confirmation SnackBar timer expire.
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();
  });

  testWidgets('new expense request displays a structured safe server message', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(800, 1600));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    var submitted = false;
    adapter.on(
      'POST',
      '/api/v1/field/expense-requests/payment-destination/verify',
      (_) => (
        409,
        {
          'detail': {
            'code': 'operations.expense_requests.destination_expired',
            'message': 'Verify the payment destination again.',
            'context': {'internal_reason': 'expired-token'},
          },
        },
      ),
    );
    adapter.on('POST', '/api/v1/field/expense-requests/submit', (_) {
      submitted = true;
      return (500, {'detail': 'must not submit'});
    });

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          apiClientProvider.overrideWithValue(client),
          expenseCategoriesProvider.overrideWith(
            (ref) async => const [
              ExpenseCategory(
                categoryCode: 'FUEL',
                categoryName: 'Fuel',
                requiresReceipt: true,
              ),
            ],
          ),
          expenseFormContextProvider.overrideWith(
            (ref) async => _testFormContext,
          ),
        ],
        child: const MaterialApp(
          home: NewExpenseRequestScreen(
            initialWorkOrderId: 'wo-1',
            initialWorkOrderLabel: 'WO-1',
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(
      find.byKey(const Key('expense-purpose')),
      'Generator fuel',
    );
    await tester.tap(find.byKey(const Key('expense-approver')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Expense Approver').last);
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
    await tester.enterText(
      find.byKey(const Key('expense-receipt-url')),
      'https://receipts.test/fuel.jpg',
    );
    await tester.tap(find.byKey(const Key('add-expense-line')));
    await tester.pump();
    await tester.ensureVisible(find.text('Submit request'));
    await tester.tap(find.text('Submit request'));
    await tester.pumpAndSettle();

    expect(
      find.text('Verify the payment destination again.'),
      findsAtLeastNWidgets(1),
    );
    expect(find.textContaining('expired-token'), findsNothing);
    expect(submitted, isFalse);
  });

  testWidgets('new expense request blocks an empty ERP category list', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(360, 640));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          expenseCategoriesProvider.overrideWith((ref) async => const []),
          expenseFormContextProvider.overrideWith(
            (ref) async => _testFormContext,
          ),
          allAssignedJobsProvider.overrideWith(
            (ref) async => _testAssignedJobs(),
          ),
        ],
        child: const MaterialApp(home: NewExpenseRequestScreen()),
      ),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const Key('expense-category-retry')),
      400,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('expense-category-code')), findsNothing);
    expect(find.byKey(const Key('expense-category')), findsNothing);
    expect(
      find.textContaining('No expense categories are available.'),
      findsOneWidget,
    );
    final addExpense = tester.widget<OutlinedButton>(
      find.byKey(const Key('add-expense-line')),
    );
    expect(addExpense.onPressed, isNull);
  });

  testWidgets('new expense request retries unavailable categories', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(800, 1200));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    var categoryLoads = 0;

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          expenseCategoriesProvider.overrideWith((ref) async {
            categoryLoads += 1;
            if (categoryLoads == 1) {
              throw StateError('ERP unavailable');
            }
            return const [
              ExpenseCategory(categoryCode: 'FUEL', categoryName: 'Fuel'),
            ];
          }),
          expenseFormContextProvider.overrideWith(
            (ref) async => _testFormContext,
          ),
          allAssignedJobsProvider.overrideWith(
            (ref) async => _testAssignedJobs(),
          ),
        ],
        child: const MaterialApp(home: NewExpenseRequestScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Could not load expense categories.'), findsOneWidget);
    expect(find.byKey(const Key('expense-category')), findsNothing);

    await tester.tap(find.byKey(const Key('expense-category-retry')));
    await tester.pumpAndSettle();

    expect(categoryLoads, 2);
    expect(find.byKey(const Key('expense-category')), findsOneWidget);
    expect(find.text('Could not load expense categories.'), findsNothing);
  });

  testWidgets('new expense request retries unavailable approver context', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(800, 1200));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    var contextLoads = 0;

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          expenseCategoriesProvider.overrideWith(
            (ref) async => const [
              ExpenseCategory(categoryCode: 'FUEL', categoryName: 'Fuel'),
            ],
          ),
          expenseFormContextProvider.overrideWith((ref) async {
            contextLoads += 1;
            if (contextLoads == 1) {
              throw StateError('ERP unavailable');
            }
            return _testFormContext;
          }),
          allAssignedJobsProvider.overrideWith(
            (ref) async => _testAssignedJobs(),
          ),
        ],
        child: const MaterialApp(home: NewExpenseRequestScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(
      find.text('Could not load expense approvers and payment details.'),
      findsOneWidget,
    );
    expect(find.byKey(const Key('expense-approver')), findsNothing);

    await tester.ensureVisible(find.byKey(const Key('expense-category')));
    await tester.tap(find.byKey(const Key('expense-category')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Fuel').last);
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const Key('expense-description')),
      'Generator fuel',
    );
    await tester.enterText(find.byKey(const Key('expense-amount')), '5000');
    await tester.tap(find.byKey(const Key('add-expense-line')));
    await tester.pump();
    expect(find.text('Total NGN 5000.00'), findsOneWidget);

    expect(
      tester
          .widget<PrimaryActionButton>(
            find.byKey(const Key('submit-expense-request')),
          )
          .onPressed,
      isNull,
    );
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const Key('save-expense-draft')))
          .onPressed,
      isNotNull,
    );

    await tester.tap(find.byKey(const Key('expense-form-context-retry')));
    await tester.pumpAndSettle();

    expect(contextLoads, 2);
    expect(find.byKey(const Key('expense-approver')), findsOneWidget);
    expect(
      find.text('Could not load expense approvers and payment details.'),
      findsNothing,
    );
  });

  testWidgets('new expense request explains when no approver is eligible', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(800, 1200));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          expenseCategoriesProvider.overrideWith(
            (ref) async => const [
              ExpenseCategory(categoryCode: 'FUEL', categoryName: 'Fuel'),
            ],
          ),
          expenseFormContextProvider.overrideWith(
            (ref) async => const ExpenseFormContext(
              approvers: [],
              banks: [],
              profileDestination: ExpenseProfileDestination(available: false),
            ),
          ),
          allAssignedJobsProvider.overrideWith(
            (ref) async => _testAssignedJobs(),
          ),
        ],
        child: const MaterialApp(home: NewExpenseRequestScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(
      find.textContaining('No eligible expense approvers are available.'),
      findsOneWidget,
    );
    expect(
      find.textContaining('No payment destination is available.'),
      findsOneWidget,
    );
    expect(find.byKey(const Key('expense-approver')), findsNothing);
    expect(
      tester
          .widget<PrimaryActionButton>(
            find.byKey(const Key('submit-expense-request')),
          )
          .onPressed,
      isNull,
    );
  });

  testWidgets(
    'new expense request constrains dropdown labels on compact screens',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(360, 800));
      addTearDown(() => tester.binding.setSurfaceSize(null));

      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            expenseCategoriesProvider.overrideWith(
              (ref) async => const [
                ExpenseCategory(
                  categoryCode: 'SITE_OPERATIONS',
                  categoryName:
                      'Emergency site operations and maintenance supplies',
                ),
              ],
            ),
            expenseVendorsProvider.overrideWith(
              (ref) async => const [
                'Dotmac Infrastructure Maintenance and Logistics Limited',
              ],
            ),
            expenseFormContextProvider.overrideWith(
              (ref) async => _testFormContext,
            ),
            allAssignedJobsProvider.overrideWith(
              (ref) async => _testAssignedJobs(),
            ),
          ],
          child: const MaterialApp(home: NewExpenseRequestScreen()),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('expense-work-order')));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('expense-work-order-search')),
        findsOneWidget,
      );
      expect(tester.takeException(), isNull);
      await tester.tap(find.byTooltip('Close'));
      await tester.pumpAndSettle();
      await tester.drag(find.byType(ListView), const Offset(0, -700));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('expense-category')), findsOneWidget);
      expect(find.byKey(const Key('expense-vendor')), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

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
          expenseFormContextProvider.overrideWith(
            (ref) async => _testFormContext,
          ),
          allAssignedJobsProvider.overrideWith(
            (ref) async => _testAssignedJobs(),
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
    await tester.ensureVisible(find.byKey(const Key('add-expense-line')));
    await tester.drag(find.byType(ListView), const Offset(0, -180));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('add-expense-line')));
    await tester.pump();

    expect(find.text('Fuel requires a receipt.'), findsOneWidget);

    await tester.enterText(
      find.byKey(const Key('expense-receipt-url')),
      'https://receipts.test/fuel.jpg',
    );
    await tester.ensureVisible(find.byKey(const Key('add-expense-line')));
    await tester.drag(find.byType(ListView), const Offset(0, -180));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('add-expense-line')));
    await tester.pump();

    expect(find.text('Fuel requires a receipt.'), findsNothing);
    expect(find.text('Diesel'), findsOneWidget);
    expect(find.textContaining('receipt attached'), findsOneWidget);
  });

  test(
    'DraftStore saves, loads and deletes an expense request draft',
    () async {
      final secure = await openTestStore();
      final store = DraftStore(
        db: secure.database,
        cipher: secure.cipher,
        scopeKey: secure.scopeKey,
      );

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
