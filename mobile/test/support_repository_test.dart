import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/repositories/support_repository.dart';
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
    final isComment = options.path.endsWith('/comments');
    return ResponseBody.fromString(
      isComment
          ? '{"id":"comment-1","ticket_id":"ticket-1",'
              '"body":"Still offline","is_internal":false}'
          : '{"id":"ticket-1","title":"No internet",'
              '"status":"open","priority":"normal"}',
      201,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

SupportRepository _repository(_FakeAdapter adapter) {
  final dio = Dio(
    BaseOptions(
      baseUrl: 'https://test.local/api/v1',
      contentType: Headers.jsonContentType,
    ),
  );
  dio.httpClientAdapter = adapter;
  return SupportRepository(dio);
}

Map<String, String> _fields(RequestOptions request) {
  final form = request.data as FormData;
  return Map<String, String>.fromEntries(form.fields);
}

void main() {
  test(
    'ticket creation without attachments still uses multipart form data',
    () async {
      final adapter = _FakeAdapter();
      final repository = _repository(adapter);

      final ticket = await repository.createTicket(
        title: 'No internet',
        description: 'Service has been down since morning',
        priority: 'high',
      );

      expect(ticket.id, 'ticket-1');
      final request = adapter.calls.single;
      expect(request.method, 'POST');
      expect(request.path, '/me/support/tickets');
      expect(request.data, isA<FormData>());
      expect(
        request.contentType,
        startsWith('${Headers.multipartFormDataContentType}; boundary='),
      );
      expect(_fields(request), {
        'title': 'No internet',
        'description': 'Service has been down since morning',
        'priority': 'high',
      });
      expect((request.data as FormData).files, isEmpty);
    },
  );

  test(
    'ticket reply without attachments still uses multipart form data',
    () async {
      final adapter = _FakeAdapter();
      final repository = _repository(adapter);

      final comment = await repository.addComment('ticket-1', 'Still offline');

      expect(comment.id, 'comment-1');
      final request = adapter.calls.single;
      expect(request.method, 'POST');
      expect(request.path, '/me/support/tickets/ticket-1/comments');
      expect(request.data, isA<FormData>());
      expect(
        request.contentType,
        startsWith('${Headers.multipartFormDataContentType}; boundary='),
      );
      expect(_fields(request), {'body': 'Still offline'});
      expect((request.data as FormData).files, isEmpty);
    },
  );
}
