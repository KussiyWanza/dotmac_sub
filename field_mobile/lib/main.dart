import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'package:sentry_flutter/sentry_flutter.dart';
import 'package:sqlcipher_flutter_libs/sqlcipher_flutter_libs.dart';
import 'package:sqlite3/open.dart';

import 'app/app.dart';
import 'core/api/token_store.dart';
import 'core/location/location_ping_store.dart';
import 'core/offline/connectivity.dart';
import 'core/offline/draft_store.dart';
import 'core/offline/sync_service.dart';
import 'core/photos/photo_queue.dart';
import 'core/push/fcm_push_source.dart';
import 'core/push/push_registrar.dart';
import 'core/secure/offline_wipe.dart';
import 'core/secure/plaintext_migration.dart';
import 'core/secure/scope_key_ring.dart';
import 'core/secure/scope_reconciler.dart';
import 'core/secure/secret_vault.dart';
import 'core/secure/secure_field_store.dart';
import 'core/secure/session_lifecycle.dart';
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

/// Points sqlite3 at SQLCipher on Android, where the system library is plain
/// sqlite. iOS and macOS link SQLCipher over the system library at build time,
/// so they need no override. If this fails the store still refuses to open:
/// `applySqlCipherKey` checks `cipher_version` rather than trusting the setup.
Future<void> _installSqlCipher() async {
  if (!Platform.isAndroid) return;
  await applyWorkaroundToOpenSqlCipherOnOldAndroidVersions();
  open.overrideFor(OperatingSystem.android, openCipherOnAndroid);
}

/// Builds the fully-wired app root (scoped encrypted store, offline sync,
/// photo/signature queues, optional FCM). Shared by [main] and the screenshot
/// integration test so the harness renders the exact same provider graph as
/// production.
///
/// Nothing here opens storage speculatively. The session lifecycle resolves the
/// signed-in principal first, finishes any interrupted wipe, destroys anything
/// belonging to anyone else, and only then binds a store. A launch with no
/// session reaches the login screen with no database open at all.
Future<Widget> buildFieldAppRoot() async {
  await _installSqlCipher();
  final documents = await getApplicationDocumentsDirectory();

  final tokenStore = SecureTokenStore();
  final keyRing = ScopeKeyRing(PlatformSecretVault());
  final legacyPaths = LegacyPlaintextPaths(documents);
  final wipe = ScopedOfflineWipe(
    documents: documents,
    keyRing: keyRing,
    tokenStore: tokenStore,
    legacyPaths: legacyPaths,
  );
  final lifecycle = SessionLifecycle(
    runtime: SecureRuntime(
      opener: SecureStoreOpener(documents: documents, keyRing: keyRing),
      wipe: wipe,
      reconciler: ScopeReconciler(
        documents: documents,
        keyRing: keyRing,
        wipe: wipe,
      ),
      migration: PlaintextOfflineMigration(legacyPaths: legacyPaths),
      tokenStore: tokenStore,
      baseUrl: defaultBaseUrl,
    ),
  );
  await lifecycle.restore();

  // FCM push, when Firebase is configured (else null → NoopPushSource).
  final fcm = await FcmPushSource.tryCreate();

  final container = ProviderContainer(
    overrides: [
      tokenStoreProvider.overrideWithValue(tokenStore),
      sessionLifecycleProvider.overrideWithValue(lifecycle),
      if (fcm != null) pushSourceProvider.overrideWithValue(fcm),
      draftStoreProvider.overrideWith((ref) {
        final store = ref.watch(sessionStoreProvider);
        if (store == null) return const DraftStore();
        return DraftStore(
          db: store.database,
          cipher: store.cipher,
          scopeKey: store.scopeKey,
        );
      }),
      locationPingStoreProvider.overrideWith((ref) {
        final store = ref.watch(sessionStoreProvider);
        if (store == null) return MemoryLocationPingStore();
        return FileLocationPingStore(
          store.locationQueueFile,
          cipher: store.cipher,
          scopeKey: store.scopeKey,
          work: store.work,
        );
      }),
      syncServiceProvider.overrideWith((ref) {
        final store = ref.watch(sessionStoreProvider);
        if (store == null) {
          throw StateError('No signed-in session: offline store is unbound');
        }
        final sync = SyncService(
          db: store.database,
          api: ref.watch(apiClientProvider),
          connectivity: DeviceConnectivity(),
          evidence: store.evidence,
        );
        Future.microtask(sync.requestBackgroundFlush);
        ref.onDispose(sync.dispose);
        return sync;
      }),
      photoCaptureProvider.overrideWith((ref) {
        final store = ref.watch(sessionStoreProvider);
        // No signed-in principal means no scoped place to put evidence, and
        // capturing it anyway is the one thing this change exists to prevent.
        if (store == null) return const NoopCompletionPhotoGateway();
        return _QueuedCompletionPhotoGateway(
          PhotoQueue(
            db: store.database,
            source: CameraImageSource(),
            location: ref.watch(locationSourceProvider),
            evidence: store.evidence,
          ),
        );
      }),
      signatureSinkProvider.overrideWith((ref) {
        final store = ref.watch(sessionStoreProvider);
        return ({required String workOrderId, required Uint8List png}) async {
          if (store == null) {
            throw StateError('No signed-in session: cannot store a signature');
          }
          final queue = PhotoQueue(
            db: store.database,
            source: CameraImageSource(),
            location: ref.read(locationSourceProvider),
            evidence: store.evidence,
          );
          await queue.enqueueImageBytes(
            png,
            kind: 'signature',
            workOrderId: workOrderId,
          );
        };
      }),
    ],
  );
  container.read(sessionStoreProvider.notifier).adopt(lifecycle.store);
  lifecycle.onStoreChanged = (store) =>
      container.read(sessionStoreProvider.notifier).adopt(store);

  return UncontrolledProviderScope(
    container: container,
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
