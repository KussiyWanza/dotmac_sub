import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/repositories/catalog_repository.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeAdapter implements HttpClientAdapter {
  final List<RequestOptions> calls = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    calls.add(options);
    return ResponseBody.fromString(
      '{"command":"${options.path.endsWith('reboot') ? 'reboot' : 'wifi_update'}",'
      '"status":"${options.path.endsWith('reboot') ? 'succeeded' : 'queued'}","subscription_id":"sub-1",'
      '"device_id":"ont-1","operation_id":"op-1",'
      '"message":"Command completed"}',
      200,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

void main() {
  late _FakeAdapter adapter;
  late CatalogRepository repository;

  setUp(() {
    adapter = _FakeAdapter();
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local/api/v1'));
    dio.httpClientAdapter = adapter;
    repository = CatalogRepository(dio);
  });

  test('reboot posts to the self-scoped command and preserves operation ID',
      () async {
    final outcome = await repository.rebootDevice('sub-1');

    expect(adapter.calls.single.path, '/me/subscriptions/sub-1/device/reboot');
    expect(adapter.calls.single.method, 'POST');
    expect(outcome.operationId, 'op-1');
  });

  test('Wi-Fi update posts desired fields with idempotency evidence', () async {
    final outcome = await repository.updateWifi(
      'sub-1',
      ssid: 'Home Network',
      password: 'password123',
    );

    expect(adapter.calls.single.path, '/me/subscriptions/sub-1/device/wifi');
    final data = adapter.calls.single.data as Map<String, dynamic>;
    expect(data['ssid'], 'Home Network');
    expect(data['password'], 'password123');
    expect(data['idempotency_key'], startsWith('wifi-'));
    expect(outcome.command, 'wifi_update');
    expect(outcome.accepted, isTrue);
  });

  test('Wi-Fi status reads the background operation', () async {
    final outcome = await repository.wifiUpdateStatus('sub-1');

    expect(adapter.calls.single.path, '/me/subscriptions/sub-1/device/wifi');
    expect(adapter.calls.single.method, 'GET');
    expect(outcome.operationId, 'op-1');
    expect(outcome.accepted, isTrue);
  });
}
