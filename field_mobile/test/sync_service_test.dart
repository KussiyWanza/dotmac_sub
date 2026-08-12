import 'dart:convert';
import 'dart:ffi';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:sqlite3/open.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:drift/drift.dart' show Value;
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

void main() {
  // Host machines ship libsqlite3.so.0 without the unversioned symlink.
  if (Platform.isLinux) {
    open.overrideFor(
      OperatingSystem.linux,
      () => DynamicLibrary.open('libsqlite3.so.0'),
    );
  }

  late AppDatabase db;
  late FakeHttpAdapter adapter;
  late FakeConnectivity connectivity;
  late SyncService sync;
  late List<Duration> delays;
  late Directory tempDir;

  final freshToken = fakeJwt(
    expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
  );

  setUp(() async {
    tempDir = Directory.systemTemp.createTempSync('sync-test');
    db = AppDatabase(NativeDatabase.memory());
    adapter = FakeHttpAdapter();
    connectivity = FakeConnectivity();
    delays = [];

    final store = InMemoryTokenStore();
    await store.save(
      accessToken: freshToken,
      refreshToken: 'r',
      loginMode: LoginMode.staff,
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
    dio.httpClientAdapter = adapter;
    final api = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
    );

    sync = SyncService(
      db: db,
      api: api,
      connectivity: connectivity,
      delay: (duration) async => delays.add(duration),
    );
  });

  tearDown(() async {
    await sync.dispose();
    await db.close();
    tempDir.deleteSync(recursive: true);
  });

  Map<String, dynamic> transitionPayload(String ref) => {
    'work_order_id': 'wo-1',
    'event': 'start',
    'client_event_id': ref,
  };

  test('outbox flushes FIFO and marks entries sent', () async {
    final calls = <String>[];
    adapter.on('POST', '/api/v1/field/jobs/wo-1/transition', (options) {
      final body = options.data is String
          ? jsonDecode(options.data as String) as Map
          : options.data as Map;
      calls.add(body['client_event_id'] as String);
      return (200, {'ok': true});
    });

    await sync.enqueue(
      kind: 'transition',
      clientRef: 'a',
      payload: transitionPayload('a'),
    );
    await sync.enqueue(
      kind: 'transition',
      clientRef: 'b',
      payload: transitionPayload('b'),
    );

    final sent = await sync.flushOutbox();
    expect(sent, 2);
    expect(calls, ['a', 'b']);
    expect(await sync.pending(), isEmpty);
  });

  test('duplicate enqueue with same clientRef is a no-op', () async {
    await sync.enqueue(
      kind: 'transition',
      clientRef: 'dup',
      payload: transitionPayload('dup'),
    );
    await sync.enqueue(
      kind: 'transition',
      clientRef: 'dup',
      payload: transitionPayload('dup'),
    );
    expect((await sync.pending()).length, 1);
  });

  test('409 conflict parks the entry without dropping it', () async {
    adapter.on(
      'POST',
      '/api/v1/field/jobs/wo-1/transition',
      (_) => (409, {'detail': 'Cannot start a job in status completed'}),
    );

    await sync.enqueue(
      kind: 'transition',
      clientRef: 'c',
      payload: transitionPayload('c'),
    );
    await sync.flushOutbox();

    final rows = await db.select(db.outboxEntries).get();
    expect(rows.single.status, 'conflict');
    expect(rows.single.lastError, contains('completed'));
  });

  test('server error stops the flush and preserves order', () async {
    var first = true;
    adapter.on('POST', '/api/v1/field/jobs/wo-1/transition', (_) {
      if (first) {
        first = false;
        return (500, {'detail': 'boom'});
      }
      return (200, {'ok': true});
    });

    await sync.enqueue(
      kind: 'transition',
      clientRef: 'x',
      payload: transitionPayload('x'),
    );
    await sync.enqueue(
      kind: 'transition',
      clientRef: 'y',
      payload: transitionPayload('y'),
    );

    expect(await sync.flushOutbox(), 0);
    expect((await sync.pending()).length, 2); // nothing lost, order kept

    expect(await sync.flushOutbox(), 2);
  });

  test('429 honors Retry-After before retrying', () async {
    var calls = 0;
    adapter.on('POST', '/api/v1/field/jobs/wo-1/transition', (_) {
      calls++;
      return calls == 1 ? (429, {'detail': 'slow down'}) : (200, {'ok': true});
    });

    await sync.enqueue(
      kind: 'transition',
      clientRef: 'r1',
      payload: transitionPayload('r1'),
    );
    await sync.flushOutbox();
    expect(delays.any((d) => d.inSeconds >= 5), isTrue);

    expect(await sync.flushOutbox(), 1);
  });

  test('flush is a no-op while offline, runs on reconnect', () async {
    adapter.on(
      'POST',
      '/api/v1/field/jobs/wo-1/transition',
      (_) => (200, {'ok': true}),
    );
    await sync.enqueue(
      kind: 'transition',
      clientRef: 'off',
      payload: transitionPayload('off'),
    );

    connectivity.online = false;
    await Future<void>.delayed(Duration.zero);
    expect(await sync.flushOutbox(), 0);

    connectivity.online = true;
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(await sync.pending(), isEmpty);
  });

  test('down-sync upserts cached jobs', () async {
    adapter.on(
      'GET',
      '/api/v1/field/jobs',
      (_) => (
        200,
        {
          'items': [
            {
              'id': 'wo-1',
              'title': 'Install fiber',
              'status': 'dispatched',
              'work_type': 'install',
              'priority': 'normal',
              'scheduled_start': '2026-06-10T09:00:00+00:00',
            },
          ],
          'count': 1,
        },
      ),
    );

    expect(await sync.downSyncJobs(), 1);
    var cached = await db.select(db.cachedJobs).get();
    expect(cached.single.status, 'dispatched');

    adapter.on(
      'GET',
      '/api/v1/field/jobs',
      (_) => (
        200,
        {
          'items': [
            {
              'id': 'wo-1',
              'title': 'Install fiber',
              'status': 'in_progress',
              'work_type': 'install',
              'priority': 'normal',
            },
          ],
          'count': 1,
        },
      ),
    );
    await sync.downSyncJobs();
    cached = await db.select(db.cachedJobs).get();
    expect(cached.single.status, 'in_progress');
    expect(cached.length, 1);
  });

  test('flushAll uploads photos before outbox mutations', () async {
    final order = <String>[];
    adapter.on('POST', '/api/v1/field/attachments', (_) {
      order.add('photo');
      return (201, {'id': 'att-1'});
    });
    adapter.on('POST', '/api/v1/field/jobs/wo-1/transition', (_) {
      order.add('transition');
      return (200, {'ok': true});
    });

    // A pending photo and a queued complete transition.
    await db
        .into(db.pendingPhotos)
        .insert(
          PendingPhotosCompanion.insert(
            clientRef: 'pp-1',
            localPath: '${tempDir.path}/pp-1.jpg',
            capturedAt: DateTime.now().toUtc(),
            workOrderId: const Value('wo-1'),
          ),
        );
    File('${tempDir.path}/pp-1.jpg').writeAsBytesSync([1, 2, 3]);
    await sync.enqueue(
      kind: 'transition',
      clientRef: 'tx-1',
      payload: transitionPayload('tx-1'),
    );

    await sync.flushAll();
    expect(order, ['photo', 'transition']);
  });

  test(
    '5xx poison entry parks as conflict after the attempt cap, queue drains',
    () async {
      adapter.on(
        'POST',
        '/api/v1/field/jobs/wo-1/transition',
        (_) => (500, {'detail': 'boom'}),
      );

      await sync.enqueue(
        kind: 'transition',
        clientRef: 'poison',
        payload: transitionPayload('poison'),
      );
      await sync.enqueue(
        kind: 'transition',
        clientRef: 'behind',
        payload: transitionPayload('behind'),
      );

      // Each flush bumps attempts and breaks (5xx). After the cap it parks.
      for (var i = 0; i < 6; i++) {
        await sync.flushOutbox();
      }

      final rows = await db.select(db.outboxEntries).get();
      final poison = rows.firstWhere((r) => r.clientRef == 'poison');
      expect(poison.status, 'conflict');
      expect(poison.lastError, contains('Gave up'));
      // The entry behind it is no longer head-of-line blocked.
      final behind = rows.firstWhere((r) => r.clientRef == 'behind');
      expect(behind.status, 'pending');
      expect(await sync.pending(), [behind]);
    },
  );

  test('permanently-4xx photo is marked failed and not retried', () async {
    await db
        .into(db.pendingPhotos)
        .insert(
          PendingPhotosCompanion.insert(
            clientRef: 'pp-bad',
            localPath: '${tempDir.path}/pp-bad.jpg',
            capturedAt: DateTime.now().toUtc(),
            workOrderId: const Value('wo-1'),
          ),
        );
    File('${tempDir.path}/pp-bad.jpg').writeAsBytesSync([1, 2, 3]);

    var calls = 0;
    adapter.on('POST', '/api/v1/field/attachments', (_) {
      calls++;
      return (415, {'detail': 'Unsupported file type'});
    });

    await sync.flushPhotos();
    await sync.flushPhotos(); // second flush must NOT retry it

    expect(calls, 1);
    final row = (await db.select(db.pendingPhotos).get()).single;
    expect(row.failed, isTrue);
    expect(row.uploaded, isFalse);
    expect(File(row.localPath).existsSync(), isTrue); // kept for review
  });

  test('equipment outbox entry routes to the equipment endpoint', () async {
    final (method, path) = OutboxRouting.route('equipment', {
      'work_order_id': 'wo-9',
      'serial_number': 'SN-1',
    });
    expect(method, 'POST');
    expect(path, '/api/v1/field/jobs/wo-9/equipment');

    Map? sent;
    adapter.on('POST', '/api/v1/field/jobs/wo-9/equipment', (options) {
      sent = options.data is String ? null : options.data as Map;
      return (201, {'assignment_id': 'a-1'});
    });
    await sync.enqueue(
      kind: 'equipment',
      clientRef: 'eq-1',
      payload: {'work_order_id': 'wo-9', 'serial_number': 'SN-1'},
    );
    expect(await sync.flushOutbox(), 1);
    expect(sent?['serial_number'], 'SN-1');
  });

  test('expense request outbox entry routes to the expense endpoint', () async {
    final payload = {
      'client_ref': 'expense-client-ref-1',
      'purpose': 'Fuel',
      'items': [
        {
          'category_code': 'FUEL',
          'description': 'Diesel',
          'amount': '5000.00',
          'receipt_url': 'https://receipts.test/fuel.jpg',
        },
      ],
    };
    final (method, path) = OutboxRouting.route('expense_request', payload);
    expect(method, 'POST');
    expect(path, '/api/v1/field/expense-requests/submit');

    Map? sent;
    adapter.on('POST', '/api/v1/field/expense-requests/submit', (options) {
      sent = options.data is String ? null : options.data as Map;
      return (201, {'id': 'exp-1', 'status': 'submitted'});
    });
    await sync.enqueue(
      kind: 'expense_request',
      clientRef: 'expense-client-ref-1',
      payload: payload,
    );
    expect(await sync.flushOutbox(), 1);
    expect(sent?['client_ref'], 'expense-client-ref-1');
    expect(sent?['items'], payload['items']);
  });

  test(
    'material request outbox entry routes to the field material endpoint',
    () async {
      final payload = {
        'priority': 'high',
        'work_order_id': 'wo-1',
        'items': [
          {'item_id': 'item-1', 'quantity': 2},
        ],
        'submit': true,
      };
      final (method, path) = OutboxRouting.route('material_request', payload);
      expect(method, 'POST');
      expect(path, '/api/v1/field/material-requests/submit');

      Map? sent;
      adapter.on('POST', '/api/v1/field/material-requests/submit', (options) {
        sent = options.data is String ? null : options.data as Map;
        return (201, {'id': 'mr-1', 'status': 'submitted'});
      });
      await sync.enqueue(
        kind: 'material_request',
        clientRef: 'material-client-ref-1',
        payload: payload,
      );
      expect(await sync.flushOutbox(), 1);
      expect(sent?['client_ref'], 'material-client-ref-1');
      expect(sent?['work_order_id'], 'wo-1');
      expect(sent?['items'], payload['items']);
    },
  );
}
