import 'dart:convert';

import 'package:dotmac_field/core/location/map_coordinates.dart';
import 'package:dotmac_field/features/jobs/location_pin_screen.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/today/asset_pin_screen.dart';
import 'package:dotmac_field/features/today/map_assets_repository.dart';
import 'package:dotmac_field/features/today/map_models.dart';
import 'package:dotmac_field/features/today/map_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';

JobSummary _job(String id, {String status = 'dispatched'}) => JobSummary(
  id: id,
  title: 'Job $id',
  status: status,
  workType: 'install',
  priority: 'normal',
);

String _detailWith({double? lat, double? lng, String addressText = 'x'}) =>
    jsonEncode({
      'location': {
        'latitude': lat,
        'longitude': lng,
        'address_text': addressText,
        'source': 'geocoded',
      },
    });

void main() {
  test('finite map camera constraint rejects non-finite camera centers', () {
    final invalidCamera = MapCamera(
      crs: const Epsg3857(),
      center: const LatLng(double.nan, double.nan),
      zoom: 12,
      rotation: 0,
      nonRotatedSize: const Size(360, 640),
    );
    final validCamera = MapCamera(
      crs: const Epsg3857(),
      center: defaultMapCenter,
      zoom: 12,
      rotation: 0,
      nonRotatedSize: const Size(360, 640),
    );

    expect(finiteMapCameraConstraint.constrain(invalidCamera), isNull);
    expect(finiteMapCameraConstraint.constrain(validCamera), same(validCamera));
  });

  test('buildJobPins skips jobs without cached coordinates', () {
    final pins = buildJobPins(
      [_job('a'), _job('b'), _job('c')],
      {
        'a': _detailWith(lat: 6.5, lng: 3.4),
        'b': _detailWith(lat: null, lng: null),
        // c has no cached detail at all
      },
    );
    expect(pins.single.id, 'a');
    expect(pins.single.latitude, 6.5);
  });

  test('buildJobPins carries cached street address for search', () {
    final pins = buildJobPins(
      [_job('a')],
      {
        'a': _detailWith(
          lat: 6.5,
          lng: 3.4,
          addressText: '12 Fiber Street, Lekki',
        ),
      },
    );

    expect(pins.single.addressText, '12 Fiber Street, Lekki');
  });

  test('buildJobPins skips out-of-range cached coordinates', () {
    final pins = buildJobPins(
      [_job('a'), _job('b')],
      {
        'a': _detailWith(lat: 91, lng: 3.4),
        'b': _detailWith(lat: 6.5, lng: 181),
      },
    );
    expect(pins, isEmpty);
  });

  test('default map asset layers match backend network defaults', () {
    expect(
      defaultMapAssetTypes,
      containsAll({
        'olt',
        'fdh',
        'fiber_access_point',
        'splice_closure',
        'wireless_mast',
      }),
    );
  });

  testWidgets('map renders a marker per pinned job', (tester) async {
    final pins = [
      const JobPin(
        id: 'a',
        title: 'Job a',
        status: 'dispatched',
        latitude: 6.5,
        longitude: 3.4,
      ),
      const JobPin(
        id: 'b',
        title: 'Job b',
        status: 'in_progress',
        latitude: 6.51,
        longitude: 3.41,
      ),
    ];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => pins),
          mapAssetsProvider.overrideWith((ref) async => []),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('pin-a')), findsOneWidget);
    expect(find.byKey(const Key('pin-b')), findsOneWidget);
    expect(find.byKey(const Key('edit-pins-button')), findsOneWidget);
  });

  testWidgets('map opened for a job centers and zooms on that job', (
    tester,
  ) async {
    final pins = [
      const JobPin(
        id: 'first',
        title: 'First job',
        status: 'dispatched',
        latitude: 6.5,
        longitude: 3.4,
      ),
      const JobPin(
        id: 'focused',
        title: 'Focused job',
        status: 'dispatched',
        latitude: 9.043599,
        longitude: 7.4963296,
      ),
    ];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => pins),
          mapAssetsProvider.overrideWith((ref) async => []),
        ],
        child: const MaterialApp(
          home: MapScreen(showTiles: false, focusJobId: 'focused'),
        ),
      ),
    );
    await tester.pumpAndSettle();

    final map = tester.widget<FlutterMap>(find.byType(FlutterMap));
    expect(map.options.initialCenter.latitude, 9.043599);
    expect(map.options.initialCenter.longitude, 7.4963296);
    expect(map.options.initialZoom, 16);
  });

  testWidgets('tapping a pin opens the job sheet', (tester) async {
    final pins = [
      const JobPin(
        id: 'a',
        title: 'Job a',
        status: 'dispatched',
        latitude: 6.5,
        longitude: 3.4,
      ),
    ];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => pins),
          mapAssetsProvider.overrideWith((ref) async => []),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('pin-a')));
    await tester.pumpAndSettle();
    expect(find.text('Job a'), findsOneWidget);
    expect(find.text('Edit pin location'), findsOneWidget);
  });

  testWidgets('map search finds a job pin and opens it', (tester) async {
    final pins = [
      const JobPin(
        id: 'a',
        title: 'Install at Marina',
        status: 'dispatched',
        latitude: 6.5,
        longitude: 3.4,
      ),
    ];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => pins),
          mapAssetsProvider.overrideWith((ref) async => []),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('map-search-field')), 'marina');
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('map-search-result-job-a')));
    await tester.pumpAndSettle();

    expect(find.text('Edit pin location'), findsOneWidget);
  });

  testWidgets('map search finds a job pin by street address', (tester) async {
    final pins = [
      const JobPin(
        id: 'a',
        title: 'Install at Marina',
        status: 'dispatched',
        latitude: 6.5,
        longitude: 3.4,
        addressText: '12 Fiber Street, Lekki',
      ),
    ];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => pins),
          mapAssetsProvider.overrideWith((ref) async => []),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('map-search-field')), 'fiber');
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('map-search-result-job-a')), findsOneWidget);
    expect(find.text('12 Fiber Street, Lekki · Dispatched'), findsOneWidget);
  });

  testWidgets('map search includes online street results', (tester) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => []),
          mapAssetsProvider.overrideWith((ref) async => []),
          mapPlaceSearchProvider.overrideWith((ref, query) async {
            if (query != 'fiber') return const [];
            return const [
              MapPlaceSearchResult(
                kind: 'job',
                id: 'online-job',
                title: 'Install at Fiber Street',
                status: 'dispatched',
                latitude: 6.5,
                longitude: 3.4,
                addressText: '12 Fiber Street, Lekki',
              ),
            ];
          }),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('map-search-field')), 'fiber');
    await tester.pumpAndSettle();

    expect(
      find.byKey(const Key('map-search-result-job-online-job')),
      findsOneWidget,
    );
    expect(find.text('12 Fiber Street, Lekki · Dispatched'), findsOneWidget);
  });

  testWidgets('map search finds a network asset and opens it', (tester) async {
    final assets = [
      const MapAsset(
        id: 'olt-1',
        type: 'olt',
        title: 'OLT Marina',
        subtitle: 'Central office',
        latitude: 6.52,
        longitude: 3.39,
      ),
    ];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => []),
          mapAssetsProvider.overrideWith((ref) async => assets),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(
      find.byKey(const Key('map-search-field')),
      'central',
    );
    await tester.pumpAndSettle();
    await tester.tap(
      find.byKey(const Key('map-search-result-asset-olt-olt-1')),
    );
    await tester.pumpAndSettle();

    expect(find.text('Edit asset location'), findsOneWidget);
  });

  testWidgets('edit pins button opens pinned job list', (tester) async {
    final pins = [
      const JobPin(
        id: 'a',
        title: 'Job a',
        status: 'dispatched',
        latitude: 6.5,
        longitude: 3.4,
      ),
    ];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => pins),
          mapAssetsProvider.overrideWith((ref) async => []),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('edit-pins-button')));
    await tester.pumpAndSettle();

    expect(find.text('Edit map pin'), findsOneWidget);
    expect(find.text('Job a'), findsOneWidget);
  });

  testWidgets('edit pins button stays visible with no pins', (tester) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => []),
          mapAssetsProvider.overrideWith((ref) async => []),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('edit-pins-button')));
    await tester.pumpAndSettle();

    expect(find.text('No pins loaded yet'), findsOneWidget);
  });

  testWidgets('map renders network asset pins and layer filters', (
    tester,
  ) async {
    final assets = [
      const MapAsset(
        id: 'olt-1',
        type: 'olt',
        title: 'OLT Alpha',
        latitude: 9.1,
        longitude: 7.4,
      ),
    ];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => []),
          mapAssetsProvider.overrideWith((ref) async => assets),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('asset-olt-olt-1')), findsOneWidget);
    expect(find.text('OLT'), findsOneWidget);
    expect(find.text('FDH'), findsOneWidget);

    await tester.tap(find.byKey(const Key('asset-olt-olt-1')));
    await tester.pumpAndSettle();
    expect(find.text('OLT Alpha'), findsOneWidget);
    expect(find.text('Edit asset location'), findsOneWidget);
  });

  testWidgets('map ignores invalid job and network asset coordinates', (
    tester,
  ) async {
    final pins = [
      const JobPin(
        id: 'bad-job',
        title: 'Bad job',
        status: 'dispatched',
        latitude: double.nan,
        longitude: 3.4,
      ),
    ];
    final assets = [
      const MapAsset(
        id: 'bad-asset',
        type: 'olt',
        title: 'Bad asset',
        latitude: 9.1,
        longitude: double.infinity,
      ),
    ];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          mapPinsProvider.overrideWith((ref) async => pins),
          mapAssetsProvider.overrideWith((ref) async => assets),
        ],
        child: const MaterialApp(home: MapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('pin-bad-job')), findsNothing);
    expect(find.byKey(const Key('asset-olt-bad-asset')), findsNothing);

    await tester.tap(find.byKey(const Key('edit-pins-button')));
    await tester.pumpAndSettle();
    expect(find.text('No pins loaded yet'), findsOneWidget);
  });

  testWidgets(
    'job pin editor falls back when initial coordinates are invalid',
    (tester) async {
      await tester.pumpWidget(
        const ProviderScope(
          child: MaterialApp(
            home: LocationPinScreen(
              jobId: 'bad-job',
              initialLocation: JobLocation(
                latitude: double.nan,
                longitude: 3.4,
                source: 'cached',
              ),
              showTiles: false,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(tester.takeException(), isNull);
      expect(find.text('6.524400, 3.379200'), findsOneWidget);
    },
  );

  testWidgets(
    'asset pin editor falls back when initial coordinates are invalid',
    (tester) async {
      await tester.pumpWidget(
        const ProviderScope(
          child: MaterialApp(
            home: AssetPinScreen(
              asset: MapAsset(
                id: 'bad-asset',
                type: 'olt',
                title: 'Bad asset',
                latitude: 9.1,
                longitude: double.infinity,
              ),
              showTiles: false,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(tester.takeException(), isNull);
      expect(find.text('6.524400, 3.379200'), findsOneWidget);
    },
  );
}
