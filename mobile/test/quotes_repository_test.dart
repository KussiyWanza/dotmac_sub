import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/repositories/quotes_repository.dart';
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
    if (options.method == 'GET') {
      return _json({
        'quotes': const [],
        'total': 0,
        'open': 0,
        'source_state': 'retired',
        'actions_available': false,
        'actions_unavailable_message': 'Please contact support.',
      });
    }
    return _json({
      'id': 'quote-1',
      'status': 'draft',
      'deposit_amount': '0',
      'feasibility': const {},
    }, statusCode: 201);
  }

  ResponseBody _json(Object body, {int statusCode = 200}) =>
      ResponseBody.fromString(
        jsonEncode(body),
        statusCode,
        headers: {
          Headers.contentTypeHeader: [Headers.jsonContentType],
        },
      );

  @override
  void close({bool force = false}) {}
}

void main() {
  late _FakeAdapter adapter;
  late QuotesRepository repository;

  setUp(() {
    adapter = _FakeAdapter();
    final dio = Dio(BaseOptions(baseUrl: 'https://selfcare.example.test'));
    dio.httpClientAdapter = adapter;
    repository = QuotesRepository(dio);
  });

  test('retains quote availability metadata from the list response', () async {
    final page = await repository.quotes();

    expect(page.actionsAvailable, isFalse);
    expect(page.actionsUnavailableMessage, 'Please contact support.');
  });

  test('sends a manually confirmed address separately from notes', () async {
    await repository.requestQuote(
      latitude: 9.0765,
      longitude: 7.3986,
      address: '12 Mississippi Street, Maitama',
      note: 'Blue gate, second floor',
    );

    final body = adapter.calls.single.data as Map<String, dynamic>;
    expect(body['address'], '12 Mississippi Street, Maitama');
    expect(body['note'], 'Blue gate, second floor');
  });
}
