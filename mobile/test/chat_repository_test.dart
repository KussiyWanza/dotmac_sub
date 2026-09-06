import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/models/chat.dart';
import 'package:dotmac_portal/src/providers/chat_controller.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:dotmac_portal/src/repositories/chat_repository.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeAdapter implements HttpClientAdapter {
  _FakeAdapter(this.response);

  final ResponseBody response;
  final List<RequestOptions> calls = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    calls.add(options);
    return response;
  }

  @override
  void close({bool force = false}) {}
}

ResponseBody _json(String body, int status) => ResponseBody.fromString(
      body,
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );

Dio _dio(_FakeAdapter adapter) {
  final dio = Dio(
    BaseOptions(
      baseUrl: 'https://selfcare.example.test/api/v1',
      validateStatus: (status) => status != null && status < 500,
    ),
  );
  dio.httpClientAdapter = adapter;
  return dio;
}

class _FailingChatRepository extends ChatRepository {
  _FailingChatRepository(this.error) : super(Dio());

  final ApiException error;

  @override
  Future<ChatSession> openSession({String endpoint = '/me/chat/session'}) {
    throw error;
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'native chat paths resolve against the authenticated API origin',
    () async {
      final adapter = _FakeAdapter(
        _json(
          '{"session_id":"session-1","visitor_token":"opaque",'
          '"conversation_id":"conversation-1",'
          '"api_base":"/widget","ws_url":"/ws/inbox"}',
          200,
        ),
      );
      final repository = ChatRepository(_dio(adapter));

      final session = await repository.openSession();

      expect(adapter.calls.single.path, '/me/chat/session');
      expect(
        session.apiBase,
        Uri.parse('https://selfcare.example.test/widget'),
      );
      expect(session.wsUrl, Uri.parse('wss://selfcare.example.test/ws/inbox'));
    },
  );

  test('absolute temporary CRM transport URLs remain unchanged', () {
    final session = ChatSession.fromJson({
      'session_id': 'session-1',
      'visitor_token': 'opaque',
      'api_base': 'https://crm.example.test/widget',
      'ws_url': 'wss://crm.example.test/ws/widget',
    }, brokerBaseUri: Uri.parse('https://selfcare.example.test/api/v1'));

    expect(session.apiBase, Uri.parse('https://crm.example.test/widget'));
    expect(session.wsUrl, Uri.parse('wss://crm.example.test/ws/widget'));
  });

  test('invalid broker transport schemes fail closed', () {
    expect(
      () => ChatSession.fromJson({
        'session_id': 'session-1',
        'visitor_token': 'opaque',
        'api_base': 'file:///private/chat',
        'ws_url': 'wss://selfcare.example.test/ws/inbox',
      }, brokerBaseUri: Uri.parse('https://selfcare.example.test/api/v1')),
      throwsFormatException,
    );
  });

  test('disabled chat preserves the broker safe error', () async {
    final adapter = _FakeAdapter(
      _json('{"detail":"Live chat is not enabled."}', 503),
    );
    final repository = ChatRepository(_dio(adapter));

    await expectLater(
      repository.openSession(),
      throwsA(
        isA<ApiException>().having(
          (error) => error.message,
          'message',
          'Live chat is not enabled.',
        ),
      ),
    );
  });

  test('chat controller shows the broker safe error to the customer', () async {
    final container = ProviderContainer(
      overrides: [
        chatRepositoryProvider.overrideWithValue(
          _FailingChatRepository(ApiException('Live chat is not enabled.')),
        ),
      ],
    );
    addTearDown(container.dispose);
    final subscription = container.listen(
      chatControllerProvider('/me/chat/session'),
      (_, __) {},
      fireImmediately: true,
    );
    addTearDown(subscription.close);

    await Future<void>.delayed(Duration.zero);

    final state = container.read(chatControllerProvider('/me/chat/session'));
    expect(state.loading, isFalse);
    expect(state.error, 'Live chat is not enabled.');
  });
}
