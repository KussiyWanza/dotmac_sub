import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/location/location_ping_store.dart';
import 'package:dotmac_field/core/location/location_source.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/draft_store.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/core/photos/photo_queue.dart';
import 'package:dotmac_field/core/secure/evidence_cipher.dart';
import 'package:dotmac_field/core/secure/evidence_files.dart';
import 'package:dotmac_field/features/location/location_cadence.dart';
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;

import '../helpers/fake_http.dart';
import '../helpers/secure_store.dart';

/// Strings a technician's device holds that must never be readable from a copy
/// of its files. Distinctive enough that a byte scan cannot match by accident.
const _customerName = 'Mrs Folasade Adeyemi-Okonkwo';
const _customerNote = 'Meter cupboard key is with the gateman, Chidi';
const _draftNote = 'Two drums of 12F drop cable for 14B Ogunlana Drive';
const _premises = '14B Ogunlana Drive, Surulere';

void main() {
  useHostSqlite3();

  test('an evidence envelope reveals nothing without the scope key', () async {
    final store = await openTestStore();
    final photo = Uint8List.fromList(utf8.encode(_customerName));

    final file = await store.evidence.write(
      'shot.evidence',
      photo,
      purpose: 'photo',
      reference: 'ref-1',
    );

    final onDisk = await file.readAsBytes();
    expect(_contains(onDisk, _customerName), isFalse);
    expect(onDisk, isNot(photo));

    // The rightful holder still reads it back byte for byte.
    final reopened = await store.evidence.read(
      file,
      purpose: 'photo',
      reference: 'ref-1',
    );
    expect(reopened, photo);
  });

  test('an envelope copied to another scope will not open there', () async {
    final device = newTestDevice();
    final one = await device.open(techOne);
    final two = await device.open(techTwo);

    final file = await one.evidence.write(
      'shot.evidence',
      utf8.encode(_customerName),
      purpose: 'photo',
      reference: 'ref-1',
    );
    final stolen = await file.readAsBytes();

    // Another principal on the same device, holding their own valid key.
    expect(
      () => two.cipher.open(
        stolen,
        context: evidenceContext(two.scopeKey, 'photo', 'ref-1'),
      ),
      throwsA(isA<EvidenceCipherFailure>()),
    );
    // ...and not even the right key opens it under the wrong binding, so an
    // envelope cannot be replayed as a different job's evidence.
    expect(
      () => one.cipher.open(
        stolen,
        context: evidenceContext(one.scopeKey, 'photo', 'ref-2'),
      ),
      throwsA(isA<EvidenceCipherFailure>()),
    );
  });

  test('a tampered envelope is refused, not partially trusted', () async {
    final store = await openTestStore();
    final sealed = store.cipher.seal(
      utf8.encode(_customerNote),
      context: 'ctx',
    );
    sealed[sealed.length - 1] ^= 0x01;

    expect(
      () => store.cipher.open(sealed, context: 'ctx'),
      throwsA(isA<EvidenceCipherFailure>()),
    );
  });

  test("a copied device's evidence files are all unreadable", () async {
    // Photos, the completion-capture marker and the queued location pings.
    final store = await openTestStore();
    final queue = PhotoQueue(
      db: store.database,
      source: FakeImageSource(Uint8List.fromList(utf8.encode(_customerName))),
      location: FakeLocation((
        latitude: 6.4954,
        longitude: 3.3543,
        accuracy: 5.0,
        timestamp: DateTime.utc(2026, 6, 1, 8),
      )),
      evidence: store.evidence,
    );

    await queue.captureForJob(workOrderId: 'wo-$_premises');
    await queue.enqueueImageBytes(
      Uint8List.fromList(utf8.encode(_customerNote)),
      kind: 'signature',
      workOrderId: 'wo-1',
    );
    await FileLocationPingStore(
      store.locationQueueFile,
      cipher: store.cipher,
      scopeKey: store.scopeKey,
      work: store.work,
    ).save([
      LocationPingPayload(
        latitude: 6.4954,
        longitude: 3.3543,
        capturedAt: DateTime.utc(2026, 8, 20, 9),
        shift: ShiftState.onShift,
        workOrderId: 'wo-$_premises',
      ),
    ]);

    final leaked = <String>[];
    await for (final entry in store.root.list(recursive: true)) {
      if (entry is! File) continue;
      final bytes = await entry.readAsBytes();
      for (final secret in [_customerName, _customerNote, _premises]) {
        if (_contains(bytes, secret)) leaked.add('${entry.path}: $secret');
      }
    }
    expect(leaked, isEmpty);
    // The scan is only meaningful if it actually looked at the evidence.
    expect(
      store.evidence.directory.listSync().whereType<File>(),
      hasLength(greaterThanOrEqualTo(2)),
    );
    expect(store.locationQueueFile.existsSync(), isTrue);
  });

  test('a copied database file reveals no customer payload', () async {
    // Worst case on purpose: the database is a PLAIN sqlite file, as if the
    // SQLCipher layer had been removed entirely. What a byte scan can still not
    // find is the payload of anything the technician wrote, because drafts and
    // queued mutations hold envelopes rather than JSON.
    final documents = Directory.systemTemp.createTempSync('plain-db-scan');
    addTearDown(() => documents.deleteSync(recursive: true));
    final file = File(p.join(documents.path, 'field.sqlite'));
    final db = AppDatabase(NativeDatabase(file));
    addTearDown(db.close);

    final store = await openTestStore();
    final drafts = DraftStore(
      db: db,
      cipher: store.cipher,
      scopeKey: store.scopeKey,
    );
    final adapter = FakeHttpAdapter();
    final sync = SyncService(
      db: db,
      api: ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: InMemoryTokenStore(),
        dio: Dio(BaseOptions(baseUrl: 'https://test.local'))
          ..httpClientAdapter = adapter,
      ),
      connectivity: FakeConnectivity(online: false),
      evidence: store.evidence,
      delay: (_) async {},
    );
    addTearDown(sync.dispose);

    await drafts.save(
      id: 'material_request:new',
      type: 'material_request',
      payload: {'note': _draftNote, 'contact': _customerName},
    );
    await sync.enqueue(
      kind: 'note',
      clientRef: 'ref-1',
      payload: {'work_order_id': 'wo-1', 'body': _customerNote},
    );
    await db.customStatement('PRAGMA wal_checkpoint(TRUNCATE);');

    final bytes = await file.readAsBytes();
    for (final secret in [_draftNote, _customerName, _customerNote]) {
      expect(
        _contains(bytes, secret),
        isFalse,
        reason: '"$secret" is readable in a copied database file',
      );
    }
    // The scan looked at a database that really does hold those rows.
    expect(await sync.pending(), hasLength(1));
    expect(
      (await drafts.load('material_request:new'))!['note'],
      _draftNote,
      reason: 'the rightful holder must still be able to read the draft',
    );
  });
}

bool _contains(List<int> haystack, String needle) {
  final target = utf8.encode(needle);
  if (target.isEmpty || haystack.length < target.length) return false;
  for (var i = 0; i <= haystack.length - target.length; i++) {
    var match = true;
    for (var j = 0; j < target.length; j++) {
      if (haystack[i + j] != target[j]) {
        match = false;
        break;
      }
    }
    if (match) return true;
  }
  return false;
}
