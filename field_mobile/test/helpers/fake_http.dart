import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';

typedef FakeHandler =
    (int status, Object body) Function(RequestOptions options);

/// Routes dio requests to canned handlers: 'POST /api/v1/auth/login' → handler.
class FakeHttpAdapter implements HttpClientAdapter {
  final Map<String, FakeHandler> handlers = {};
  final List<RequestOptions> requests = [];

  void on(String method, String path, FakeHandler handler) {
    handlers['${method.toUpperCase()} $path'] = handler;
  }

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    await requestStream?.drain<void>();
    final key = '${options.method.toUpperCase()} ${options.path}';
    final handler = handlers[key];
    if (handler == null) {
      return ResponseBody.fromString(
        jsonEncode({'detail': 'no fake for $key'}),
        404,
        headers: _jsonHeaders,
      );
    }
    final (status, body) = handler(options);
    return ResponseBody.fromString(
      jsonEncode(body),
      status,
      headers: _jsonHeaders,
    );
  }

  static final _jsonHeaders = {
    Headers.contentTypeHeader: [Headers.jsonContentType],
  };

  @override
  void close({bool force = false}) {}
}

/// Unsigned JWT with the given expiry — the client only reads the exp claim.
String fakeJwt({required DateTime expiry, String sub = 'person-1'}) {
  String b64(Map<String, Object> data) =>
      base64Url.encode(utf8.encode(jsonEncode(data))).replaceAll('=', '');
  final header = b64({'alg': 'HS256', 'typ': 'JWT'});
  final payload = b64({
    'sub': sub,
    'exp': expiry.millisecondsSinceEpoch ~/ 1000,
  });
  return '$header.$payload.signature';
}
