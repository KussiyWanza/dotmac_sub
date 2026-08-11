import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:drift/drift.dart';

import '../api/api_client.dart';
import 'connectivity.dart';
import 'database.dart';

/// Pluggable clock/delay so throttle behavior is testable without real time.
typedef DelayFn = Future<void> Function(Duration duration);

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
      'material_request' => ('POST', '/api/v1/field/material-requests'),
      'expense_request' => ('POST', '/api/v1/field/expense-requests'),
      _ => throw ArgumentError('Unknown outbox kind: $kind'),
    };
  }
}

class SyncService {
  SyncService({
    required this.db,
    required this.api,
    required this.connectivity,
    DelayFn? delay,
    this.throttle = const Duration(seconds: 1),
  }) : _delay = delay ?? Future.delayed {
    _subscription = connectivity.onlineChanges.listen((online) {
      if (online) unawaited(flushAll());
    });
  }

  /// Upload evidence (photos + signatures) BEFORE outbox mutations, so a queued
  /// "complete" transition never reaches the server ahead of its attachments
  /// (which would trip the server's photo+signature completion gate).
  Future<void> flushAll() async {
    await flushPhotos();
    await flushOutbox();
  }

  final AppDatabase db;
  final ApiClient api;
  final ConnectivitySource connectivity;
  final Duration throttle;
  final DelayFn _delay;

  // After this many failed attempts a 5xx/network entry is parked as a
  // conflict so it can't block the FIFO queue indefinitely.
  static const _maxOutboxAttempts = 5;

  StreamSubscription<bool>? _subscription;
  bool _flushing = false;

  Future<void> dispose() async {
    await _subscription?.cancel();
  }

  // ---- Down-sync ---------------------------------------------------------

  Future<int> downSyncJobs() async {
    final response = await api.dio.get(
      '/api/v1/field/jobs',
      queryParameters: {'limit': 200},
    );
    final items = (response.data['items'] as List).cast<Map>();
    await cacheJobs(items);
    return items.length;
  }

  /// Upsert job-list rows into the offline cache.
  Future<void> cacheJobs(List<Map> items) async {
    final now = DateTime.now().toUtc();
    await db.batch((batch) {
      for (final item in items) {
        batch.insert(
          db.cachedJobs,
          CachedJobsCompanion.insert(
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
  }

  /// Cached job-list rows (optionally filtered by status), newest schedule first.
  Future<List<CachedJob>> readCachedJobs({String? status}) async {
    final query = db.select(db.cachedJobs);
    if (status != null) {
      query.where((row) => row.status.equals(status));
    }
    query.orderBy([(row) => OrderingTerm.asc(row.scheduledStart)]);
    return query.get();
  }

  Future<void> cacheJobDetail(String jobId, Map<String, dynamic> detail) async {
    await (db.update(db.cachedJobs)..where((row) => row.id.equals(jobId)))
        .write(CachedJobsCompanion(detailJson: Value(jsonEncode(detail))));
  }

  /// Cached job-detail JSON, or null if not cached.
  Future<Map<String, dynamic>?> readCachedDetail(String jobId) async {
    final row = await (db.select(
      db.cachedJobs,
    )..where((r) => r.id.equals(jobId))).getSingleOrNull();
    if (row?.detailJson == null) return null;
    return (jsonDecode(row!.detailJson!) as Map).cast<String, dynamic>();
  }

  Future<int> downSyncSchedule() async {
    final response = await api.dio.get('/api/v1/field/schedule');
    final items = (response.data as List).cast<Map>();
    await cacheSchedule(items);
    return items.length;
  }

  /// Replace the cached schedule with the latest fetch. A full replace (rather
  /// than upsert) ensures entries dropped server-side don't linger offline.
  Future<void> cacheSchedule(List<Map> items) async {
    await db.transaction(() async {
      await db.delete(db.cachedScheduleEntries).go();
      await db.batch((batch) {
        for (final item in items) {
          batch.insert(
            db.cachedScheduleEntries,
            CachedScheduleEntriesCompanion.insert(
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
  }

  /// Cached schedule entries, earliest first.
  Future<List<CachedScheduleEntry>> readCachedSchedule() async {
    final query = db.select(db.cachedScheduleEntries)
      ..orderBy([(row) => OrderingTerm.asc(row.startAt)]);
    return query.get();
  }

  // ---- Outbox ------------------------------------------------------------

  Future<void> enqueue({
    required String kind,
    required String clientRef,
    required Map<String, dynamic> payload,
  }) async {
    await db
        .into(db.outboxEntries)
        .insert(
          OutboxEntriesCompanion.insert(
            clientRef: clientRef,
            kind: kind,
            payloadJson: jsonEncode(payload),
            createdAt: DateTime.now().toUtc(),
          ),
          mode: InsertMode.insertOrIgnore, // retried enqueues are no-ops
        );
  }

  Future<List<OutboxEntry>> pending() =>
      (db.select(db.outboxEntries)
            ..where((row) => row.status.equals('pending'))
            ..orderBy([(row) => OrderingTerm.asc(row.seq)]))
          .get();

  Future<OutboxEntry?> outboxEntry(String clientRef) => (db.select(
    db.outboxEntries,
  )..where((row) => row.clientRef.equals(clientRef))).getSingleOrNull();

  Future<List<PendingPhoto>> pendingPhotosForJob(String workOrderId) =>
      (db.select(
        db.pendingPhotos,
      )..where((row) => row.workOrderId.equals(workOrderId))).get();

  Future<void> removePendingPhoto(String clientRef) async {
    final row = await (db.select(
      db.pendingPhotos,
    )..where((photo) => photo.clientRef.equals(clientRef))).getSingleOrNull();
    if (row == null || row.uploaded) return;
    final file = File(row.localPath);
    if (file.existsSync()) {
      try {
        file.deleteSync();
      } on FileSystemException {
        // Cache-file cleanup is best effort.
      }
    }
    await (db.delete(
      db.pendingPhotos,
    )..where((photo) => photo.clientRef.equals(clientRef))).go();
  }

  Future<bool> get isOnline => connectivity.isOnline;

  /// Flush pending entries FIFO. One failure stops the flush (order matters:
  /// a note may reference a transition); conflicts are parked, not dropped.
  Future<int> flushOutbox() async {
    if (_flushing) return 0;
    if (!await connectivity.isOnline) return 0;
    _flushing = true;
    var sent = 0;
    try {
      for (final entry in await pending()) {
        final payload = (jsonDecode(entry.payloadJson) as Map)
            .cast<String, dynamic>();
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
  }

  // ---- Photo uploads -----------------------------------------------------

  bool _flushingPhotos = false;

  /// Upload queued photos as multipart to the attachments endpoint. The
  /// server dedupes on client_ref, so retries are safe. 4xx responses record
  /// the error but keep the file — evidence is never silently dropped.
  Future<int> flushPhotos() async {
    if (_flushingPhotos) return 0;
    if (!await connectivity.isOnline) return 0;
    _flushingPhotos = true;
    var uploaded = 0;
    try {
      final rows =
          await (db.select(db.pendingPhotos)..where(
                (row) => row.uploaded.equals(false) & row.failed.equals(false),
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
        final form = FormData.fromMap({
          'file': MultipartFile.fromBytes(
            await file.readAsBytes(),
            filename: 'photo.jpg',
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
          try {
            file.deleteSync();
          } on FileSystemException {
            // Cleanup is best-effort; the row is already marked uploaded.
          }
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
  }

  Future<void> _markPhoto(
    String clientRef, {
    required bool uploaded,
    bool failed = false,
    String? error,
  }) async {
    await (db.update(
      db.pendingPhotos,
    )..where((row) => row.clientRef.equals(clientRef))).write(
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
    if (status != null) return 'Request rejected with HTTP $status';
    return error.message ?? 'request failed';
  }

  Future<void> _mark(OutboxEntry entry, String status, {String? error}) async {
    await (db.update(
      db.outboxEntries,
    )..where((row) => row.seq.equals(entry.seq))).write(
      OutboxEntriesCompanion(
        status: Value(status),
        lastError: Value(error),
        attempts: Value(entry.attempts + 1),
      ),
    );
  }

  Future<void> _bumpAttempts(OutboxEntry entry, String error) async {
    await (db.update(
      db.outboxEntries,
    )..where((row) => row.seq.equals(entry.seq))).write(
      OutboxEntriesCompanion(
        attempts: Value(entry.attempts + 1),
        lastError: Value(error),
      ),
    );
  }
}
