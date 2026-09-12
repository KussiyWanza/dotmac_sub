import 'package:dotmac_field/app/app.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/location/map_coordinates.dart';
import 'package:dotmac_field/core/location/location_source.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/features/attendance/attendance_models.dart';
import 'package:dotmac_field/features/attendance/attendance_repository.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/location/location_cadence.dart';
import 'package:dotmac_field/features/location/location_ping_service.dart';
import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:dotmac_field/features/profile/profile_screen.dart';
import 'package:dotmac_field/features/vendor/vendor_map_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _AuthedController extends AuthController {
  @override
  AuthState build() => const Authenticated(LoginMode.staff);
}

class _VendorController extends AuthController {
  @override
  AuthState build() => const Authenticated(LoginMode.vendor);
}

class _UnauthedController extends AuthController {
  @override
  AuthState build() => const Unauthenticated();
}

AttendanceView _attendance(AttendanceState state) => AttendanceView(
  state: state,
  attendanceDate: '2026-09-09',
  timezone: 'Africa/Lagos',
  allowedActions: switch (state) {
    AttendanceState.notCheckedIn => {AttendanceAction.checkIn},
    AttendanceState.checkedIn => {AttendanceAction.checkOut},
    AttendanceState.checkedOut || AttendanceState.ineligible => {},
  },
);

class _FakeAttendanceRepository implements AttendanceRepositoryContract {
  _FakeAttendanceRepository([
    AttendanceState state = AttendanceState.notCheckedIn,
  ]) : view = _attendance(state);

  AttendanceView view;
  final List<AttendanceAction> punches = [];

  @override
  Future<AttendanceView> today() async => view;

  @override
  Future<AttendanceView> punch(AttendanceAction action) async {
    punches.add(action);
    view = _attendance(
      action == AttendanceAction.checkIn
          ? AttendanceState.checkedIn
          : AttendanceState.checkedOut,
    );
    return view;
  }
}

class _ReadyAttendanceLocation implements AttendanceLocationSource {
  @override
  Future<AttendancePosition?> current() async => AttendancePosition(
    latitude: 9.0765,
    longitude: 7.3986,
    accuracyM: 8,
    observedAt: DateTime.utc(2026, 9, 9, 7, 30),
  );

  @override
  Future<bool> isReady() async => true;

  @override
  Future<void> requestAccess() async {}
}

class _UnavailableAttendanceLocation extends _ReadyAttendanceLocation {
  bool requested = false;

  @override
  Future<bool> isReady() async => false;

  @override
  Future<void> requestAccess() async => requested = true;
}

Widget _app({
  bool authenticated = true,
  LocationPingService? locationPingService,
  AuthController Function() controller = _AuthedController.new,
  ManagerProfile? managerProfile,
  List<ManagerJob> managerJobs = const [],
  Future<JobList> Function()? jobsLoader,
  AttendanceRepositoryContract? attendanceRepository,
  AttendanceLocationSource? attendanceLocationSource,
  List<Override> extra = const [],
}) {
  return ProviderScope(
    overrides: [
      if (locationPingService != null)
        locationPingServiceProvider.overrideWithValue(locationPingService),
      if (!authenticated)
        authControllerProvider.overrideWith(_UnauthedController.new),
      if (authenticated) ...[
        authControllerProvider.overrideWith(controller),
        attendanceRepositoryProvider.overrideWithValue(
          attendanceRepository ?? _FakeAttendanceRepository(),
        ),
        attendanceLocationSourceProvider.overrideWithValue(
          attendanceLocationSource ?? _ReadyAttendanceLocation(),
        ),
        ...extra,
        managerProfileProvider.overrideWith((ref) async => managerProfile),
        managerSummaryProvider.overrideWith(
          (ref) async => const ManagerSummary(
            techniciansTotal: 3,
            techniciansLive: 1,
            techniciansSharing: 2,
            openJobs: 4,
            unassignedJobs: 1,
            pendingExpenses: 2,
          ),
        ),
        managerTechniciansProvider.overrideWith(
          (ref) async => const <ManagerTechnician>[],
        ),
        managerJobsProvider.overrideWith((ref) async => managerJobs),
        managerExpensesProvider.overrideWith((ref) async => const []),
        meProvider.overrideWith(
          (ref) async => const MeSummary(
            name: 'Chidi Tech',
            openJobs: 2,
            completedToday: 1,
          ),
        ),
        jobsListProvider.overrideWith(
          (ref) =>
              jobsLoader?.call() ?? Future.value(const JobList(<JobSummary>[])),
        ),
        todayJobsProvider.overrideWith(
          (ref) async => const JobList(<JobSummary>[]),
        ),
        allAssignedJobsProvider.overrideWith(
          (ref) async => const JobList(<JobSummary>[]),
        ),
        // SyncStatusBar reads these; empty streams keep it off-screen without
        // needing a real SyncService.
        pendingOutboxProvider.overrideWith(
          (ref) => Stream.value(<OutboxEntry>[]),
        ),
        conflictOutboxProvider.overrideWith(
          (ref) => Stream.value(<OutboxEntry>[]),
        ),
        pendingPhotosProvider.overrideWith((ref) => Stream.value(0)),
      ],
    ],
    child: const DotmacFieldApp(),
  );
}

void main() {
  testWidgets('unauthenticated users land on the login screen', (tester) async {
    await tester.pumpWidget(_app(authenticated: false));
    await tester.pumpAndSettle();

    expect(find.text('DotMac Field'), findsOneWidget);
    expect(find.text('Sign in'), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
  });

  testWidgets('technician shell hides CRM search and sales tabs', (
    tester,
  ) async {
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();

    expect(find.text('Hello, Chidi'), findsOneWidget);
    expect(find.text('Map'), findsOneWidget);
    expect(find.text('Schedule'), findsOneWidget);
    expect(find.text('Customers'), findsNothing);
    expect(find.text('Sales'), findsNothing);
    expect(find.text('Profile'), findsOneWidget);
    expect(find.byType(NavigationBar), findsOneWidget);
  });

  testWidgets('tapping a tab switches branch', (tester) async {
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();

    await tester.tap(find.text('Schedule'));
    await tester.pumpAndSettle();
    expect(find.text('Schedule'), findsWidgets);
  });

  testWidgets('jobs failure does not crash the application shell', (
    tester,
  ) async {
    await tester.pumpWidget(
      _app(jobsLoader: () => Future.error(StateError('jobs unavailable'))),
    );
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.text('Hello, Chidi'), findsOneWidget);
  });

  testWidgets('vendor shell shows work-order tabs and vendor-scoped map', (
    tester,
  ) async {
    await tester.pumpWidget(
      _app(
        controller: _VendorController.new,
        extra: [
          vendorNearbyPlantProvider.overrideWith(
            (ref) async =>
                const VendorMapData(center: defaultMapCenter, assets: []),
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.text('Hello, Chidi'), findsOneWidget);
    expect(find.text('Today'), findsOneWidget);
    expect(find.text('Map'), findsOneWidget); // vendor-scoped nearby-plant map
    expect(find.text('Schedule'), findsOneWidget);
    expect(find.text('Materials'), findsOneWidget);
    expect(find.text('Expenses'), findsOneWidget);
    expect(find.text('Profile'), findsOneWidget);

    await tester.tap(find.text('Map'));
    await tester.pumpAndSettle();
    expect(find.text('Nearby plant'), findsOneWidget);

    expect(find.text('Projects'), findsNothing);
    expect(find.text('Customers'), findsNothing);
    expect(find.text('Sales'), findsNothing);
  });

  testWidgets('manager shell shows dispatch and approval tabs', (tester) async {
    await tester.pumpWidget(
      _app(
        managerProfile: const ManagerProfile(
          name: 'Amaka Manager',
          roles: ['field_manager'],
          permissions: [
            'operations:work_order:read',
            'operations:dispatch:read',
          ],
          isManager: true,
        ),
        managerJobs: [
          ManagerJob(
            id: 'wo-dispatch-1',
            title: 'Install service at Garki',
            status: 'scheduled',
            priority: 'normal',
            workType: 'install',
          ),
          ManagerJob(
            id: 'wo-dispatch-2',
            title: 'Repair customer drop',
            status: 'dispatched',
            priority: 'high',
            workType: 'repair',
            assignmentQueueId: 'queue-1',
            assignedToPersonId: 'person-1',
            assignedToLabel: 'Ada Technician',
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.text('Operations dashboard'), findsOneWidget);
    expect(find.text('Dashboard'), findsOneWidget);
    expect(find.text('Team'), findsWidgets);
    expect(find.text('Dispatch'), findsOneWidget);
    expect(find.text('Approvals'), findsWidgets);
    expect(find.text('Materials'), findsOneWidget);
    expect(find.text('Sales'), findsNothing);

    await tester.tap(find.text('Dispatch'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.text('Open work orders'), findsOneWidget);
    expect(find.text('Install service at Garki'), findsOneWidget);
    expect(find.text('Assign'), findsOneWidget);
    expect(find.text('Repair customer drop'), findsOneWidget);
    expect(find.text('Assigned to Ada Technician'), findsOneWidget);
    expect(find.text('Unassign'), findsOneWidget);
  });

  testWidgets('manager shell hides team map without dispatch read', (
    tester,
  ) async {
    await tester.pumpWidget(
      _app(
        managerProfile: const ManagerProfile(
          name: 'Expense Manager',
          roles: ['field_manager'],
          permissions: ['operations:expense_request:read'],
          isManager: true,
        ),
      ),
    );
    await tester.pumpAndSettle();

    final navigation = tester.widget<NavigationBar>(find.byType(NavigationBar));
    final labels = navigation.destinations
        .whereType<NavigationDestination>()
        .map((destination) => destination.label)
        .toList();
    expect(labels, isNot(contains('Team')));
    expect(find.text('Team location'), findsNothing);
  });

  testWidgets(
    'check in immediately goes on shift and enables location sharing',
    (tester) async {
      final calls = <({bool enabled, ShiftState shift})>[];
      final attendance = _FakeAttendanceRepository();
      final locationService = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
        sharingUpdater: ({required enabled, required shift}) async {
          calls.add((enabled: enabled, shift: shift));
          return true;
        },
      );

      await tester.pumpWidget(
        _app(
          locationPingService: locationService,
          attendanceRepository: attendance,
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Location sharing'), findsOneWidget);
      expect(find.text('Check In'), findsOneWidget);
      expect(find.text('Check Out'), findsOneWidget);
      expect(find.text('Break'), findsNothing);
      expect(find.text('Off'), findsNothing);

      await tester.tap(find.text('On shift'));
      await tester.pumpAndSettle();
      expect(calls, isEmpty);

      await tester.tap(find.text('Check In'));
      await tester.pumpAndSettle();
      expect(attendance.punches, [AttendanceAction.checkIn]);
      expect(locationService.shift, ShiftState.onShift);
      expect(calls.single.enabled, isTrue);
      expect(calls.single.shift, ShiftState.onShift);
      expect(
        find.text('On shift · sharing location with dispatch.'),
        findsOneWidget,
      );
    },
  );

  testWidgets('failed automatic sharing can be retried from On shift', (
    tester,
  ) async {
    var attempts = 0;
    final attendance = _FakeAttendanceRepository();
    final locationService = LocationPingService(
      location: FakeLocation(null),
      poster: (_) async => true,
      sharingUpdater: ({required enabled, required shift}) async {
        attempts += 1;
        return attempts > 1;
      },
    );

    await tester.pumpWidget(
      _app(
        locationPingService: locationService,
        attendanceRepository: attendance,
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.text('Check In'));
    await tester.pumpAndSettle();

    expect(attempts, 1);
    expect(locationService.shift, ShiftState.offShift);
    expect(
      find.text(
        'Checked in · location sharing could not start. Tap On shift to retry.',
      ),
      findsOneWidget,
    );

    await tester.tap(find.text('On shift'));
    await tester.pumpAndSettle();

    expect(attempts, 2);
    expect(locationService.shift, ShiftState.onShift);
  });

  testWidgets('restores server location sharing when the app starts', (
    tester,
  ) async {
    final attendance = _FakeAttendanceRepository(AttendanceState.checkedIn);
    final locationService = LocationPingService(
      location: FakeLocation(null),
      poster: (_) async => true,
      sharingReader: () async => const LocationSharingSnapshot(
        enabled: true,
        shift: ShiftState.onShift,
      ),
    );

    await tester.pumpWidget(
      _app(
        locationPingService: locationService,
        attendanceRepository: attendance,
      ),
    );
    await tester.pumpAndSettle();

    expect(locationService.shift, ShiftState.onShift);
  });

  testWidgets('reminds the engineer to enable location once on app open', (
    tester,
  ) async {
    final location = _UnavailableAttendanceLocation();

    await tester.pumpWidget(_app(attendanceLocationSource: location));
    await tester.pumpAndSettle();

    expect(find.text('Enable location'), findsNWidgets(2));
    expect(
      find.text(
        'Location must be turned on and allowed before you can check in and share your location.',
      ),
      findsOneWidget,
    );
    await tester.tap(find.widgetWithText(FilledButton, 'Enable location'));
    await tester.pumpAndSettle();
    expect(location.requested, isTrue);
  });

  testWidgets('confirmed checkout stops location sharing', (tester) async {
    final calls = <({bool enabled, ShiftState shift})>[];
    final attendance = _FakeAttendanceRepository(AttendanceState.checkedIn);
    final locationService = LocationPingService(
      location: FakeLocation(null),
      poster: (_) async => true,
      sharingUpdater: ({required enabled, required shift}) async {
        calls.add((enabled: enabled, shift: shift));
        return true;
      },
    )..setShift(ShiftState.onShift);

    await tester.pumpWidget(
      _app(
        locationPingService: locationService,
        attendanceRepository: attendance,
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.text('Check Out'));
    await tester.pumpAndSettle();

    expect(attendance.punches, [AttendanceAction.checkOut]);
    expect(locationService.shift, ShiftState.offShift);
    expect(calls.single.enabled, isFalse);
    expect(calls.single.shift, ShiftState.offShift);
  });
}
