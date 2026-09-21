import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/features/attendance/attendance_models.dart';
import 'package:dotmac_field/features/attendance/attendance_repository.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

class _AttendanceLocation implements AttendanceLocationSource {
  _AttendanceLocation(this.position);

  AttendancePosition? position;
  bool accessRequested = false;

  @override
  Future<AttendancePosition?> current() async => position;

  @override
  Future<bool> isReady() async => position != null;

  @override
  Future<void> requestAccess() async => accessRequested = true;
}

Map<String, Object?> _response(String state) => {
  'state': state,
  'attendance_date': '2026-09-09',
  'timezone': 'Africa/Lagos',
  'check_in_at': state == 'not_checked_in' ? null : '2026-09-09T07:30:00+00:00',
  'check_out_at': state == 'checked_out' ? '2026-09-09T16:30:00+00:00' : null,
  'working_hours': state == 'checked_out' ? '9.0' : null,
  'status': state == 'not_checked_in' ? null : 'PRESENT',
  'allowed_actions': switch (state) {
    'not_checked_in' => ['check_in'],
    'checked_in' => ['check_out'],
    _ => <String>[],
  },
  'reason': null,
};

void main() {
  late FakeHttpAdapter adapter;
  late AttendanceRepository repository;
  late _AttendanceLocation location;

  setUp(() async {
    adapter = FakeHttpAdapter();
    final tokenStore = InMemoryTokenStore();
    await tokenStore.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'refresh',
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    location = _AttendanceLocation(
      AttendancePosition(
        latitude: 9.0765,
        longitude: 7.3986,
        accuracyM: 8.5,
        observedAt: DateTime.utc(2026, 9, 9, 7, 29, 58),
      ),
    );
    repository = AttendanceRepository(
      api: ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: tokenStore,
        dio: dio,
      ),
      location: location,
    );
  });

  test('today reads the typed ERP attendance projection', () async {
    adapter.on('GET', '/api/v1/field/attendance', (_) {
      return (200, _response('not_checked_in'));
    });

    final attendance = await repository.today();

    expect(attendance.state, AttendanceState.notCheckedIn);
    expect(attendance.allowedActions, {AttendanceAction.checkIn});
  });

  test('check in submits a fresh location and idempotency key', () async {
    adapter.on('POST', '/api/v1/field/attendance/check-in', (options) {
      expect(options.headers['Idempotency-Key'], isNotEmpty);
      expect(options.data, {
        'latitude': 9.0765,
        'longitude': 7.3986,
        'accuracy_m': 8.5,
        'observed_at': '2026-09-09T07:29:58.000Z',
      });
      return (200, _response('checked_in'));
    });

    final attendance = await repository.punch(AttendanceAction.checkIn);

    expect(attendance.state, AttendanceState.checkedIn);
    expect(attendance.allowedActions, {AttendanceAction.checkOut});
  });

  test(
    'attendance punches are not queued without a current location',
    () async {
      location.position = null;

      await expectLater(
        repository.punch(AttendanceAction.checkOut),
        throwsA(
          isA<AttendanceFailure>().having(
            (error) => error.code,
            'code',
            'location_required',
          ),
        ),
      );
      expect(adapter.requests, isEmpty);
    },
  );

  test('stable API error details are preserved', () async {
    adapter.on('POST', '/api/v1/field/attendance/check-out', (_) {
      return (
        422,
        {
          'detail': {
            'code': 'outside_geofence',
            'message': 'You are outside the permitted attendance location.',
          },
        },
      );
    });

    await expectLater(
      repository.punch(AttendanceAction.checkOut),
      throwsA(
        isA<AttendanceFailure>()
            .having((error) => error.code, 'code', 'outside_geofence')
            .having(
              (error) => error.message,
              'message',
              'You are outside the permitted attendance location.',
            ),
      ),
    );
  });
}
