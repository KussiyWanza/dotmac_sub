import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/draft_store.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/core/secure/data_scope.dart';
import 'package:dotmac_field/core/secure/encrypted_database.dart';
import 'package:dotmac_field/core/secure/evidence_cipher.dart';
import 'package:dotmac_field/core/secure/evidence_files.dart';
import 'package:dotmac_field/core/secure/secure_field_store.dart';
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;

import '../helpers/fake_http.dart';
import '../helpers/secure_store.dart';

/// The scope column is the second line of defence: the store already opens one
/// database file per principal, so these tests deliberately put two principals
/// in ONE database to prove the column alone is enough. If both mechanisms had
/// to hold for isolation to work, only one of them would really be tested.
void main() {
  useHostSqlite3();

  late Directory documents;
  late AppDatabase shared;

  SecureFieldStore storeFor(DataScope scope) {
    final root = Directory(p.join(documents.path, 'scopes', scope.key));
    root.createSync(recursive: true);
    final cipher = EvidenceCipher(EvidenceCipher.newKey());
    return SecureFieldStore(
      scope: scope,
      database: shared,
      root: root,
      cipher: cipher,
      evidence: EvidenceFiles(
        directory: Directory(p.join(root.path, 'evidence')),
        cipher: cipher,
        scopeKey: scope.key,
        work: shared.work,
      ),
    );
  }

  ({SyncService sync, FakeHttpAdapter adapter}) syncFor(
    SecureFieldStore store, {
    bool online = false,
  }) {
    final adapter = FakeHttpAdapter();
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    final service = SyncService(
      db: store.database,
      api: ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: InMemoryTokenStore(),
        dio: dio,
      ),
      connectivity: FakeConnectivity(online: online),
      evidence: store.evidence,
      delay: (_) async {},
    );
    addTearDown(service.dispose);
    return (sync: service, adapter: adapter);
  }

  DraftStore draftsFor(SecureFieldStore store) => DraftStore(
    db: store.database,
    cipher: store.cipher,
    scopeKey: store.scopeKey,
  );

  setUp(() {
    documents = Directory.systemTemp.createTempSync('scope-isolation');
    shared = AppDatabase(NativeDatabase.memory());
  });

  tearDown(() async {
    await shared.close();
    documents.deleteSync(recursive: true);
  });

  test("a second technician reads none of the first one's data", () async {
    final one = storeFor(techOne);
    final two = storeFor(techTwo);
    final first = syncFor(one).sync;
    final second = syncFor(two).sync;

    await first.cacheJobs([
      {
        'id': 'wo-1',
        'title': 'Fibre drop for Mrs Adeyemi',
        'status': 'assigned',
        'work_type': 'install',
        'priority': 'high',
        'scheduled_start': null,
      },
    ]);
    await first.enqueue(
      kind: 'note',
      clientRef: 'ref-1',
      payload: {'work_order_id': 'wo-1', 'body': 'Customer not home'},
    );
    await draftsFor(one).save(
      id: 'material_request:new',
      type: 'material_request',
      payload: {'note': 'Two drums of drop cable'},
    );

    expect(await first.readCachedJobs(), hasLength(1));
    expect(await first.pending(), hasLength(1));
    expect(await draftsFor(one).list('material_request'), hasLength(1));

    expect(await second.readCachedJobs(), isEmpty);
    expect(await second.pending(), isEmpty);
    expect(await second.outboxEntry('ref-1'), isNull);
    expect(await second.pendingPhotosForJob('wo-1'), isEmpty);
    expect(await draftsFor(two).list('material_request'), isEmpty);
    expect(await draftsFor(two).load('material_request:new'), isNull);
  });

  test('the same technician on another deployment reads nothing', () async {
    // Cross-tenant, rather than cross-user: one person, two deployments.
    final home = storeFor(techOne);
    final elsewhere = storeFor(techOneOtherTenant);
    expect(techOne.key, isNot(techOneOtherTenant.key));

    final atHome = syncFor(home).sync;
    await atHome.cacheSchedule([
      {
        'reference_id': 'sched-1',
        'type': 'work_order',
        'start_at': '2026-08-20T08:00:00Z',
        'end_at': null,
        'title': 'Ikeja splice',
      },
    ]);
    await atHome.enqueue(
      kind: 'worklog',
      clientRef: 'ref-home',
      payload: {'work_order_id': 'wo-9', 'minutes': 45},
    );

    final away = syncFor(elsewhere).sync;
    expect(await away.readCachedSchedule(), isEmpty);
    expect(await away.pending(), isEmpty);
    expect(await away.offlineRequestHistory('worklog'), isEmpty);
    // ...and the first deployment's data is untouched by the second's reads.
    expect(await atHome.readCachedSchedule(), hasLength(1));
    expect(await atHome.pending(), hasLength(1));
  });

  test('a mutation queued by another account is never sent', () async {
    final one = storeFor(techOne);
    final two = storeFor(techTwo);

    await syncFor(one).sync.enqueue(
      kind: 'transition',
      clientRef: 'ref-old',
      payload: {'work_order_id': 'wo-1', 'to_status': 'completed'},
    );

    // The new account comes online and flushes. The old entry is not selected,
    // so nothing is requested and nothing is marked sent.
    final second = syncFor(two, online: true);
    expect(await second.sync.flushOutbox(), 0);
    expect(second.adapter.requests, isEmpty);

    final survivor = await syncFor(one).sync.outboxEntry('ref-old');
    expect(survivor, isNotNull);
    expect(survivor!.status, 'pending');
    expect(survivor.attempts, 0);
  });

  test('a foreign row in our database is destroyed, not read', () async {
    final one = storeFor(techOne);
    final two = storeFor(techTwo);
    await syncFor(one).sync.enqueue(
      kind: 'note',
      clientRef: 'ref-foreign',
      payload: {'work_order_id': 'wo-1', 'body': 'not ours'},
    );
    await syncFor(two).sync.enqueue(
      kind: 'note',
      clientRef: 'ref-ours',
      payload: {'work_order_id': 'wo-2', 'body': 'ours'},
    );

    final removed = await purgeForeignScopeRows(shared, techTwo.key);

    expect(removed, greaterThan(0));
    expect(await syncFor(two).sync.outboxEntry('ref-ours'), isNotNull);
    expect(await syncFor(one).sync.outboxEntry('ref-foreign'), isNull);
  });

  test('every table is rebuildable or pending outbound, never both', () {
    final projections = shared.rebuildableProjections.toSet();
    final outbound = shared.pendingOutbound.toSet();

    expect(projections.intersection(outbound), isEmpty);
    expect(
      {...projections, ...outbound},
      shared.allTables.toSet(),
      reason: 'a table with no declared class has no wipe or migration policy',
    );
    expect(outbound.map((table) => table.actualTableName).toSet(), {
      'outbox_entries',
      'pending_photos',
      'draft_entries',
    });
  });

  test('every table carries the scope column', () {
    for (final table in shared.allTables) {
      expect(
        table.$columns.map((column) => column.name),
        contains('scope_key'),
        reason: '${table.actualTableName} can hold unscoped rows',
      );
    }
  });
}
