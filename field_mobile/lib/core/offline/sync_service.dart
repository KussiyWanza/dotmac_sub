import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:drift/drift.dart';

import '../api/api_client.dart';
import '../secure/evidence_files.dart';
import '../secure/store_work_gate.dart';
import 'connectivity.dart';
import 'database.dart';

/// Pluggable clock/delay so throttle behavior is testable without real time.
typedef DelayFn = Future<void> Function(Duration duration);

class OfflineRequestHistoryEntry {
  const OfflineRequestHistoryEntry({
    required this.clientRef,
    required this.kind,
    required this.status,
    required this.payload,
    required this.createdAt,
    this.lastError,
  });

  final String clientRef;
  final String kind;
  final String status;
  final Map<String, dynamic> payload;
  final DateTime createdAt;
  final String? lastError;
}

enum MutationDeliveryState { delivered, queued, failed }

class MutationDeliveryResult {
  const MutationDeliveryResult({
    required this.clientRef,
    required this.state,
    this.error,
  });

  final String clientRef;
  final MutationDeliveryState state;
  final String? error;
}

class OfflineNoteProjection {
  const OfflineNoteProjection({
    required this.clientRef,
    required this.body,
    required this.isInternal,
    required this.createdAt,
    required this.state,
    this.error,
  });

  final String clientRef;
  final String body;
  final bool isInternal;
  final DateTime createdAt;
  final MutationDeliveryState state;
  final String? error;
}

/// Maps an outbox entry kind to its API call.
class OutboxRouting {
  static (String method, String path) route(
    String kind,
    Map<String, dynamic> payload,
  ) {
    return switch (kind) {
      'transition' => (
        'POST',
        '/api/v1/field/jobs/${payload['work_order_id']}/transition',
      ),
      'note' => (
        'POST',
        '/api/v1/field/jobs/${payload['work_order_id']}/notes',
      ),
      'worklog' => (
        'POST',
        '/api/v1/field/jobs/${payload['work_order_id']}/worklogs',
      ),
      'material_consume' => (
        'POST',
        '/api/v1/field/jobs/${payload['work_order_id']}/materials/consume',
      ),
      'equipment' => (
        'POST',
        '/api/v1/field/jobs/${payload['work_order_id']}/equipment',
      ),
      'material_request' => ('POST', '/api/v1/field/material-requests/submit'),
      'expense_request' => ('POST', '/api/v1/field/expense-requests/submit'),
      _ => throw ArgumentError('Unknown outbox kind: $kind'),
    };
  }
}

/// Flushes the offline queues. Scope-bound: every read and write names the
/// scope, and every payload it stores or reads back is an envelope, so a queue
/// belonging to a principal who is no longer signed in cannot be flushed —
/// there is nothing for it to select and nothing it could open.
///
/// Flush ordering, retry policy and conflict handling are unchanged by the move
/// to encrypted storage; only the persistence beneath them is.
class SyncService {
  SyncService({
    required this.db,
    required this.api,
    required this.connectivity,
    required this.evidence,
    DelayFn? delay,
    this.throttle = const Duration(seconds: 1),
  }) : _delay = delay ?? Future.delayed {
    _subscription = connectivity.onlineChanges.listen((online) {
      if (online) requestBackgroundFlush();
    });
  }

  /// Upload evidence (photos + signatures) BEFORE outbox mutations, so a queued
  /// "complete" transition never reaches the server ahead of its attachments
  /// (which would trip the server's photo+signature completion gate).
  Future<void> flushAll() => _run(() async {
    await flushPhotos();
    await flushOutbox();
  });

  final AppDatabase db;
  final ApiClient api;
  final ConnectivitySource connectivity;
  final EvidenceFiles evidence;
  final Duration throttle;
  final DelayFn _delay;

  String get scopeKey => evidence.scopeKey;

  // After this many failed attempts a 5xx/network entry is parked as a
  // conflict so it can't block the FIFO queue indefinitely.
  static const _maxOutboxAttempts = 5;

  StreamSubscription<bool>? _subscription;
  bool _flushing = false;
  bool _disposed = false;
  Future<void>? _backgroundWork;

  Future<T> _run<T>(Future<T> Function() operation) => db.work.run(operation);

  /// Starts best-effort automatic work without surfacing the expected session
  /// teardown fence as an uncaught application error.
  void requestBackgroundFlush() {
    if (_disposed || _backgroundWork != null) return;
    late final Future<void> work;
    work = _backgroundFlush().whenComplete(() {
      if (identical(_backgroundWork, work)) _backgroundWork = null;
    });
    _backgroundWork = work;
    unawaited(work);
  }

  Future<void> waitForBackgroundWork() async {
    await _backgroundWork;
  }

  Future<void> _backgroundFlush() async {
    try {
      await flushAll();
    } on StoreDiscarded {
      // Logout/account switch won admission. A new session gets a new service
      // and its own startup flush; the retired store must not be reopened.
    }
  }

  Future<void> dispose() async {
    _disposed = true;
    await _subscription?.cancel();
    await waitForBackgroundWork();
  }

  // ---- Down-sync ---------------------------------------------------------

  Future<int> downSyncJobs() => _run(() async {
    final response = await api.dio.get(
      '/api/v1/field/jobs',
      queryParameters: {'limit': 200},
    );
    final items = (response.data['items'] as List).cast<Map>();
    await cacheJobs(items);
    return items.length;
  });

  /// Upsert job-list rows into the offline cache.
  Future<void> cacheJobs(List<Map> items) => _run(() async {
    final now = DateTime.now().toUtc();
    await db.batch((batch) {
      for (final item in items) {
        batch.insert(
          db.cachedJobs,
          CachedJobsCompanion.insert(
            scopeKey: scopeKey,
            id: item['id'] as String,
            title: item['title'] as String,
            status: item['status'] as String,
            workType: item['work_type'] as String,
            priority: item['priority'] as String,
            scheduledStart: Value(
              item['scheduled_start'] != null
                  ? DateTime.parse(item['scheduled_start'] as String)
                  : null,
            ),
            cachedAt: now,
          ),
          onConflict: DoUpdate(
            (old) => CachedJobsCompanion.custom(
              title: Constant(item['title'] as String),
              status: Constant(item['status'] as String),
              cachedAt: Constant(now),
            ),
          ),
        );
      }
    });
  });

  /// Cached job-list rows (optionally filtered by status), newest schedule first.
  Future<List<CachedJob>> readCachedJobs({String? status}) => _run(() async {
    final query = db.select(db.cachedJobs)
      ..where((row) => row.scopeKey.equals(scopeKey));
    if (status != null) {
      query.where((row) => row.status.equals(status));
    }
    query.orderBy([(row) => OrderingTerm.asc(row.scheduledStart)]);
    return query.get();
  });

  Future<void> cacheJobDetail(String jobId, Map<String, dynamic> detail) =>
      _run(() async {
        await (db.update(db.cachedJobs)..where(
              (row) => row.scopeKey.equals(scopeKey) & row.id.equals(jobId),
            ))
            .write(CachedJobsCompanion(detailJson: Value(jsonEncode(detail))));
      });

  /// Cached job-detail JSON, or null if not cached.
  Future<Map<String, dynamic>?> readCachedDetail(String jobId) =>
      _run(() async {
        final row =
            await (db.select(db.cachedJobs)..where(
                  (r) => r.scopeKey.equals(scopeKey) & r.id.equals(jobId),
                ))
                .getSingleOrNull();
        if (row?.detailJson == null) return null;
        return (jsonDecode(row!.detailJson!) as Map).cast<String, dynamic>();
      });

  Future<int> downSyncSchedule() => _run(() async {
    final response = await api.dio.get('/api/v1/field/schedule');
    final items = (response.data as List).cast<Map>();
    await cacheSchedule(items);
    return items.length;
  });

  /// Replace the cached schedule with the latest fetch. A full replace (rather
  /// than upsert) ensures entries dropped server-side don't linger offline.
  Future<void> cacheSchedule(List<Map> items) => _run(() async {
    await db.transaction(() async {
      await (db.delete(
        db.cachedScheduleEntries,
      )..where((row) => row.scopeKey.equals(scopeKey))).go();
      await db.batch((batch) {
        for (final item in items) {
          batch.insert(
            db.cachedScheduleEntries,
            CachedScheduleEntriesCompanion.insert(
              scopeKey: scopeKey,
              referenceId: item['reference_id'] as String,
              type: item['type'] as String,
              startAt: DateTime.parse(item['start_at'] as String),
              endAt: Value(
                item['end_at'] != null
                    ? DateTime.parse(item['end_at'] as String)
                    : null,
              ),
              title: item['title'] as String? ?? '',
            ),
            mode: InsertMode.insertOrReplace,
          );
        }
      });
    });
  });

  /// Cached schedule entries, earliest first.
  Future<List<CachedScheduleEntry>> readCachedSchedule() => _run(() async {
    final query = db.select(db.cachedScheduleEntries)
      ..where((row) => row.scopeKey.equals(scopeKey))
      ..orderBy([(row) => OrderingTerm.asc(row.startAt)]);
    return query.get();
  });

  // ---- Outbox ------------------------------------------------------------

  Future<void> enqueue({
    required String kind,
    required String clientRef,
    required Map<String, dynamic> payload,
  }) => _run(() async {
    await db
        .into(db.outboxEntries)
        .insert(
          OutboxEntriesCompanion.insert(
            scopeKey: scopeKey,
            clientRef: clientRef,
            kind: kind,
            payloadJson: _sealPayload(clientRef, payload),
            createdAt: DateTime.now().toUtc(),
          ),
          mode: InsertMode.insertOrIgnore, // retried enqueues are no-ops
        );
  });

  Future<List<OutboxEntry>> pending() => _run(
    () =>
        (db.select(db.outboxEntries)
              ..where(
                (row) =>
                    row.scopeKey.equals(scopeKey) &
                    row.status.equals('pending'),
              )
              ..orderBy([(row) => OrderingTerm.asc(row.seq)]))
            .get(),
  );

  Future<OutboxEntry?> outboxEntry(String clientRef) => _run(
    () =>
        (db.select(db.outboxEntries)..where(
              (row) =>
                  row.scopeKey.equals(scopeKey) &
                  row.clientRef.equals(clientRef),
            ))
            .getSingleOrNull(),
  );

  Future<MutationDeliveryResult> deliveryResult(String clientRef) =>
      _run(() async {
        final entry = await outboxEntry(clientRef);
        if (entry == null) {
          return MutationDeliveryResult(
            clientRef: clientRef,
            state: MutationDeliveryState.failed,
            error: 'Queued note could not be found',
          );
        }
        return MutationDeliveryResult(
          clientRef: clientRef,
          state: switch (entry.status) {
            'sent' => MutationDeliveryState.delivered,
            'conflict' => MutationDeliveryState.failed,
            _ => MutationDeliveryState.queued,
          },
          error: entry.lastError,
        );
      });

  /// Queued mutations carry the customer's own words and readings, so the
  /// payload column holds an envelope bound to this scope and this client ref.
  /// Nothing else can open it, which is also what stops a queue left behind by
  /// a previous account from ever being replayed.
  String _sealPayload(String clientRef, Map<String, dynamic> payload) =>
      evidence.cipher.sealText(
        jsonEncode(payload),
        context: evidence.contextFor('outbox', clientRef),
      );

  Map<String, dynamic> _openPayload(OutboxEntry entry) {
    final json = evidence.cipher.openText(
      entry.payloadJson,
      context: evidence.contextFor('outbox', entry.clientRef),
    );
    return (jsonDecode(json) as Map).cast<String, dynamic>();
  }

  Future<List<OfflineRequestHistoryEntry>> offlineRequestHistory(String kind) =>
      _run(() async {
        final rows =
            await (db.select(db.outboxEntries)
                  ..where(
                    (row) =>
                        row.scopeKey.equals(scopeKey) &
                        row.kind.equals(kind) &
                        row.status.isNotValue('sent'),
                  )
                  ..orderBy([(row) => OrderingTerm.desc(row.createdAt)]))
                .get();
        return [
          for (final row in rows)
            OfflineRequestHistoryEntry(
              clientRef: row.clientRef,
              kind: row.kind,
              status: row.status,
              payload: _openPayload(row),
              createdAt: row.createdAt,
              lastError: row.lastError,
            ),
        ];
      });

  Future<List<OfflineNoteProjection>> offlineNotesForJob(String workOrderId) =>
      _run(() async {
        final entries = await offlineRequestHistory('note');
        return [
          for (final entry in entries)
            if (entry.payload['work_order_id'] == workOrderId)
              OfflineNoteProjection(
                clientRef: entry.clientRef,
                body: entry.payload['body'] as String? ?? '',
                isInternal: entry.payload['is_internal'] as bool? ?? true,
                createdAt: entry.createdAt,
                state: entry.status == 'conflict'
                    ? MutationDeliveryState.failed
                    : MutationDeliveryState.queued,
                error: entry.lastError,
              ),
        ];
      });

  Future<List<PendingPhoto>> pendingPhotosForJob(String workOrderId) => _run(
    () =>
        (db.select(db.pendingPhotos)..where(
              (row) =>
                  row.scopeKey.equals(scopeKey) &
                  row.workOrderId.equals(workOrderId),
            ))
            .get(),
  );

  Future<void> removePendingPhoto(String clientRef) => _run(() async {
    final row =
        await (db.select(db.pendingPhotos)..where(
              (photo) =>
                  photo.scopeKey.equals(scopeKey) &
                  photo.clientRef.equals(clientRef),
            ))
            .getSingleOrNull();
    if (row == null || row.uploaded) return;
    await evidence.delete(File(row.localPath));
    await (db.delete(db.pendingPhotos)..where(
          (photo) =>
              photo.scopeKey.equals(scopeKey) &
              photo.clientRef.equals(clientRef),
        ))
        .go();
  });

  Future<bool> get isOnline => connectivity.isOnline;

  /// Flush pending entries FIFO. One failure stops the flush (order matters:
  /// a note may reference a transition); conflicts are parked, not dropped.
  Future<int> flushOutbox() => _run(() async {
    if (_flushing) return 0;
    if (!await connectivity.isOnline) return 0;
    _flushing = true;
    var sent = 0;
    try {
      for (final entry in await pending()) {
        final Map<String, dynamic> payload;
        try {
          payload = _openPayload(entry);
        } on Object {
          // The envelope will not open, so this entry belongs to key material
          // this device no longer holds. It can never be sent; park it for
          // review rather than blocking the FIFO queue behind it forever.
          await _mark(entry, 'conflict', error: 'Unreadable queued mutation');
          continue;
        }
        if (entry.kind == 'material_request' ||
            entry.kind == 'expense_request' ||
            entry.kind == 'note') {
          payload['client_ref'] = entry.clientRef;
        }
        final (method, path) = OutboxRouting.route(entry.kind, payload);
        try {
          await api.dio.request(
            path,
            data: payload,
            options: Options(method: method),
          );
          await _mark(entry, 'sent');
          sent++;
        } on DioException catch (error) {
          final status = error.response?.statusCode;
          if (status == 429) {
            final retryAfter =
                int.tryParse(
                  error.response?.headers.value('Retry-After') ?? '',
                ) ??
                5;
            await _delay(Duration(seconds: retryAfter));
            await _bumpAttempts(entry, 'rate limited');
            break; // re-flush later, keep FIFO order
          }
          if (status == 409) {
            // Structured conflict (job reassigned/cancelled): park it for
            // review. Evidence is never dropped.
            await _mark(entry, 'conflict', error: _detail(error));
            continue;
          }
          if (status != null && status >= 400 && status < 500) {
            // Permanent rejection: park as conflict for review.
            await _mark(entry, 'conflict', error: _detail(error));
            continue;
          }
          // Network/5xx trouble. Stop to preserve FIFO order and retry next
          // trigger — but cap attempts so one poison entry can't block the
          // whole queue forever; park it for review and let the rest drain.
          final attempts = entry.attempts + 1;
          if (attempts >= _maxOutboxAttempts) {
            await _mark(
              entry,
              'conflict',
              error: 'Gave up after $attempts attempts: ${_detail(error)}',
            );
            continue;
          }
          await _bumpAttempts(entry, _detail(error));
          break;
        }
        await _delay(throttle);
      }
    } finally {
      _flushing = false;
    }
    return sent;
  });

  // ---- Photo uploads -----------------------------------------------------

  bool _flushingPhotos = false;

  /// Upload queued photos as multipart to the attachments endpoint. The
  /// server dedupes on client_ref, so retries are safe. 4xx responses record
  /// the error but keep the file — evidence is never silently dropped.
  Future<int> flushPhotos() => _run(() async {
    if (_flushingPhotos) return 0;
    if (!await connectivity.isOnline) return 0;
    _flushingPhotos = true;
    var uploaded = 0;
    try {
      final rows =
          await (db.select(db.pendingPhotos)..where(
                (row) =>
                    row.scopeKey.equals(scopeKey) &
                    row.uploaded.equals(false) &
                    row.failed.equals(false),
              ))
              .get();
      for (final photo in rows) {
        final file = File(photo.localPath);
        if (!file.existsSync()) {
          // The file vanished (cleared cache): nothing left to upload.
          await _markPhoto(
            photo.clientRef,
            uploaded: true,
            error: 'local file missing',
          );
          continue;
        }
        final Uint8List image;
        try {
          image = await evidence.read(
            file,
            purpose: 'photo',
            reference: photo.clientRef,
          );
        } on Object {
          // An envelope we cannot open is evidence we can never upload. Mark it
          // terminally failed so it surfaces for review instead of retrying.
          await _markPhoto(
            photo.clientRef,
            uploaded: false,
            failed: true,
            error: 'Unreadable local evidence',
          );
          continue;
        }
        final form = FormData.fromMap({
          'file': MultipartFile.fromBytes(
            image,
            filename: 'photo.jpg',
            contentType: DioMediaType('image', 'jpeg'),
          ),
          'kind': photo.kind,
          'client_ref': photo.clientRef,
          'work_order_id': ?photo.workOrderId,
          'installation_project_id': ?photo.installationProjectId,
          'latitude': ?photo.latitude?.toString(),
          'longitude': ?photo.longitude?.toString(),
          'captured_at': photo.capturedAt.toIso8601String(),
        });
        try {
          await api.dio.post('/api/v1/field/attachments', data: form);
          await _markPhoto(photo.clientRef, uploaded: true);
          await evidence.delete(file);
          uploaded++;
        } on DioException catch (error) {
          final status = error.response?.statusCode;
          if (status != null &&
              status >= 400 &&
              status < 500 &&
              status != 429) {
            // Permanent rejection (bad MIME, too large, gone): terminal. Mark
            // failed so it's not retried forever; keep the file for review.
            await _markPhoto(
              photo.clientRef,
              uploaded: false,
              failed: true,
              error: _detail(error),
            );
            continue;
          }
          await _markPhoto(
            photo.clientRef,
            uploaded: false,
            error: _detail(error),
          );
          break; // network/server/rate trouble: retry on the next trigger
        }
        await _delay(throttle);
      }
    } finally {
      _flushingPhotos = false;
    }
    return uploaded;
  });

  Future<void> _markPhoto(
    String clientRef, {
    required bool uploaded,
    bool failed = false,
    String? error,
  }) async {
    await (db.update(db.pendingPhotos)..where(
          (row) =>
              row.scopeKey.equals(scopeKey) & row.clientRef.equals(clientRef),
        ))
        .write(
          PendingPhotosCompanion(
            uploaded: Value(uploaded),
            failed: Value(failed),
            lastError: Value(error),
          ),
        );
  }

  String _detail(DioException error) {
    final data = error.response?.data;
    if (data is Map && data['detail'] != null) return data['detail'].toString();
    if (data is String && data.trim().isNotEmpty) return data.trim();
    if (data != null) return data.toString();
    final status = error.response?.statusCode;
    if (status == 422) {
      return 'The server rejected this request. Refresh the job and check all completion prerequisites.';
    }
    if (status != null) return 'Request rejected with HTTP $status';
    return 'Could not reach the server. Check your connection and try again.';
  }

  Future<void> _mark(OutboxEntry entry, String status, {String? error}) async {
    await (db.update(db.outboxEntries)..where(
          (row) => row.scopeKey.equals(scopeKey) & row.seq.equals(entry.seq),
        ))
        .write(
          OutboxEntriesCompanion(
            status: Value(status),
            lastError: Value(error),
            attempts: Value(entry.attempts + 1),
          ),
        );
  }

  Future<void> _bumpAttempts(OutboxEntry entry, String error) async {
    await (db.update(db.outboxEntries)..where(
          (row) => row.scopeKey.equals(scopeKey) & row.seq.equals(entry.seq),
        ))
        .write(
          OutboxEntriesCompanion(
            attempts: Value(entry.attempts + 1),
            lastError: Value(error),
          ),
        );
  }
}
