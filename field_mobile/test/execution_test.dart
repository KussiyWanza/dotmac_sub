import 'dart:ffi';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/location/location_source.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/core/secure/secure_field_store.dart';
import 'package:dotmac_field/features/execution/completion_state.dart';
import 'package:dotmac_field/features/execution/execution_controller.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';
import 'helpers/secure_store.dart';

void main() {
  if (Platform.isLinux) {
    open.overrideFor(
      OperatingSystem.linux,
      () => DynamicLibrary.open('libsqlite3.so.0'),
    );
  }

  late SecureFieldStore store;
  late AppDatabase db;
  late SyncService sync;
  late FakeConnectivity connectivity;
  late FakeLocation location;
  late ProviderContainer container;

  setUp(() async {
    store = await openTestStore();
    db = store.database;
    connectivity = FakeConnectivity(
      online: false,
    ); // keep entries queued for inspection
    location = FakeLocation((
      latitude: 6.43,
      longitude: 3.42,
      accuracy: 5.0,
      timestamp: DateTime.utc(2026, 6, 1, 8),
    ));
    final adapter = FakeHttpAdapter();
    final tokens = InMemoryTokenStore();
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
    dio.httpClientAdapter = adapter;
    sync = SyncService(
      db: db,
      api: ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: tokens,
        dio: dio,
      ),
      connectivity: connectivity,
      evidence: store.evidence,
      delay: (_) async {},
    );
    container = ProviderContainer(
      overrides: [
        syncServiceProvider.overrideWithValue(sync),
        locationSourceProvider.overrideWithValue(location),
      ],
    );
  });

  tearDown(() async {
    container.dispose();
    await sync.dispose();
    await db.close();
  });

  // Queued payloads are envelopes bound to the scope, so the assertions read
  // them back the same way the sync service does before it sends one.
  Future<List<Map<String, dynamic>>> queued(String kind) async {
    final rows = await db.select(db.outboxEntries).get();
    return rows
        .where((row) => row.kind == kind)
        .map((row) => readQueuedPayload(store, row.clientRef, row.payloadJson))
        .toList();
  }

  group('transition outbox payloads', () {
    test('carry client_event_id, GPS, and occurred_at', () async {
      final controller = container.read(executionControllerProvider.notifier);
      final clientEventId = await controller.transition('wo-1', 'start');

      final payloads = await queued('transition');
      expect(payloads.single['client_event_id'], clientEventId);
      expect(payloads.single['event'], 'start');
      expect(payloads.single['latitude'], 6.43);
      expect(payloads.single['longitude'], 3.42);
      expect(payloads.single['occurred_at'], isNotNull);
    });

    test('GPS denial still queues the event', () async {
      location.point = null;
      final controller = container.read(executionControllerProvider.notifier);
      await controller.transition('wo-1', 'accept');

      final payloads = await queued('transition');
      expect(payloads.single.containsKey('latitude'), isFalse);
    });

    test('en route carries selected destination payload', () async {
      final controller = container.read(executionControllerProvider.notifier);
      await controller.transition(
        'wo-1',
        'en_route',
        payload: {
          'destination_type': 'cabinet',
          'destination_id': 'fdh-1',
          'destination_label': 'FDH 1',
        },
      );

      final payloads = await queued('transition');
      expect(payloads.single['event'], 'en_route');
      expect(
        (payloads.single['payload'] as Map)['destination_type'],
        'cabinet',
      );
      expect((payloads.single['payload'] as Map)['destination_label'], 'FDH 1');
    });

    test('arrived carries destination payload and GPS', () async {
      final controller = container.read(executionControllerProvider.notifier);
      await controller.transition(
        'wo-1',
        'arrived',
        payload: {
          'destination_type': 'customer',
          'destination_label': 'Customer site',
        },
      );

      final payloads = await queued('transition');
      expect(payloads.single['event'], 'arrived');
      expect(payloads.single['latitude'], 6.43);
      expect(
        (payloads.single['payload'] as Map)['destination_type'],
        'customer',
      );
    });
  });

  group('work order notes', () {
    test('addNote queues a technician note for the job', () async {
      final controller = container.read(executionControllerProvider.notifier);
      final outcome = await controller.addNote('wo-1', '  ONT replaced  ');

      final payloads = await queued('note');
      expect(payloads.single['work_order_id'], 'wo-1');
      expect(payloads.single['body'], 'ONT replaced');
      expect(payloads.single['is_internal'], isTrue);
      expect(payloads.single['attachment_ids'], isEmpty);
      expect(outcome.deliveryState, MutationDeliveryState.queued);

      final rows = await db.select(db.outboxEntries).get();
      expect(rows.single.clientRef, outcome.clientRef);
    });

    test('addNote rejects blank notes', () async {
      final controller = container.read(executionControllerProvider.notifier);
      await expectLater(controller.addNote('wo-1', '   '), throwsArgumentError);
    });

    test('addNote still succeeds when immediate flush fails', () async {
      await sync.dispose();
      final failingSync = _FlushFailingSyncService(
        db: db,
        api: sync.api,
        connectivity: connectivity,
        evidence: store.evidence,
      );
      sync = failingSync;
      container.dispose();
      container = ProviderContainer(
        overrides: [
          syncServiceProvider.overrideWithValue(failingSync),
          locationSourceProvider.overrideWithValue(location),
        ],
      );

      final controller = container.read(executionControllerProvider.notifier);
      final outcome = await controller.addNote('wo-1', '  ONT replaced  ');

      final payloads = await queued('note');
      expect(outcome.clientRef, isNotEmpty);
      expect(outcome.deliveryState, MutationDeliveryState.queued);
      expect(payloads.single['body'], 'ONT replaced');
    });

    test('addNote can queue an external technician note', () async {
      final controller = container.read(executionControllerProvider.notifier);
      await controller.addNote('wo-1', 'Customer update', isInternal: false);

      final payloads = await queued('note');
      expect(payloads.single['body'], 'Customer update');
      expect(payloads.single['is_internal'], isFalse);
    });
  });

  group('timer', () {
    test(
      'start opens local timer state; pause clears it without local worklog',
      () async {
        final controller = container.read(executionControllerProvider.notifier);
        await controller.transition('wo-1', 'start');
        expect(container.read(executionControllerProvider), isNotNull);

        await controller.transition('wo-1', 'pause');
        expect(container.read(executionControllerProvider), isNull);

        final transitions = await queued('transition');
        expect(transitions.last['event'], 'pause');
        expect(await queued('worklog'), isEmpty);
      },
    );

    test('complete also clears local timer without local worklog', () async {
      final controller = container.read(executionControllerProvider.notifier);
      await controller.transition('wo-1', 'start');
      await controller.transition('wo-1', 'complete');
      expect(container.read(executionControllerProvider), isNull);
      expect(await queued('worklog'), isEmpty);
    });

    test(
      'unable to complete queues a cancel event with reason and clears the timer',
      () async {
        final controller = container.read(executionControllerProvider.notifier);
        await controller.transition('wo-1', 'start');
        expect(container.read(executionControllerProvider), isNotNull);

        await controller.unableToComplete(
          'wo-1',
          reason: 'no_access',
          note: 'gate locked',
        );
        expect(
          container.read(executionControllerProvider),
          isNull,
        ); // timer cleared

        final event = (await queued(
          'transition',
        )).firstWhere((p) => p['event'] == 'unable_to_complete');
        expect((event['payload'] as Map)['reason'], 'no_access');
        expect(event['note'], 'gate locked');
      },
    );
  });

  group('completion gating consumes the server contract', () {
    test('blocks only on server-required photo and sign-off', () {
      var state = const CompletionState();
      expect(state.canComplete, isFalse);
      expect(state.blockers.length, 2);

      state = state.copyWith(photoCount: 1);
      expect(state.canComplete, isFalse);
      expect(state.blockers.single, contains('signature'));

      expect(state.copyWith(hasSignature: true).canComplete, isTrue);
    });

    test('advisory checklist never creates a client-only completion gate', () {
      final state = const CompletionState(photoCount: 1, hasSignature: true);

      expect(state.checklistDone, isFalse);
      expect(state.canComplete, isTrue);
      expect(state.blockers, isEmpty);
    });

    test(
      'disabled server evidence policy permits completion without evidence',
      () {
        const requirements = JobCompletionRequirements(
          evidenceRequired: false,
          minimumPhotoCount: 0,
          customerSignoffRequired: false,
          signatureUnavailableReasonAllowed: false,
        );

        const state = CompletionState(requirements: requirements);

        expect(state.canComplete, isTrue);
        expect(state.blockers, isEmpty);
      },
    );

    test('signature fallback reason satisfies sign-off', () {
      final state = const CompletionState(
        checklistDone: true,
        photoCount: 1,
      ).copyWith(signatureUnavailableReason: 'customer absent');
      expect(state.canComplete, isTrue);
      expect(
        state.transitionPayload['signature_unavailable_reason'],
        'customer absent',
      );
    });

    test('signer name is included in the completion payload', () {
      final state = const CompletionState(
        checklistDone: true,
        photoCount: 1,
        hasSignature: true,
      ).copyWith(signerName: '  Ada Customer  ');
      expect(state.canComplete, isTrue);
      expect(state.transitionPayload['signer_name'], 'Ada Customer');
    });

    test('whitespace-only fallback does not count', () {
      final state = const CompletionState(
        checklistDone: true,
        photoCount: 1,
      ).copyWith(signatureUnavailableReason: '   ');
      expect(state.canComplete, isFalse);
    });

    test('fallback does not satisfy a contract that disallows it', () {
      const requirements = JobCompletionRequirements(
        evidenceRequired: true,
        minimumPhotoCount: 1,
        customerSignoffRequired: true,
        signatureUnavailableReasonAllowed: false,
      );
      final state = const CompletionState(
        requirements: requirements,
        photoCount: 1,
      ).copyWith(signatureUnavailableReason: 'customer absent');

      expect(state.canComplete, isFalse);
      expect(state.blockers.single, 'Capture a customer signature');
    });
  });
}

class _FlushFailingSyncService extends SyncService {
  _FlushFailingSyncService({
    required super.db,
    required super.api,
    required super.connectivity,
    required super.evidence,
  });

  @override
  Future<int> flushOutbox() async {
    throw StateError('simulated immediate flush failure');
  }
}
