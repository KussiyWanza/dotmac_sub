import 'dart:ffi' hide Size;

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/draft_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/materials/material_models.dart';
import 'package:dotmac_field/features/materials/materials_providers.dart';
import 'package:dotmac_field/features/materials/materials_screen.dart';
import 'package:drift/native.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';

void main() {
  late ProviderContainer container;
  late FakeHttpAdapter adapter;

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
    final client = ApiClient(
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

    expect(requests.single.number, 'MR-0001');
    expect(requests.single.status, 'submitted');
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

    expect(requests.single.number, 'MR-0002');
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

    expect(requests, hasLength(1));
    expect(requests.single.id, 'mr-3');
    expect(requests.single.number, '3003');
  });

  testWidgets('materials screen shows request list before inventory', (
    tester,
  ) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          materialRequestsProvider.overrideWith(
            (ref) async => [
              MaterialRequest.fromJson({
                'id': 'mr-1',
                'number': 'MR-0001',
                'status': 'submitted',
                'priority': 'high',
              }),
            ],
          ),
          inventorySearchProvider.overrideWith((ref) async => const []),
        ],
        child: const MaterialApp(home: MaterialsScreen()),
      ),
    );
    await tester.pump();

    expect(find.text('Requests'), findsOneWidget);
    expect(find.text('MR-0001'), findsOneWidget);
    expect(find.text('Inventory'), findsOneWidget);
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
        ],
        child: const MaterialApp(home: NewMaterialRequestScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('New material request'), findsOneWidget);
    expect(find.text('Submit request'), findsOneWidget);
    expect(find.text('Save draft'), findsOneWidget);
  });

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
      final db = AppDatabase(NativeDatabase.memory());
      addTearDown(db.close);
      final store = DraftStore(db);

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
