import 'dart:async';
import 'dart:convert';

import 'package:dotmac_portal/src/core/token_storage.dart';
import 'package:dotmac_portal/src/models/realtime_event.dart';
import 'package:dotmac_portal/src/repositories/app_realtime_socket.dart';
import 'package:flutter_test/flutter_test.dart';

class _TokenStorage extends TokenStorage {
  _TokenStorage(this.token);

  String? token;

  @override
  Future<String?> readAccessToken() async => token;
}

class _FakeChannel implements AppRealtimeChannel {
  final StreamController<dynamic> incoming = StreamController.broadcast();
  final List<Object> sent = [];
  bool closed = false;

  @override
  Stream<dynamic> get stream => incoming.stream;

  @override
  Future<void> get ready async {}

  @override
  void add(Object data) => sent.add(data);

  @override
  Future<void> close([int? closeCode]) async {
    closed = true;
    await incoming.close();
  }
}

void main() {
  test('builds the authenticated inbox URL without query credentials', () {
    expect(
      appRealtimeWebSocketUri('https://selfcare.example/api/v1?token=old'),
      Uri.parse('wss://selfcare.example/ws/inbox'),
    );
    expect(
      appRealtimeWebSocketUri('http://10.0.2.2:8000'),
      Uri.parse('ws://10.0.2.2:8000/ws/inbox'),
    );
  });

  test('authenticates by subprotocol and rereads the token on reconnect',
      () async {
    final storage = _TokenStorage('first-token');
    final calls = <(Uri, List<String>)>[];
    final channels = <_FakeChannel>[];
    AppRealtimeChannel connector(
      Uri uri, {
      Iterable<String>? protocols,
    }) {
      calls.add((uri, protocols!.toList()));
      final channel = _FakeChannel();
      channels.add(channel);
      return channel;
    }

    final socket = AuthenticatedAppRealtimeSocket(
      endpoint: Uri.parse('wss://selfcare.example/ws/inbox'),
      tokenStorage: storage,
      connector: connector,
    );
    await socket.connect();
    await socket.close();
    storage.token = 'new-token';
    await socket.connect();

    expect(calls[0].$1.toString(), isNot(contains('first-token')));
    expect(calls[0].$2, ['dotmac-auth', 'first-token']);
    expect(calls[1].$1.toString(), isNot(contains('new-token')));
    expect(calls[1].$2, ['dotmac-auth', 'new-token']);
    await socket.dispose();
  });

  test('decodes server frames into typed events', () async {
    final channel = _FakeChannel();
    final socket = AuthenticatedAppRealtimeSocket(
      endpoint: Uri.parse('wss://selfcare.example/ws/inbox'),
      tokenStorage: _TokenStorage('token'),
      connector: (_, {protocols}) => channel,
    );
    final eventFuture = socket.signals
        .where((signal) => signal is AppRealtimeEventSignal)
        .cast<AppRealtimeEventSignal>()
        .map((signal) => signal.event)
        .first;
    await socket.connect();
    channel.incoming.add(
      jsonEncode({
        'schema_version': 1,
        'event_id': '4031e709-ceba-47c7-a024-0209c3cf7312',
        'event': 'connection_ack',
        'topic': 'realtime:connection',
        'timestamp': '2026-09-14T12:00:00Z',
        'refresh_required': true,
        'data': {'status': 'connected'},
      }),
    );

    expect((await eventFuture).type, AppRealtimeEventType.connectionAck);
    await socket.dispose();
  });
}
