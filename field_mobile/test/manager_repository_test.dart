import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

void main() {
  late ProviderContainer container;
  late FakeHttpAdapter adapter;

  setUp(() async {
    adapter = FakeHttpAdapter();
    final store = InMemoryTokenStore();
    await store.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'refresh',
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    final client = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
    );
    container = ProviderContainer(
      overrides: [apiClientProvider.overrideWithValue(client)],
    );
  });

  tearDown(() => container.dispose());

  test('unassignJob sends the assignment queue transition', () async {
    adapter.on('POST', '/api/v1/field/manager/assignments/queue-1/unassign', (
      options,
    ) {
      expect(options.data, {
        'reason': 'Manager unassigned technician from mobile dispatch',
      });
      return (200, {'id': 'queue-1', 'status': 'skipped'});
    });

    final outcome = await container
        .read(managerRepositoryProvider)
        .unassignJob(
          assignmentQueueId: 'queue-1',
          reason: 'Manager unassigned technician from mobile dispatch',
        );

    expect(outcome.id, 'queue-1');
    expect(outcome.status, 'skipped');
  });
}
