import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:geolocator/geolocator.dart';
import 'package:uuid/uuid.dart';

import '../../core/api/api_client.dart';
import '../../core/location/device_location.dart';
import '../auth/auth_state.dart';
import 'attendance_models.dart';

abstract class AttendanceLocationSource {
  Future<AttendancePosition?> current();

  Future<bool> isReady();

  Future<void> requestAccess();
}

class DeviceAttendanceLocationSource implements AttendanceLocationSource {
  @override
  Future<bool> isReady() async {
    try {
      if (!await Geolocator.isLocationServiceEnabled()) return false;
      final permission = await Geolocator.checkPermission();
      return decideForPermission(permission, serviceEnabled: true) ==
          LocationDecision.proceed;
    } catch (_) {
      return false;
    }
  }

  @override
  Future<void> requestAccess() async {
    try {
      if (!await Geolocator.isLocationServiceEnabled()) {
        await Geolocator.openLocationSettings();
        return;
      }
      final permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        await Geolocator.requestPermission();
      } else if (permission == LocationPermission.deniedForever) {
        await Geolocator.openAppSettings();
      }
    } catch (_) {
      // The card remains fail-closed and will explain the requirement again
      // when the engineer attempts an attendance or Shift action.
    }
  }

  @override
  Future<AttendancePosition?> current() async {
    try {
      final serviceEnabled = await Geolocator.isLocationServiceEnabled();
      var permission = await Geolocator.checkPermission();
      var decision = decideForPermission(
        permission,
        serviceEnabled: serviceEnabled,
      );
      if (decision == LocationDecision.request) {
        permission = await Geolocator.requestPermission();
        decision = decideForPermission(
          permission,
          serviceEnabled: serviceEnabled,
        );
      }
      if (decision != LocationDecision.proceed) return null;
      final position = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 10),
        ),
      );
      return AttendancePosition(
        latitude: position.latitude,
        longitude: position.longitude,
        accuracyM: position.accuracy,
        observedAt: position.timestamp,
      );
    } catch (_) {
      return null;
    }
  }
}

abstract class AttendanceRepositoryContract {
  Future<AttendanceView> today();

  Future<AttendanceView> punch(AttendanceAction action);
}

class AttendanceRepository implements AttendanceRepositoryContract {
  AttendanceRepository({
    required this.api,
    required this.location,
    this._uuid = const Uuid(),
  });

  final ApiClient api;
  final AttendanceLocationSource location;
  final Uuid _uuid;

  @override
  Future<AttendanceView> today() async {
    try {
      final response = await api.dio.get('/api/v1/field/attendance');
      return _view(response.data);
    } on DioException catch (error) {
      throw _failure(error);
    } on FormatException {
      throw const AttendanceFailure(
        'invalid_provider_response',
        'Attendance is temporarily unavailable.',
        unavailable: true,
      );
    }
  }

  @override
  Future<AttendanceView> punch(AttendanceAction action) async {
    final position = await location.current();
    if (position == null) {
      throw const AttendanceFailure(
        'location_required',
        'Location access is required to record attendance.',
      );
    }
    try {
      final response = await api.dio.post(
        '/api/v1/field/attendance/${action.apiPath}',
        data: position.toJson(),
        options: Options(headers: {'Idempotency-Key': _uuid.v4()}),
      );
      return _view(response.data);
    } on DioException catch (error) {
      throw _failure(error);
    } on FormatException {
      throw const AttendanceFailure(
        'invalid_provider_response',
        'Attendance is temporarily unavailable.',
        unavailable: true,
      );
    }
  }

  AttendanceView _view(Object? data) {
    if (data is! Map) {
      throw const FormatException('Invalid attendance response');
    }
    return AttendanceView.fromJson(Map<String, Object?>.from(data));
  }

  AttendanceFailure _failure(DioException error) {
    final data = error.response?.data;
    final detail = data is Map ? data['detail'] : null;
    if (detail is Map) {
      final code = detail['code']?.toString();
      final message = detail['message']?.toString();
      if (code != null &&
          code.isNotEmpty &&
          message != null &&
          message.isNotEmpty) {
        return AttendanceFailure(
          code,
          message,
          unavailable: error.response?.statusCode == 503,
        );
      }
    }
    return AttendanceFailure(
      'attendance_unavailable',
      'Attendance is temporarily unavailable. Please try again.',
      unavailable: true,
    );
  }
}

final attendanceLocationSourceProvider = Provider<AttendanceLocationSource>(
  (ref) => DeviceAttendanceLocationSource(),
);

final locationReminderShownProvider = StateProvider<bool>((ref) => false);

final attendanceRepositoryProvider = Provider<AttendanceRepositoryContract>(
  (ref) => AttendanceRepository(
    api: ref.watch(apiClientProvider),
    location: ref.watch(attendanceLocationSourceProvider),
  ),
);

class AttendanceController extends AutoDisposeAsyncNotifier<AttendanceView> {
  @override
  Future<AttendanceView> build() =>
      ref.watch(attendanceRepositoryProvider).today();

  Future<AttendanceView> punch(AttendanceAction action) async {
    final view = await ref.read(attendanceRepositoryProvider).punch(action);
    state = AsyncData(view);
    return view;
  }

  Future<void> refresh() async {
    state = const AsyncLoading();
    state = await AsyncValue.guard(
      () => ref.read(attendanceRepositoryProvider).today(),
    );
  }
}

final attendanceControllerProvider =
    AsyncNotifierProvider.autoDispose<AttendanceController, AttendanceView>(
      AttendanceController.new,
    );
