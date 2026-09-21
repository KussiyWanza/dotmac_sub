import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:uuid/uuid.dart';

import '../../core/location/device_location.dart';
import '../../core/location/location_source.dart';
import '../../core/offline/sync_service.dart';

/// Real devices use geolocator; tests override with fakes. kIsWeb and
/// headless test binaries never construct the plugin path.
final locationSourceProvider = Provider<LocationSource>((ref) {
  if (kIsWeb) return const UnavailableLocation();
  return GeolocatorLocationSource();
});

/// Provided at app bootstrap once the drift database is opened.
final syncServiceProvider = Provider<SyncService>(
  (ref) => throw UnimplementedError('overridden at bootstrap'),
);

/// Lightweight local timer state. Server-side worklogs are authoritative.
class ActiveTimer {
  const ActiveTimer({required this.jobId, required this.startedAt});

  final String jobId;
  final DateTime startedAt;
}

class NoteSubmissionOutcome {
  const NoteSubmissionOutcome({
    required this.clientRef,
    required this.body,
    required this.isInternal,
    required this.deliveryState,
    this.error,
  });

  final String clientRef;
  final String body;
  final bool isInternal;
  final MutationDeliveryState deliveryState;
  final String? error;
}

class ExecutionController extends Notifier<ActiveTimer?> {
  @override
  ActiveTimer? build() => null;

  SyncService get _sync => ref.read(syncServiceProvider);

  static const _uuid = Uuid();

  /// Queue a job transition. Every event carries a client UUID (server-side
  /// idempotency) and best-effort GPS. Returns the client_event_id.
  Future<String> transition(
    String jobId,
    String event, {
    String? note,
    Map<String, dynamic>? payload,
  }) async {
    final clientEventId = _uuid.v4();
    final position = await ref.read(locationSourceProvider).current();
    await _sync.enqueue(
      kind: 'transition',
      clientRef: clientEventId,
      payload: {
        'work_order_id': jobId,
        'event': event,
        'client_event_id': clientEventId,
        'occurred_at': DateTime.now().toUtc().toIso8601String(),
        'latitude': ?position?.latitude,
        'longitude': ?position?.longitude,
        'note': ?note,
        'payload': ?payload,
      },
    );

    if (event == 'start' || event == 'resume') {
      state = ActiveTimer(jobId: jobId, startedAt: DateTime.now().toUtc());
    }
    if (event == 'pause' ||
        event == 'hold' ||
        event == 'complete' ||
        event == 'unable_to_complete') {
      final timer = state;
      if (timer != null && timer.jobId == jobId) {
        state = null;
      }
    }

    // Best-effort immediate delivery; offline entries stay queued.
    await _sync.flushOutbox();
    return clientEventId;
  }

  /// Record a failed visit (customer absent, no access, …). Cancels the job
  /// server-side with the reason; bypasses the completion-evidence gate.
  Future<String> unableToComplete(
    String jobId, {
    required String reason,
    String? note,
  }) {
    return transition(
      jobId,
      'unable_to_complete',
      note: note,
      payload: {'reason': reason},
    );
  }

  Future<NoteSubmissionOutcome> addNote(
    String jobId,
    String body, {
    bool isInternal = true,
    List<String> attachmentIds = const [],
  }) async {
    final trimmed = body.trim();
    if (trimmed.isEmpty) {
      throw ArgumentError.value(body, 'body', 'Note body is required');
    }
    final clientRef = _uuid.v4();
    await _sync.enqueue(
      kind: 'note',
      clientRef: clientRef,
      payload: {
        'work_order_id': jobId,
        'body': trimmed,
        'is_internal': isInternal,
        'attachment_ids': attachmentIds,
      },
    );
    try {
      await _sync.flushOutbox();
    } catch (_) {
      // The durable row below remains the outcome authority even when the
      // best-effort immediate flush itself fails unexpectedly.
    }
    final delivery = await _sync.deliveryResult(clientRef);
    return NoteSubmissionOutcome(
      clientRef: clientRef,
      body: trimmed,
      isInternal: isInternal,
      deliveryState: delivery.state,
      error: delivery.error,
    );
  }
}

final executionControllerProvider =
    NotifierProvider<ExecutionController, ActiveTimer?>(
      ExecutionController.new,
    );
