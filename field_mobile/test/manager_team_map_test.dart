import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:dotmac_field/features/manager/manager_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

const _livePosition = ManagerTeamMapPosition(
  technicianId: 'tech-live',
  personId: 'person-live',
  label: 'Ada Technician',
  status: ManagerPresenceStatus.onShift,
  latitude: 6.5244,
  longitude: 3.3792,
  isLive: true,
  accuracyM: 8,
);

const _stalePosition = ManagerTeamMapPosition(
  technicianId: 'tech-stale',
  personId: 'person-stale',
  label: 'Bola Technician',
  status: ManagerPresenceStatus.onBreak,
  latitude: 9.0765,
  longitude: 7.3986,
  isLive: false,
);

const _technicians = [
  ManagerTechnician(
    personId: 'person-live',
    technicianId: 'tech-live',
    name: 'Ada Technician',
    status: 'on_shift',
    title: 'Installer',
    region: 'Lagos',
    locationSharingEnabled: true,
    isLive: true,
    activeWorkOrderTitle: 'Install at Marina',
  ),
  ManagerTechnician(
    personId: 'person-stale',
    technicianId: 'tech-stale',
    name: 'Bola Technician',
    status: 'break',
    title: 'Engineer',
    region: 'Abuja',
    locationSharingEnabled: true,
    isLive: false,
  ),
  ManagerTechnician(
    personId: 'person-private',
    technicianId: 'tech-private',
    name: 'Chidi Technician',
    status: 'off_shift',
    region: 'Kano',
    locationSharingEnabled: false,
    isLive: false,
  ),
];

ManagerTeamMapFeed _feed(List<ManagerTeamMapPosition> positions) =>
    ManagerTeamMapFeed(
      count: positions.length,
      liveCount: positions.where((item) => item.isLive).length,
      staleAfterSeconds: 120,
      positions: positions,
      receivedAt: DateTime.now(),
    );

Widget _subject({
  List<ManagerTeamMapPosition> positions = const [
    _livePosition,
    _stalePosition,
  ],
  List<ManagerTechnician> technicians = _technicians,
}) {
  return ProviderScope(
    overrides: [
      managerTeamMapProvider.overrideWith((ref) async => _feed(positions)),
      managerTechniciansProvider.overrideWith((ref) async => technicians),
      managerTechnicianLocationDetailProvider.overrideWith((
        ref,
        technicianId,
      ) async {
        final position = positions.firstWhere(
          (item) => item.technicianId == technicianId,
        );
        return ManagerTechnicianLocationDetail(
          position: position,
          addressStatus: ManagerLocationAddressStatus.available,
          addressText: technicianId == 'tech-live'
              ? 'Marina Road, Lagos'
              : 'Central District, Abuja',
        );
      }),
    ],
    child: const MaterialApp(home: ManagerTeamMapScreen(showTiles: false)),
  );
}

void main() {
  testWidgets('renders every sharing technician at geographic coordinates', (
    tester,
  ) async {
    await tester.pumpWidget(_subject());
    await tester.pumpAndSettle();

    expect(find.byType(FlutterMap), findsOneWidget);
    expect(find.byKey(const Key('team-map-marker-tech-live')), findsOneWidget);
    expect(find.byKey(const Key('team-map-marker-tech-stale')), findsOneWidget);
    expect(find.text('1 live · 1 stale'), findsOneWidget);
  });

  testWidgets('marker details expose freshness, accuracy, and work context', (
    tester,
  ) async {
    await tester.pumpWidget(_subject());
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('team-map-marker-tech-live')));
    await tester.pumpAndSettle();

    expect(find.text('Ada Technician'), findsWidgets);
    expect(find.text('Live · On shift'), findsOneWidget);
    expect(find.text('Accuracy ±8 m'), findsOneWidget);
    expect(find.text('Install at Marina'), findsOneWidget);
    expect(find.text('View dispatch'), findsOneWidget);
    expect(find.text('Live location address'), findsOneWidget);
    expect(find.text('Marina Road, Lagos'), findsOneWidget);
  });

  testWidgets(
    'roster tap brings the map into view and focuses the technician',
    (tester) async {
      await tester.pumpWidget(_subject());
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.byKey(const Key('team-map-search')),
        250,
        scrollable: find.byType(Scrollable).last,
      );
      await tester.enterText(find.byKey(const Key('team-map-search')), 'bola');
      await tester.pumpAndSettle();
      final rosterTile = find.byKey(const Key('team-technician-person-stale'));
      expect(rosterTile, findsOneWidget);

      await tester.tap(rosterTile);
      await tester.pumpAndSettle();

      final map = tester.widget<FlutterMap>(find.byType(FlutterMap));
      expect(
        map.mapController!.camera.center.latitude,
        closeTo(9.0765, 0.000001),
      );
      expect(
        map.mapController!.camera.center.longitude,
        closeTo(7.3986, 0.000001),
      );
      expect(map.mapController!.camera.zoom, 17);
      expect(find.text('Last known address'), findsOneWidget);
      expect(find.text('Central District, Abuja'), findsOneWidget);
      expect(
        tester.getTopLeft(find.byType(FlutterMap)).dy,
        greaterThanOrEqualTo(0),
      );
    },
  );

  testWidgets('address-unavailable detail preserves the location drawer', (
    tester,
  ) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          managerTeamMapProvider.overrideWith(
            (ref) async => _feed(const [_livePosition]),
          ),
          managerTechniciansProvider.overrideWith(
            (ref) async => [_technicians.first],
          ),
          managerTechnicianLocationDetailProvider.overrideWith((ref, id) async {
            return const ManagerTechnicianLocationDetail(
              position: _livePosition,
              addressStatus: ManagerLocationAddressStatus.unavailable,
            );
          }),
        ],
        child: const MaterialApp(home: ManagerTeamMapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('team-map-marker-tech-live')));
    await tester.pumpAndSettle();

    expect(find.text('Nearest address unavailable'), findsOneWidget);
    expect(find.text('Live location address'), findsOneWidget);
    expect(find.text('Ada Technician'), findsWidgets);
  });

  testWidgets('location-less technician opens status without address lookup', (
    tester,
  ) async {
    var addressLookups = 0;
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          managerTeamMapProvider.overrideWith(
            (ref) async => _feed(const [_livePosition]),
          ),
          managerTechniciansProvider.overrideWith((ref) async => _technicians),
          managerTechnicianLocationDetailProvider.overrideWith((ref, id) async {
            addressLookups += 1;
            return const ManagerTechnicianLocationDetail(
              position: _livePosition,
              addressStatus: ManagerLocationAddressStatus.available,
              addressText: 'Marina Road, Lagos',
            );
          }),
        ],
        child: const MaterialApp(home: ManagerTeamMapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const Key('team-map-search')),
      250,
      scrollable: find.byType(Scrollable).last,
    );
    await tester.enterText(find.byKey(const Key('team-map-search')), 'chidi');
    await tester.pumpAndSettle();
    final rosterTile = find.byKey(const Key('team-technician-person-private'));
    expect(rosterTile, findsOneWidget);

    await tester.tap(rosterTile);
    await tester.pumpAndSettle();

    expect(find.text('Location sharing is off'), findsOneWidget);
    expect(find.text('Live location address'), findsNothing);
    expect(find.text('Last known address'), findsNothing);
    expect(addressLookups, 0);
  });

  testWidgets('search and privacy filter narrow the synchronized roster', (
    tester,
  ) async {
    await tester.pumpWidget(_subject());
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const Key('team-map-search')),
      250,
      scrollable: find.byType(Scrollable).last,
    );

    await tester.enterText(find.byKey(const Key('team-map-search')), 'chidi');
    await tester.pump();

    expect(
      find.byKey(const Key('team-technician-person-private')),
      findsOneWidget,
    );
    expect(find.byKey(const Key('team-technician-person-live')), findsNothing);

    await tester.tap(find.widgetWithText(ChoiceChip, 'Not sharing'));
    await tester.pump();
    expect(find.text('Not sharing'), findsWidgets);
  });

  testWidgets('invalid coordinates never become map markers', (tester) async {
    const invalid = ManagerTeamMapPosition(
      technicianId: 'tech-invalid',
      personId: 'person-invalid',
      label: 'Invalid Technician',
      status: ManagerPresenceStatus.onShift,
      latitude: double.nan,
      longitude: 3.4,
      isLive: true,
    );
    await tester.pumpWidget(
      _subject(positions: const [invalid], technicians: const []),
    );
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('team-map-marker-tech-invalid')), findsNothing);
    expect(
      find.text('No technicians are currently sharing a mapped location'),
      findsOneWidget,
    );
  });

  testWidgets('map failures preserve a retryable scroll surface', (
    tester,
  ) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          managerTeamMapProvider.overrideWith(
            (ref) => Future<ManagerTeamMapFeed>.error(Exception('offline')),
          ),
          managerTechniciansProvider.overrideWith((ref) async => const []),
        ],
        child: const MaterialApp(home: ManagerTeamMapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Could not load team locations'), findsOneWidget);
    expect(find.text('Retry'), findsOneWidget);
    expect(find.byType(CustomScrollView), findsOneWidget);
  });

  testWidgets('compact mobile viewport has no layout overflow', (tester) async {
    tester.view.physicalSize = const Size(320, 640);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(_subject(positions: const []));
    await tester.pumpAndSettle();
    await tester.drag(find.byType(CustomScrollView), const Offset(0, -500));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
  });

  testWidgets('refreshes the visible map feed every 30 seconds', (
    tester,
  ) async {
    var fetches = 0;
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          managerTeamMapProvider.overrideWith((ref) async {
            fetches += 1;
            return _feed(const [_livePosition]);
          }),
          managerTechniciansProvider.overrideWith(
            (ref) async => [_technicians.first],
          ),
        ],
        child: const MaterialApp(home: ManagerTeamMapScreen(showTiles: false)),
      ),
    );
    await tester.pump();
    await tester.pump();
    expect(fetches, 1);

    await tester.pump(const Duration(seconds: 30));
    await tester.pump();

    expect(fetches, 2);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
