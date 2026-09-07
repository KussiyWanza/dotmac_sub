import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:uuid/uuid.dart';

import '../../app/status_presentation.dart';
import '../auth/auth_state.dart';
import '../../core/api/token_store.dart';
import '../expenses/expense_models.dart';

class ManagerProfile {
  const ManagerProfile({
    required this.name,
    required this.roles,
    required this.permissions,
    required this.isManager,
  });

  final String name;
  final List<String> roles;
  final List<String> permissions;
  final bool isManager;

  factory ManagerProfile.fromJson(Map<String, dynamic> json) => ManagerProfile(
    name: json['name']?.toString() ?? 'Manager',
    roles: _stringList(json['roles']),
    permissions: _stringList(json['permissions']),
    isManager: json['is_manager'] == true,
  );
}

class ManagerSummary {
  const ManagerSummary({
    required this.techniciansTotal,
    required this.techniciansLive,
    required this.techniciansSharing,
    required this.openJobs,
    required this.unassignedJobs,
    required this.pendingExpenses,
  });

  final int techniciansTotal;
  final int techniciansLive;
  final int techniciansSharing;
  final int openJobs;
  final int unassignedJobs;
  final int pendingExpenses;

  factory ManagerSummary.fromJson(Map<String, dynamic> json) => ManagerSummary(
    techniciansTotal: _int(json['technicians_total']),
    techniciansLive: _int(json['technicians_live']),
    techniciansSharing: _int(json['technicians_sharing']),
    openJobs: _int(json['open_jobs']),
    unassignedJobs: _int(json['unassigned_jobs']),
    pendingExpenses: _int(json['pending_expenses']),
  );
}

class ManagerTechnician {
  const ManagerTechnician({
    required this.personId,
    required this.name,
    required this.status,
    required this.locationSharingEnabled,
    required this.isLive,
    this.technicianId,
    this.title,
    this.region,
    this.latitude,
    this.longitude,
    this.accuracyM,
    this.lastLocationAt,
    this.activeWorkOrderTitle,
    this.activeWorkOrderStatus,
    this.activeWorkOrderStatusPresentation,
  });

  final String personId;
  final String? technicianId;
  final String name;
  final String status;
  final String? title;
  final String? region;
  final bool locationSharingEnabled;
  final bool isLive;
  final double? latitude;
  final double? longitude;
  final double? accuracyM;
  final DateTime? lastLocationAt;
  final String? activeWorkOrderTitle;
  final String? activeWorkOrderStatus;
  final StatusPresentation? activeWorkOrderStatusPresentation;

  factory ManagerTechnician.fromJson(Map<String, dynamic> json) {
    final activeWork = (json['active_work_order'] as Map?)
        ?.cast<String, dynamic>();
    return ManagerTechnician(
      personId: json['person_id']?.toString() ?? '',
      technicianId: json['technician_id']?.toString(),
      name: json['person_label']?.toString() ?? 'Technician',
      status: json['status']?.toString() ?? 'off_shift',
      title: json['title']?.toString(),
      region: json['region']?.toString(),
      locationSharingEnabled: json['location_sharing_enabled'] == true,
      isLive: json['is_live'] == true,
      latitude: _double(json['last_latitude'] ?? json['latitude']),
      longitude: _double(json['last_longitude'] ?? json['longitude']),
      accuracyM: _double(json['accuracy_m']),
      lastLocationAt: _date(json['last_location_at']),
      activeWorkOrderTitle: activeWork?['title']?.toString(),
      activeWorkOrderStatus: activeWork?['status']?.toString(),
      activeWorkOrderStatusPresentation: activeWork == null
          ? null
          : StatusPresentation.fromJsonOrFallback(
              activeWork['status_presentation'],
              activeWork['status']?.toString() ?? 'unknown',
            ),
    );
  }
}

class ManagerJob {
  ManagerJob({
    required this.id,
    required this.title,
    required this.status,
    required this.priority,
    required this.workType,
    this.scheduledStart,
    this.scheduledEnd,
    this.assignmentQueueId,
    this.assignedToPersonId,
    this.assignedToLabel,
    this.subscriberLabel,
    this.addressText,
    this.latitude,
    this.longitude,
    StatusPresentation? statusPresentation,
  }) : statusPresentation =
           statusPresentation ?? StatusPresentation.neutralFallback(status);

  final String id;
  final String title;
  final String status;
  final StatusPresentation statusPresentation;
  final String priority;
  final String workType;
  final DateTime? scheduledStart;
  final DateTime? scheduledEnd;
  final String? assignmentQueueId;
  final String? assignedToPersonId;
  final String? assignedToLabel;
  final String? subscriberLabel;
  final String? addressText;
  final double? latitude;
  final double? longitude;

  factory ManagerJob.fromJson(Map<String, dynamic> json) => ManagerJob(
    id: json['id']?.toString() ?? '',
    title: json['title']?.toString() ?? 'Work order',
    status: json['status']?.toString() ?? 'scheduled',
    statusPresentation: StatusPresentation.fromJsonOrFallback(
      json['status_presentation'],
      json['status']?.toString() ?? 'scheduled',
    ),
    priority: json['priority']?.toString() ?? 'normal',
    workType: json['work_type']?.toString() ?? 'other',
    scheduledStart: _date(json['scheduled_start']),
    scheduledEnd: _date(json['scheduled_end']),
    assignmentQueueId: json['assignment_queue_id']?.toString(),
    assignedToPersonId: json['assigned_to_person_id']?.toString(),
    assignedToLabel: json['assigned_to_label']?.toString(),
    subscriberLabel: json['subscriber_label']?.toString(),
    addressText: json['address_text']?.toString(),
    latitude: _double(json['latitude']),
    longitude: _double(json['longitude']),
  );
}

class ManagerAssignmentUpdate {
  const ManagerAssignmentUpdate({required this.id, required this.status});

  final String id;
  final String status;

  factory ManagerAssignmentUpdate.fromJson(Map<String, dynamic> json) =>
      ManagerAssignmentUpdate(
        id: json['id']?.toString() ?? '',
        status: json['status']?.toString() ?? 'unknown',
      );
}

class ExpenseApprovalResult {
  const ExpenseApprovalResult({
    required this.id,
    required this.status,
    required this.erpSyncStatus,
    this.erpSyncEventId,
    this.erpSyncError,
  });

  final String id;
  final String status;
  final String erpSyncStatus;
  final String? erpSyncEventId;
  final String? erpSyncError;

  factory ExpenseApprovalResult.fromJson(Map<String, dynamic> json) =>
      ExpenseApprovalResult(
        id: json['id']?.toString() ?? '',
        status: json['status']?.toString() ?? 'approved',
        erpSyncStatus: json['erp_sync_status']?.toString() ?? 'not_queued',
        erpSyncEventId: json['erp_sync_event_id']?.toString(),
        erpSyncError: json['erp_sync_error']?.toString(),
      );
}

class ManagerRepository {
  const ManagerRepository(this._ref);

  final Ref _ref;

  Future<ManagerProfile?> fetchProfile() async {
    final auth = _ref.read(authControllerProvider);
    if (auth is! Authenticated || auth.mode != LoginMode.staff) return null;
    try {
      final response = await _ref
          .read(apiClientProvider)
          .dio
          .get('/api/v1/field/manager/me');
      return ManagerProfile.fromJson(
        (response.data as Map).cast<String, dynamic>(),
      );
    } on DioException catch (error) {
      if (error.response?.statusCode == 401 ||
          error.response?.statusCode == 403 ||
          error.response?.statusCode == 404) {
        return null;
      }
      rethrow;
    }
  }

  Future<ManagerSummary> fetchSummary() async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/manager/summary');
    return ManagerSummary.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<List<ManagerTechnician>> fetchTechnicians() async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/manager/technicians');
    return _items(response.data).map(ManagerTechnician.fromJson).toList();
  }

  Future<List<ManagerJob>> fetchJobs() async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/manager/jobs');
    return _items(response.data).map(ManagerJob.fromJson).toList();
  }

  Future<void> assignJob({
    required String jobId,
    required String personId,
  }) async {
    await _ref
        .read(apiClientProvider)
        .dio
        .post(
          '/api/v1/field/manager/jobs/$jobId/assign',
          data: {'person_id': personId},
        );
  }

  Future<ManagerAssignmentUpdate> unassignJob({
    required String assignmentQueueId,
    required String reason,
  }) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .post(
          '/api/v1/field/manager/assignments/$assignmentQueueId/unassign',
          data: {'reason': reason},
        );
    return ManagerAssignmentUpdate.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<List<ExpenseRequest>> fetchExpenses() async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/manager/expenses');
    return _items(response.data).map(ExpenseRequest.fromJson).toList();
  }

  Future<ExpenseApprovalResult> approveExpense(String id) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .post(
          '/api/v1/field/manager/expenses/$id/approve',
          options: Options(headers: {'X-Request-ID': const Uuid().v4()}),
        );
    return ExpenseApprovalResult.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<void> rejectExpense(String id, String reason) async {
    await _ref
        .read(apiClientProvider)
        .dio
        .post(
          '/api/v1/field/manager/expenses/$id/reject',
          data: {'reason': reason},
        );
  }
}

final managerRepositoryProvider = Provider<ManagerRepository>(
  ManagerRepository.new,
);

final managerProfileProvider = FutureProvider<ManagerProfile?>((ref) {
  ref.watch(authControllerProvider);
  return ref.watch(managerRepositoryProvider).fetchProfile();
});

final managerSummaryProvider = FutureProvider<ManagerSummary>(
  (ref) => ref.watch(managerRepositoryProvider).fetchSummary(),
);

final managerTechniciansProvider = FutureProvider<List<ManagerTechnician>>(
  (ref) => ref.watch(managerRepositoryProvider).fetchTechnicians(),
);

final managerJobsProvider = FutureProvider<List<ManagerJob>>(
  (ref) => ref.watch(managerRepositoryProvider).fetchJobs(),
);

final managerExpensesProvider = FutureProvider<List<ExpenseRequest>>(
  (ref) => ref.watch(managerRepositoryProvider).fetchExpenses(),
);

bool isManagerProfile(AsyncValue<ManagerProfile?> value) =>
    value.valueOrNull?.isManager == true;

List<String> _stringList(Object? raw) {
  if (raw is! List) return const [];
  return [for (final item in raw) item.toString()];
}

List<Map<String, dynamic>> _items(Object? data) {
  if (data is Map && data['items'] is List) {
    return _mapItems(data['items']);
  }
  if (data is List) return _mapItems(data);
  return const [];
}

List<Map<String, dynamic>> _mapItems(Object? raw) {
  if (raw is! List) return const [];
  return [
    for (final item in raw)
      if (item is Map) item.cast<String, dynamic>(),
  ];
}

int _int(Object? value) => switch (value) {
  int() => value,
  num() => value.toInt(),
  String() => int.tryParse(value) ?? 0,
  _ => 0,
};

double? _double(Object? value) => switch (value) {
  num() => value.toDouble(),
  String() => double.tryParse(value),
  _ => null,
};

DateTime? _date(Object? value) =>
    value is String ? DateTime.tryParse(value) : null;
