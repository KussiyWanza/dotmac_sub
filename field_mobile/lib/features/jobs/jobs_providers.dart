import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/offline/database.dart';
import '../auth/auth_state.dart';
import '../execution/execution_controller.dart';
import 'job_models.dart';

class MeSummary {
  const MeSummary({
    required this.name,
    required this.openJobs,
    required this.completedToday,
  });

  final String name;
  final int openJobs;
  final int completedToday;
}

/// A job list plus whether it came from the offline cache (drives the banner).
class JobList {
  const JobList(this.jobs, {this.fromCache = false});

  final List<JobSummary> jobs;
  final bool fromCache;
}

JobSummary _summaryFromCache(CachedJob row) => JobSummary(
  id: row.id,
  title: row.title,
  status: row.status,
  workType: row.workType,
  priority: row.priority,
  scheduledStart: row.scheduledStart,
);

class JobsRepository {
  JobsRepository(this._read);

  final Ref _read;

  Future<MeSummary> fetchMe() async {
    final response = await _read
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/me');
    final data = (response.data as Map).cast<String, dynamic>();
    return MeSummary(
      name: data['name'] as String? ?? '',
      openJobs: data['open_jobs'] as int? ?? 0,
      completedToday: data['completed_today'] as int? ?? 0,
    );
  }

  Future<JobList> fetchJobs({
    String? status,
    DateTime? dateFrom,
    DateTime? dateTo,
  }) async {
    final sync = _read.read(syncServiceProvider);
    try {
      final response = await _read
          .read(apiClientProvider)
          .dio
          .get(
            '/api/v1/field/jobs',
            queryParameters: {
              'status': ?status,
              'from': ?dateFrom?.toUtc().toIso8601String(),
              'to': ?dateTo?.toUtc().toIso8601String(),
              'limit': 200,
            },
          );
      final items = (response.data['items'] as List).cast<Map>();
      await sync.cacheJobs(items); // keep the offline cache warm
      return JobList(
        items
            .map((item) => JobSummary.fromJson(item.cast<String, dynamic>()))
            .toList(),
      );
    } on DioException {
      // Offline / server unreachable: serve the cache so the tech still works.
      final cached = await sync.readCachedJobs(status: status);
      if (cached.isEmpty) rethrow;
      return JobList(cached.map(_summaryFromCache).toList(), fromCache: true);
    }
  }

  Future<JobDetail> fetchDetail(String jobId) async {
    final sync = _read.read(syncServiceProvider);
    try {
      final response = await _read
          .read(apiClientProvider)
          .dio
          .get('/api/v1/field/jobs/$jobId');
      final data = (response.data as Map).cast<String, dynamic>();
      await sync.cacheJobDetail(jobId, data);
      return JobDetail.fromJson(data);
    } on DioException {
      final cached = await sync.readCachedDetail(jobId);
      if (cached == null) rethrow;
      return JobDetail.fromJson(cached);
    }
  }

  Future<List<JobDestination>> fetchDestinations(String jobId) async {
    final response = await _read
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/jobs/$jobId/destinations');
    final data = (response.data as Map).cast<String, dynamic>();
    final items = (data['items'] as List? ?? const []).cast<Map>();
    return items
        .map((item) => JobDestination.fromJson(item.cast<String, dynamic>()))
        .toList();
  }

  Future<JobChatThread> fetchChat(String jobId) async {
    final response = await _read
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/jobs/$jobId/chat');
    final data = (response.data as Map).cast<String, dynamic>();
    return JobChatThread.fromJson(data);
  }

  Future<JobChatMessage> sendChatMessage(String jobId, String body) async {
    final response = await _read
        .read(apiClientProvider)
        .dio
        .post('/api/v1/field/jobs/$jobId/chat/messages', data: {'body': body});
    final data = (response.data as Map).cast<String, dynamic>();
    return JobChatMessage.fromJson(data);
  }

  Future<JobLocation> updateLocation({
    required String jobId,
    required double latitude,
    required double longitude,
  }) async {
    final sync = _read.read(syncServiceProvider);
    final response = await _read
        .read(apiClientProvider)
        .dio
        .patch(
          '/api/v1/field/jobs/$jobId/location',
          data: {'latitude': latitude, 'longitude': longitude},
        );
    final data = (response.data as Map).cast<String, dynamic>();
    final locationData = ((data['location'] as Map?) ?? data)
        .cast<String, dynamic>();
    final location = JobLocation.fromJson(locationData);

    final cached = await sync.readCachedDetail(jobId);
    if (cached != null) {
      cached['location'] = location.toJson();
      await sync.cacheJobDetail(jobId, cached);
    }
    return location;
  }
}

final jobsRepositoryProvider = Provider<JobsRepository>(JobsRepository.new);

final meProvider = FutureProvider<MeSummary>((ref) {
  ref.watch(authControllerProvider);
  return ref.watch(jobsRepositoryProvider).fetchMe();
});

final jobsFilterProvider = StateProvider<String?>((ref) => null);

final jobsListProvider = FutureProvider<JobList>((ref) {
  final filter = ref.watch(jobsFilterProvider);
  return ref.watch(jobsRepositoryProvider).fetchJobs(status: filter);
});

final todayJobsProvider = FutureProvider<JobList>((ref) async {
  final filter = ref.watch(jobsFilterProvider);
  final now = DateTime.now();
  final start = DateTime(now.year, now.month, now.day);
  final end = start.add(const Duration(days: 1));
  final list = await ref
      .watch(jobsRepositoryProvider)
      .fetchJobs(status: filter, dateTo: end);
  return JobList(
    list.jobs.where((job) => isActionableOnDay(job, start)).toList(),
    fromCache: list.fromCache,
  );
});

final allAssignedJobsProvider = FutureProvider<JobList>((ref) {
  return ref.watch(jobsRepositoryProvider).fetchJobs();
});

final jobDetailProvider = FutureProvider.family<JobDetail, String>(
  (ref, jobId) => ref.watch(jobsRepositoryProvider).fetchDetail(jobId),
);

final jobDestinationsProvider =
    FutureProvider.family<List<JobDestination>, String>(
      (ref, jobId) =>
          ref.watch(jobsRepositoryProvider).fetchDestinations(jobId),
    );

final jobChatProvider = FutureProvider.family<JobChatThread, String>(
  (ref, jobId) => ref.watch(jobsRepositoryProvider).fetchChat(jobId),
);

bool _isSameLocalDay(DateTime? value, DateTime day) {
  if (value == null) return false;
  final local = value.toLocal();
  return local.year == day.year &&
      local.month == day.month &&
      local.day == day.day;
}

/// Work that belongs in the technician's actionable Today view.
///
/// Unscheduled and overdue open work must remain visible until completed.
/// Terminal work is shown only on the day it completed, keeping the list
/// aligned with the open/completed counters returned by `/field/me`.
bool isActionableOnDay(JobSummary job, DateTime day) {
  if (job.status == 'completed') {
    return _isSameLocalDay(job.completedAt, day);
  }
  if (job.status == 'canceled') return false;
  final scheduled = job.scheduledStart?.toLocal();
  if (scheduled == null) return true;
  final end = DateTime(
    day.year,
    day.month,
    day.day,
  ).add(const Duration(days: 1));
  return scheduled.isBefore(end);
}
