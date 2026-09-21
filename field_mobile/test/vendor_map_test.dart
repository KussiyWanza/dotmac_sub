import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/location/location_source.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/execution/execution_controller.dart';
import 'package:dotmac_field/features/vendor/vendor_map_screen.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

void main() {
  test(
    'vendorNearbyPlantProvider fetches plant around the crew location',
    () async {
      final adapter = FakeHttpAdapter();
      Map<String, dynamic>? sentQuery;
      adapter.on('GET', '/api/v1/field/vendor/map-assets/nearby', (options) {
        sentQuery = options.queryParameters;
        return (
          200,
          {
            'items': [
              {
                'id': 'olt-1',
                'type': 'olt',
                'title': 'OLT Ikeja',
                'latitude': 6.60,
                'longitude': 3.35,
              },
              {
                'id': 'clo-1',
                'type': 'splice_closure',
                'title': 'Closure 12',
                'latitude': 6.61,
                'longitude': 3.36,
              },
            ],
            'count': 2,
            'latitude': 6.6,
            'longitude': 3.35,
            'radius_m': 2000,
            'server_time': '2026-07-02T00:00:00Z',
          },
        );
      });

      final store = InMemoryTokenStore();
      await store.save(
        accessToken: fakeJwt(
          expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
        ),
        refreshToken: 'r',
        loginMode: LoginMode.vendor,
      );
      final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
        ..httpClientAdapter = adapter;
      final client = ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: store,
        dio: dio,
      );

      final container = ProviderContainer(
        overrides: [
          apiClientProvider.overrideWithValue(client),
          locationSourceProvider.overrideWithValue(
            FakeLocation((
              latitude: 6.6,
              longitude: 3.35,
              accuracy: 5.0,
              timestamp: DateTime.utc(2026, 6, 1, 8),
            )),
          ),
        ],
      );
      addTearDown(container.dispose);

      final data = await container.read(vendorNearbyPlantProvider.future);

      expect(data.center.latitude, 6.6);
      expect(data.assets.map((a) => a.id), ['olt-1', 'clo-1']);
      expect(sentQuery?['lat'], 6.6);
      expect(sentQuery?['radius_m'], 2000);
    },
  );

  test(
    'vendorNearbyPlantProvider falls back to default centre without a fix',
    () async {
      final adapter = FakeHttpAdapter();
      adapter.on(
        'GET',
        '/api/v1/field/vendor/map-assets/nearby',
        (_) => (
          200,
          {'items': <Map>[], 'count': 0, 'server_time': '2026-07-02T00:00:00Z'},
        ),
      );

      final store = InMemoryTokenStore();
      await store.save(
        accessToken: fakeJwt(
          expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
        ),
        refreshToken: 'r',
        loginMode: LoginMode.vendor,
      );
      final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
        ..httpClientAdapter = adapter;
      final client = ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: store,
        dio: dio,
      );

      final container = ProviderContainer(
        overrides: [
          apiClientProvider.overrideWithValue(client),
          locationSourceProvider.overrideWithValue(FakeLocation(null)),
        ],
      );
      addTearDown(container.dispose);

      final data = await container.read(vendorNearbyPlantProvider.future);
      // Lagos default centre when no GPS fix is available.
      expect(data.center.latitude, closeTo(6.52, 0.1));
      expect(data.assets, isEmpty);
    },
  );
}
