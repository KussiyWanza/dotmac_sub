import 'dart:io';
import 'dart:typed_data';

import 'package:drift/native.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:sentry_flutter/sentry_flutter.dart';

import 'app/app.dart';
import 'core/location/location_ping_store.dart';
import 'core/offline/connectivity.dart';
import 'core/offline/database.dart';
import 'core/offline/draft_store.dart';
import 'core/offline/sync_service.dart';
import 'core/photos/photo_queue.dart';
import 'core/push/fcm_push_source.dart';
import 'core/push/push_registrar.dart';
import 'features/auth/auth_repository.dart' show appVersion;
import 'features/auth/auth_state.dart';
import 'features/execution/completion_wizard.dart';
import 'features/execution/execution_controller.dart';
import 'features/location/location_ping_service.dart';

class _QueuedCompletionPhotoGateway implements CompletionPhotoGateway {
  const _QueuedCompletionPhotoGateway(this.queue);

  final PhotoQueue queue;

  @override
  Future<bool> capture({required String workOrderId}) =>
      queue.captureForJob(workOrderId: workOrderId);

  @override
  Future<bool> recoverLost({required String workOrderId}) =>
      queue.recoverForJob(workOrderId: workOrderId);
}

/// Crash/error telemetry DSN, injected via `--dart-define=SENTRY_DSN=...`.
/// Empty (the default) disables telemetry entirely — local/dev builds run
/// untouched. Sentry auto-captures uncaught Flutter + async errors once init'd.
const _sentryDsn = String.fromEnvironment('SENTRY_DSN');

/// Builds the fully-wired app root (drift DB, offline sync, photo/signature
/// queues, optional FCM). Shared by [main] and the screenshot integration test
/// so the harness renders the exact same provider graph as production.
Future<Widget> buildFieldAppRoot() async {
  final documents = await getApplicationDocumentsDirectory();
  final dbFile = File(p.join(documents.path, 'dotmac_field.sqlite'));
  final photoDir = Directory(p.join(documents.path, 'field_photos'));
  await photoDir.create(recursive: true);

  final db = AppDatabase(NativeDatabase(dbFile));

  // FCM push, when Firebase is configured (else null → NoopPushSource).
  final fcm = await FcmPushSource.tryCreate();

  return ProviderScope(
    overrides: [
      draftStoreProvider.overrideWithValue(DraftStore(db)),
      locationPingStoreProvider.overrideWithValue(
        FileLocationPingStore(
          File(p.join(documents.path, 'pending_location_pings.json')),
        ),
      ),
      if (fcm != null) pushSourceProvider.overrideWithValue(fcm),
      syncServiceProvider.overrideWith((ref) {
        final sync = SyncService(
          db: db,
          api: ref.watch(apiClientProvider),
          connectivity: DeviceConnectivity(),
        );
        Future.microtask(sync.flushAll);
        ref.onDispose(sync.dispose);
        ref.onDispose(db.close);
        return sync;
      }),
      photoCaptureProvider.overrideWith((ref) {
        final queue = PhotoQueue(
          db: db,
          source: CameraImageSource(),
          location: ref.watch(locationSourceProvider),
          storageDir: photoDir,
        );
        return _QueuedCompletionPhotoGateway(queue);
      }),
      signatureSinkProvider.overrideWith((ref) {
        final queue = PhotoQueue(
          db: db,
          source: CameraImageSource(),
          location: ref.watch(locationSourceProvider),
          storageDir: photoDir,
        );
        return ({required String workOrderId, required Uint8List png}) {
          return queue.enqueueImageBytes(
            png,
            kind: 'signature',
            workOrderId: workOrderId,
          );
        };
      }),
    ],
    child: const DotmacFieldApp(),
  );
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final root = await buildFieldAppRoot();
  void runTheApp() => runApp(root);

  if (_sentryDsn.isEmpty) {
    // No DSN configured (local/dev): run without telemetry.
    runTheApp();
    return;
  }

  await SentryFlutter.init((options) {
    options.dsn = _sentryDsn;
    options.tracesSampleRate = 0.2;
    options.release = 'dotmac_field@$appVersion';
  }, appRunner: runTheApp);
}
