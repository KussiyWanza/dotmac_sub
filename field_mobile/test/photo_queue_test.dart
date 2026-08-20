import 'dart:ffi';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/location/location_source.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/core/photos/photo_queue.dart';
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:image/image.dart' as img;
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';

Uint8List _testImage({int width = 2400, int height = 600}) {
  final image = img.Image(width: width, height: height);
  img.fill(image, color: img.ColorRgb8(0, 150, 200));
  return Uint8List.fromList(img.encodePng(image));
}

void main() {
  if (Platform.isLinux) {
    open.overrideFor(
      OperatingSystem.linux,
      () => DynamicLibrary.open('libsqlite3.so.0'),
    );
  }

  late AppDatabase db;
  late Directory tempDir;
  late FakeImageSource source;
  late PhotoQueue queue;

  setUp(() {
    db = AppDatabase(NativeDatabase.memory());
    tempDir = Directory.systemTemp.createTempSync('photo-queue-test');
    source = FakeImageSource(_testImage());
    queue = PhotoQueue(
      db: db,
      source: source,
      location: FakeLocation((latitude: 6.43, longitude: 3.42)),
      storageDir: tempDir,
    );
  });

  tearDown(() async {
    await db.close();
    tempDir.deleteSync(recursive: true);
  });

  test('processPhoto downscales the long edge to 1600', () {
    final processed = processPhoto(_testImage(width: 2400, height: 600));
    final decoded = img.decodeImage(processed)!;
    expect(decoded.width, maxPhotoDimension);
    expect(decoded.height, lessThan(600));
  });

  test('native camera capture is bounded to the stored photo limit', () {
    expect(cameraCaptureMaxWidth, maxPhotoDimension);
    expect(cameraCaptureMaxHeight, maxPhotoDimension);
  });

  test('small images are not upscaled', () {
    final processed = processPhoto(_testImage(width: 800, height: 400));
    final decoded = img.decodeImage(processed)!;
    expect(decoded.width, 800);
  });

  test(
    'captureForJob writes the file and a queued row with metadata',
    () async {
      final captured = await queue.captureForJob(workOrderId: 'wo-1');
      expect(captured, isTrue);

      final row = (await db.select(db.pendingPhotos).get()).single;
      expect(row.workOrderId, 'wo-1');
      expect(row.latitude, 6.43);
      expect(row.uploaded, isFalse);
      expect(File(row.localPath).existsSync(), isTrue);
      expect(await queue.pendingCount(), 1);
    },
  );

  test('cancelled capture is a no-op', () async {
    source.bytes = null;
    expect(await queue.captureForJob(workOrderId: 'wo-1'), isFalse);
    expect(await queue.pendingCount(), 0);
  });

  test(
    'lost Android camera result is recovered once for its work order',
    () async {
      // Simulate Android killing the first queue after it durably records the
      // capture target but before image_picker returns to Dart.
      final marker = File('${tempDir.path}/.pending_completion_capture');
      await marker.writeAsString('wo-recovered', flush: true);
      source.lostBytes = _testImage(width: 800, height: 400);

      expect(await queue.recoverForJob(workOrderId: 'wo-recovered'), isTrue);
      expect(await queue.recoverForJob(workOrderId: 'wo-recovered'), isFalse);

      final row = (await db.select(db.pendingPhotos).get()).single;
      expect(row.workOrderId, 'wo-recovered');
      expect(row.kind, 'photo');
      expect(File(row.localPath).existsSync(), isTrue);
    },
  );

  group('flushPhotos', () {
    late FakeHttpAdapter adapter;
    late SyncService sync;
    late FakeConnectivity connectivity;

    setUp(() async {
      adapter = FakeHttpAdapter();
      connectivity = FakeConnectivity();
      final store = InMemoryTokenStore();
      await store.save(
        accessToken: fakeJwt(
          expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
        ),
        refreshToken: 'r',
        loginMode: LoginMode.staff,
      );
      final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
      dio.httpClientAdapter = adapter;
      sync = SyncService(
        db: db,
        api: ApiClient(
          baseUrl: 'https://test.local',
          tokenStore: store,
          dio: dio,
        ),
        connectivity: connectivity,
        delay: (_) async {},
      );
    });

    tearDown(() => sync.dispose());

    test('uploads multipart, marks uploaded, deletes the file', () async {
      await queue.captureForJob(workOrderId: 'wo-1');
      final localPath =
          (await db.select(db.pendingPhotos).get()).single.localPath;

      late FormData sent;
      adapter.on('POST', '/api/v1/field/attachments', (options) {
        sent = options.data as FormData;
        return (201, {'id': 'att-1'});
      });

      expect(await sync.flushPhotos(), 1);
      final fields = {for (final f in sent.fields) f.key: f.value};
      expect(fields['kind'], 'photo');
      expect(fields['work_order_id'], 'wo-1');
      expect(fields['client_ref'], isNotEmpty);
      expect(fields['latitude'], '6.43');
      expect(sent.files.single.key, 'file');

      final row = (await db.select(db.pendingPhotos).get()).single;
      expect(row.uploaded, isTrue);
      expect(File(localPath).existsSync(), isFalse);
    });

    test('4xx keeps the file and records the error', () async {
      await queue.captureForJob(workOrderId: 'wo-1');
      adapter.on(
        'POST',
        '/api/v1/field/attachments',
        (_) => (422, {'detail': 'Unsupported file type'}),
      );

      expect(await sync.flushPhotos(), 0);
      final row = (await db.select(db.pendingPhotos).get()).single;
      expect(row.uploaded, isFalse);
      expect(row.lastError, contains('Unsupported'));
      expect(File(row.localPath).existsSync(), isTrue);
    });

    test('queued photo can be removed before upload', () async {
      await queue.captureForJob(workOrderId: 'wo-1');
      final row = (await db.select(db.pendingPhotos).get()).single;

      await sync.removePendingPhoto(row.clientRef);

      expect(await db.select(db.pendingPhotos).get(), isEmpty);
      expect(File(row.localPath).existsSync(), isFalse);
    });

    test('offline is a no-op', () async {
      await queue.captureForJob(workOrderId: 'wo-1');
      connectivity.online = false;
      expect(await sync.flushPhotos(), 0);
    });
  });

  group('enqueueImageBytes (signatures)', () {
    test('queues a kind=signature pending photo from raw bytes', () async {
      await queue.enqueueImageBytes(
        _testImage(width: 600, height: 220),
        kind: 'signature',
        workOrderId: 'wo-1',
      );
      final row = (await db.select(db.pendingPhotos).get()).single;
      expect(row.kind, 'signature');
      expect(row.workOrderId, 'wo-1');
      expect(row.uploaded, isFalse);
      expect(File(row.localPath).existsSync(), isTrue);
    });
  });
}
