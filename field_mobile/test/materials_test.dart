import 'dart:ffi' hide Size;

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/draft_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/materials/material_models.dart';
import 'package:dotmac_field/features/materials/materials_providers.dart';
import 'package:dotmac_field/features/materials/materials_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';
import 'helpers/secure_store.dart';

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

JobDetail _testJobDetail() => JobDetail(
  job: _testAssignedJobs().jobs.first,
  location: const JobLocation(source: 'none'),
  customerExperience: const JobCustomerExperience(
    project: JobLifecycleReference(
      id: 'project-1',
      number: 'PRJ-001',
      title: 'Campus rollout',
      status: 'active',
    ),
    originTicket: JobLifecycleReference(
      id: 'ticket-1',
      number: 'TKT-001',
      title: 'Fiber signal fault',
      status: 'open',
    ),
  ),
);

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

  test('searchInventory reads field inventory items', () async {
    adapter.on('GET', '/api/v1/field/inventory/items', (options) {
      expect(options.queryParameters['q'], 'cable');
      expect(options.queryParameters['source_location_id'], isNull);
      return (
        200,
        {
          'items': [
            {
              'id': 'item-1',
              'name': 'Drop cable',
              'sku': 'DC-100',
              'unit': 'm',
              'available_quantity': 50,
            },
          ],
        },
      );
    });

    final items = await container
        .read(materialsRepositoryProvider)
        .searchInventory('cable');

    expect(items.single.name, 'Drop cable');
    expect(items.single.availableQuantity, 50);
  });

  test('searchInventory can filter by source location', () async {
    adapter.on('GET', '/api/v1/field/inventory/items', (options) {
      expect(options.queryParameters['q'], 'router');
      expect(options.queryParameters['source_location_id'], 'warehouse-1');
      return (
        200,
        {
          'items': [
            {
              'id': 'item-2',
              'name': 'Router',
              'available_quantity': 4,
              'stock_by_location': [
                {
                  'location_id': 'warehouse-1',
                  'location_name': 'Main warehouse',
                  'location_code': 'WH',
                  'available_quantity': 4,
                },
              ],
            },
          ],
        },
      );
    });

    final items = await container
        .read(materialsRepositoryProvider)
        .searchInventory('router', sourceLocationId: 'warehouse-1');

    expect(items.single.availableQuantity, 4);
    expect(
      items.single.stockByLocation.single.displayLocation,
      'Main warehouse (WH)',
    );
  });

  test('createRequest posts request payload with items', () async {
    adapter.on('POST', '/api/v1/field/material-requests/submit', (options) {
      final data = (options.data as Map).cast<String, dynamic>();
      expect(data['priority'], 'high');
      expect(data['client_ref'], 'material-client-ref-1');
      expect(data['work_order_id'], 'wo-1');
      expect(data['source_warehouse_code'], 'WH-MAIN');
      expect(data['items'], [
        {'item_id': 'item-1', 'quantity': 2},
      ]);
      return (
        201,
        {
          'id': 'mr-1',
          'number': 'MR-0001',
          'status': 'submitted',
          'priority': 'high',
          'items': [
            {
              'id': 'line-1',
              'item_id': 'item-1',
              'quantity': 2,
              'item_name': 'Drop cable',
            },
          ],
        },
      );
    });

    final request = await container
        .read(materialsRepositoryProvider)
        .createRequest(
          priority: 'high',
          clientRef: 'material-client-ref-1',
          workOrderId: 'wo-1',
          sourceLocationId: 'warehouse-1',
          sourceWarehouseCode: 'WH-MAIN',
          destinationLocationId: 'van-2',
          items: [
            const MaterialRequestItemDraft(
              item: InventoryItem(id: 'item-1', name: 'Drop cable'),
              quantity: 2,
            ),
          ],
        );

    expect(request.number, 'MR-0001');
    expect(request.items.single.itemName, 'Drop cable');
  });

  test('fetchRequests reads paginated material request items', () async {
    adapter.on('GET', '/api/v1/field/material-requests', (options) {
      expect(options.queryParameters['limit'], 100);
      return (
        200,
        {
          'items': [
            {
              'id': 'mr-1',
              'number': 'MR-0001',
              'status': 'submitted',
              'priority': 'high',
            },
          ],
          'count': 1,
          'limit': 100,
          'offset': 0,
        },
      );
    });

    final requests = await container
        .read(materialsRepositoryProvider)
        .fetchRequests();

    expect(requests.totalCount, 1);
    expect(requests.items.single.number, 'MR-0001');
    expect(requests.items.single.status, 'submitted');
  });

  test('fetchRequests accepts nested response envelopes', () async {
    adapter.on('GET', '/api/v1/field/material-requests', (_) {
      return (
        200,
        {
          'data': {
            'items': [
              {'id': 'mr-2', 'number': 'MR-0002', 'status': 'issued'},
            ],
          },
        },
      );
    });

    final requests = await container
        .read(materialsRepositoryProvider)
        .fetchRequests();

    expect(requests.items.single.number, 'MR-0002');
  });

  test('fetchRequests skips malformed rows instead of crashing', () async {
    adapter.on('GET', '/api/v1/field/material-requests', (_) {
      return (
        200,
        {
          'items': [
            null,
            'bad-row',
            {'id': 'mr-3', 'number': 3003, 'status': 'submitted'},
          ],
        },
      );
    });

    final requests = await container
        .read(materialsRepositoryProvider)
        .fetchRequests();

    expect(requests.items, hasLength(1));
    expect(requests.items.single.id, 'mr-3');
    expect(requests.items.single.number, '3003');
  });

  testWidgets('materials screen shows request list before inventory', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(320, 640));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          materialRequestsProvider.overrideWith(
            (ref) async => MaterialRequestHistory(
              totalCount: 1,
              items: [
                MaterialRequest.fromJson({
                  'id': 'mr-1',
                  'number': 'MR-0001',
                  'status': 'submitted',
                  'priority': 'high',
                }),
              ],
            ),
          ),
          inventorySearchProvider.overrideWith((ref) async => const []),
        ],
        child: MaterialApp(
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: const TextScaler.linear(2)),
            child: child!,
          ),
          home: const MaterialsScreen(),
        ),
      ),
    );
    await tester.pump();

    expect(find.text('My requests (1)'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Request'), findsNothing);
    expect(find.text('MR-0001'), findsOneWidget);
    expect(find.text('Inventory'), findsOneWidget);
  });

  testWidgets('queued material request uses a friendly label instead of UUID', (
    tester,
  ) async {
    const clientRef = 'abcdef12-1234-1234-1234-123456789abc';
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          materialRequestsProvider.overrideWith(
            (ref) async => MaterialRequestHistory(
              totalCount: 1,
              items: [
                MaterialRequest.fromJson({
                  'id': clientRef,
                  'number': 'Queued materials',
                  'status': 'queued',
                  'priority': 'high',
                }),
              ],
            ),
          ),
          inventorySearchProvider.overrideWith((ref) async => const []),
        ],
        child: const MaterialApp(home: MaterialsScreen()),
      ),
    );
    await tester.pump();

    expect(find.text('Queued materials'), findsOneWidget);
    expect(find.text('queued'), findsOneWidget);
    expect(find.text(clientRef), findsNothing);
    expect(find.text('abcdef12'), findsNothing);
  });

  testWidgets('new material request form renders on phone width', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(360, 640));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          inventoryLocationsProvider.overrideWith((ref) async => const []),
          inventorySearchProvider.overrideWith((ref) async => const []),
          allAssignedJobsProvider.overrideWith(
            (ref) async => _testAssignedJobs(),
          ),
        ],
        child: const MaterialApp(home: NewMaterialRequestScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('New material request'), findsOneWidget);
    expect(find.text('Submit request'), findsOneWidget);
    expect(find.text('Save draft'), findsOneWidget);
    expect(find.byKey(const Key('material-work-order')), findsOneWidget);
    expect(find.text('Work order ID'), findsNothing);
    expect(find.text('Project ID'), findsNothing);
    expect(find.text('Ticket ID'), findsNothing);
  });

  testWidgets(
    'new material request selects an assigned work order and derives its context',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(800, 1600));
      addTearDown(() => tester.binding.setSurfaceSize(null));

      Map<String, dynamic>? posted;
      adapter.on('POST', '/api/v1/field/material-requests/submit', (options) {
        posted = (options.data as Map).cast<String, dynamic>();
        return (
          201,
          {
            'id': 'mr-9',
            'number': 'MR-0009',
            'status': 'submitted',
            'priority': 'medium',
            'items': <Object>[],
          },
        );
      });

      final router = GoRouter(
        initialLocation: '/materials/new',
        routes: [
          GoRoute(
            path: '/materials/new',
            builder: (_, _) => const NewMaterialRequestScreen(),
          ),
          GoRoute(
            path: '/materials',
            builder: (_, _) => const Scaffold(body: Text('Materials list')),
          ),
        ],
      );

      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            apiClientProvider.overrideWithValue(client),
            inventoryLocationsProvider.overrideWith(
              (ref) async => const [
                InventoryLocation(
                  id: 'warehouse-1',
                  name: 'Main warehouse',
                  code: 'WH-MAIN',
                ),
              ],
            ),
            inventorySearchProvider.overrideWith(
              (ref) async => const [
                InventoryItem(
                  id: 'item-1',
                  name: 'Drop cable',
                  sku: 'DC-100',
                  availableQuantity: 50,
                ),
              ],
            ),
            allAssignedJobsProvider.overrideWith(
              (ref) async => _testAssignedJobs(),
            ),
            jobDetailProvider(
              'wo-1',
            ).overrideWith((ref) async => _testJobDetail()),
            materialRequestsProvider.overrideWith(
              (ref) async =>
                  const MaterialRequestHistory(items: [], totalCount: 0),
            ),
          ],
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('source-location')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Main warehouse (WH-MAIN)').last);
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('material-work-order')));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('material-work-order-search')),
        findsOneWidget,
      );
      await tester.enterText(
        find.byKey(const Key('material-work-order-search')),
        'fiber',
      );
      await tester.pumpAndSettle();
      expect(find.text('Campus fiber repair'), findsOneWidget);
      expect(find.text('Library router installation'), findsNothing);
      await tester.tap(
        find.byKey(const Key('material-work-order-option-wo-1')),
      );
      await tester.pumpAndSettle();

      expect(find.text('Campus rollout (PRJ-001)'), findsOneWidget);
      expect(find.text('Fiber signal fault (TKT-001)'), findsOneWidget);

      await tester.scrollUntilVisible(
        find.text('Drop cable (DC-100)'),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.tap(find.text('Drop cable (DC-100)'));
      await tester.pump();
      await tester.ensureVisible(find.text('Add item'));
      await tester.tap(find.text('Add item'));
      await tester.pump();

      await tester.tap(find.text('Submit request'));
      await tester.pumpAndSettle();

      expect(posted, isNotNull);
      expect(posted!['work_order_id'], 'wo-1');
      expect(posted!.containsKey('project_id'), isFalse);
      expect(posted!.containsKey('ticket_id'), isFalse);
      expect(posted!['source_warehouse_code'], 'WH-MAIN');
      expect(posted!['items'], [
        {'item_id': 'item-1', 'quantity': 1},
      ]);
      expect(find.text('Materials list'), findsOneWidget);

      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
    },
  );

  test('MaterialRequest parses status flow and issued quantities', () {
    final request = MaterialRequest.fromJson({
      'id': 'mr-1',
      'number': 'MR-0001',
      'status': 'issued',
      'priority': 'high',
      'source_location': {'id': 'warehouse-1', 'name': 'Main warehouse'},
      'destination_location': {'id': 'van-2', 'name': 'Installer van'},
      'approval_notes': 'Approved for urgent install',
      'issue_notes': 'Partially issued from main warehouse',
      'submitted_at': '2026-07-04T08:00:00Z',
      'approved_at': '2026-07-04T09:00:00Z',
      'issued_at': '2026-07-04T10:00:00Z',
      'items': [
        {
          'id': 'line-1',
          'item_id': 'item-1',
          'item_name': 'Drop cable',
          'quantity': 2,
          'approved_quantity': 2,
          'issued_quantity': 1,
        },
      ],
    });

    expect(request.sourceLocationLabel, 'Main warehouse');
    expect(request.destinationLocationLabel, 'Installer van');
    expect(request.approvalNotes, 'Approved for urgent install');
    expect(request.issueNotes, 'Partially issued from main warehouse');
    expect(request.items.single.issuedQuantity, 1);
  });

  test('MaterialRequest ignores malformed item rows', () {
    final request = MaterialRequest.fromJson({
      'id': 'mr-1',
      'status': 'issued',
      'priority': 1,
      'approval_notes': 42,
      'items': [
        null,
        'bad-row',
        {
          'id': 'line-1',
          'item_id': 'item-1',
          'item_name': 123,
          'quantity': '2',
        },
      ],
    });

    expect(request.priority, '1');
    expect(request.approvalNotes, '42');
    expect(request.items, hasLength(1));
    expect(request.items.single.itemName, '123');
    expect(request.items.single.quantity, 2);
  });

  testWidgets('material request detail shows status flow and issue progress', (
    tester,
  ) async {
    final request = MaterialRequest.fromJson({
      'id': 'mr-1',
      'number': 'MR-0001',
      'status': 'issued',
      'priority': 'high',
      'notes': 'Required for the customer installation',
      'source_location': {'id': 'warehouse-1', 'name': 'Main warehouse'},
      'destination_location': {'id': 'van-2', 'name': 'Installer van'},
      'approval_notes': 'Approved for urgent install',
      'issue_notes': 'Partially issued from main warehouse',
      'items': [
        {
          'id': 'line-1',
          'item_id': 'item-1',
          'item_name': 'Drop cable',
          'quantity': 2,
          'approved_quantity': 2,
          'issued_quantity': 1,
        },
      ],
    });

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          materialRequestProvider('mr-1').overrideWith((ref) async => request),
        ],
        child: const MaterialApp(home: MaterialRequestDetailScreen(id: 'mr-1')),
      ),
    );
    await tester.pump();

    expect(find.text('MR-0001'), findsOneWidget);
    expect(find.text('issued'), findsOneWidget);
    expect(find.text('Status flow'), findsOneWidget);
    expect(find.text('Main warehouse'), findsOneWidget);
    expect(find.text('Installer van'), findsOneWidget);
    await tester.drag(find.byType(ListView), const Offset(0, -300));
    await tester.pumpAndSettle();
    expect(find.text('Description'), findsOneWidget);
    expect(find.text('Required for the customer installation'), findsOneWidget);
    await tester.scrollUntilVisible(
      find.text('2/2 approved · 1/2 issued'),
      200,
    );
    expect(find.text('2/2 approved · 1/2 issued'), findsOneWidget);
    expect(find.text('Approved for urgent install'), findsOneWidget);
  });

  test(
    'DraftStore saves, loads and deletes a material request draft',
    () async {
      final secure = await openTestStore();
      final store = DraftStore(
        db: secure.database,
        cipher: secure.cipher,
        scopeKey: secure.scopeKey,
      );

      await store.save(
        id: materialRequestDraftId,
        type: 'material_request',
        payload: {
          'priority': 'urgent',
          'source_location_id': 'warehouse-1',
          'items': [
            {
              'item': {'id': 'item-1', 'name': 'Drop cable'},
              'quantity': 2,
            },
          ],
        },
      );

      final draft = await store.load(materialRequestDraftId);
      expect(draft?['priority'], 'urgent');
      expect(draft?['source_location_id'], 'warehouse-1');

      await store.delete(materialRequestDraftId);
      expect(await store.load(materialRequestDraftId), isNull);
    },
  );
}
