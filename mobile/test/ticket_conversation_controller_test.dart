import 'dart:async';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/models/page.dart' as models;
import 'package:dotmac_portal/src/models/realtime_event.dart';
import 'package:dotmac_portal/src/models/ticket.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:dotmac_portal/src/providers/ticket_conversation_controller.dart';
import 'package:dotmac_portal/src/repositories/app_realtime_socket.dart';
import 'package:dotmac_portal/src/repositories/support_repository.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

const _ticketId = 'e94a93e9-bdf0-49e7-a300-9d5226ba86b7';
const _otherTicketId = 'a637c96b-a8f5-496c-99ae-84725ea13fd5';

class _FakeSupportRepository extends SupportRepository {
  _FakeSupportRepository() : super(Dio());

  int reads = 0;
  Object? error;
  List<TicketComment> currentComments = [];

  @override
  Future<models.Page<TicketComment>> comments(
    String ticketId, {
    int limit = 100,
    int offset = 0,
  }) async {
    reads += 1;
    final currentError = error;
    if (currentError != null) throw currentError;
    return models.Page(
      items: List.of(currentComments),
      count: currentComments.length,
      limit: limit,
      offset: offset,
    );
  }
}

class _FakeSocket implements AppRealtimeSocket {
  _FakeSocket({this.failConnect = false});

  final StreamController<AppRealtimeSignal> controller =
      StreamController.broadcast(sync: true);
  final bool failConnect;
  int connects = 0;
  int closes = 0;

  @override
  Stream<AppRealtimeSignal> get signals => controller.stream;

  @override
  Future<void> connect() async {
    connects += 1;
    if (failConnect) throw StateError('offline');
    controller.add(
      const AppRealtimeConnectionSignal(AppRealtimeConnectionStatus.connected),
    );
  }

  @override
  Future<void> close() async {
    closes += 1;
  }

  @override
  Future<void> dispose() async {
    await controller.close();
  }

  void event(
    String event, {
    String ticketId = _ticketId,
    String? topic,
    bool refreshRequired = true,
  }) {
    controller.add(
      AppRealtimeEventSignal(
        AppRealtimeEvent.fromJson({
          'schema_version': 1,
          'event_id': '4031e709-ceba-47c7-a024-0209c3cf7312',
          'event': event,
          'topic': topic ??
              (event == 'connection_ack'
                  ? 'realtime:connection'
                  : 'principal:subscriber-id'),
          'timestamp': '2026-09-14T12:00:00Z',
          'refresh_required': refreshRequired,
          'data': event == 'connection_ack'
              ? {'status': 'connected'}
              : {
                  'ticket_id': ticketId,
                  'change': 'comment_created',
                  'comment_id': 'ab9fa0cb-a06a-4ec7-a570-0025d67c713f',
                },
        }),
      ),
    );
  }
}

ProviderContainer _container(
  _FakeSupportRepository repository,
  _FakeSocket socket,
) =>
    ProviderContainer(
      overrides: [
        supportRepositoryProvider.overrideWithValue(repository),
        appRealtimeSocketFactoryProvider.overrideWithValue(() => socket),
        ticketConversationTimingProvider.overrideWithValue(
          const TicketConversationTiming(
            eventDebounce: Duration(milliseconds: 10),
            unavailableAfter: Duration(milliseconds: 50),
          ),
        ),
        ticketConversationJitterProvider.overrideWithValue(() => 0),
      ],
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('matching events refresh comments and event bursts coalesce', (
    tester,
  ) async {
    final repository = _FakeSupportRepository();
    final socket = _FakeSocket();
    final container = _container(repository, socket);
    addTearDown(container.dispose);
    final subscription = container.listen(
      ticketConversationProvider(_ticketId),
      (_, __) {},
      fireImmediately: true,
    );
    addTearDown(subscription.close);
    await tester.pump();
    expect(repository.reads, 1);

    socket.event('support_ticket_comment_changed');
    socket.event('support_ticket_comment_changed');
    socket.event('support_ticket_comment_changed');
    await tester.pump(const Duration(milliseconds: 11));
    expect(repository.reads, 2);

    socket.event(
      'support_ticket_comment_changed',
      ticketId: _otherTicketId,
    );
    await tester.pump(const Duration(milliseconds: 20));
    expect(repository.reads, 2);

    socket.event('message_new');
    socket.event(
      'support_ticket_comment_changed',
      topic: 'conversation:unrelated',
    );
    socket.event(
      'support_ticket_comment_changed',
      refreshRequired: false,
    );
    await tester.pump(const Duration(milliseconds: 20));
    expect(repository.reads, 2);
  });

  testWidgets('connection acknowledgement performs comments catch-up', (
    tester,
  ) async {
    final repository = _FakeSupportRepository()..error = StateError('offline');
    final socket = _FakeSocket();
    final container = _container(repository, socket);
    addTearDown(container.dispose);
    final subscription = container.listen(
      ticketConversationProvider(_ticketId),
      (_, __) {},
      fireImmediately: true,
    );
    addTearDown(subscription.close);
    await tester.pump();
    expect(repository.reads, 1);
    repository.error = null;
    socket.event('connection_ack');
    await tester.pump(const Duration(milliseconds: 11));
    expect(repository.reads, 2);
  });

  testWidgets('prolonged connection failure exposes manual-refresh fallback', (
    tester,
  ) async {
    final repository = _FakeSupportRepository();
    final socket = _FakeSocket(failConnect: true);
    final container = _container(repository, socket);
    addTearDown(container.dispose);
    final subscription = container.listen(
      ticketConversationProvider(_ticketId),
      (_, __) {},
      fireImmediately: true,
    );
    addTearDown(subscription.close);
    await tester.pump();
    expect(
      container.read(ticketConversationProvider(_ticketId)).liveStatus,
      TicketLiveCommentStatus.reconnecting,
    );
    await tester.pump(const Duration(milliseconds: 51));
    expect(
      container.read(ticketConversationProvider(_ticketId)).liveStatus,
      TicketLiveCommentStatus.unavailable,
    );
    container
        .read(ticketConversationProvider(_ticketId).notifier)
        .didChangeAppLifecycleState(AppLifecycleState.paused);
    await tester.pump();
  });

  testWidgets('refresh failure keeps the last successful comments', (
    tester,
  ) async {
    final repository = _FakeSupportRepository()
      ..currentComments = [
        TicketComment(
          id: 'comment',
          ticketId: _ticketId,
          authorType: TicketCommentAuthorType.staff,
          body: 'Keep me',
        ),
      ];
    final socket = _FakeSocket();
    final container = _container(repository, socket);
    addTearDown(container.dispose);
    final subscription = container.listen(
      ticketConversationProvider(_ticketId),
      (_, __) {},
      fireImmediately: true,
    );
    addTearDown(subscription.close);
    await tester.pump();
    repository.error = StateError('offline');

    await container
        .read(ticketConversationProvider(_ticketId).notifier)
        .refreshComments();
    final state = container.read(ticketConversationProvider(_ticketId));
    expect(state.comments.hasError, isTrue);
    expect(state.comments.valueOrNull?.items.single.body, 'Keep me');
  });

  testWidgets('background closes and resume reconnects with one catch-up', (
    tester,
  ) async {
    final repository = _FakeSupportRepository();
    final socket = _FakeSocket();
    final container = _container(repository, socket);
    addTearDown(container.dispose);
    final subscription = container.listen(
      ticketConversationProvider(_ticketId),
      (_, __) {},
      fireImmediately: true,
    );
    addTearDown(subscription.close);
    await tester.pump();
    final controller =
        container.read(ticketConversationProvider(_ticketId).notifier);
    controller.didChangeAppLifecycleState(AppLifecycleState.paused);
    await tester.pump();
    expect(socket.closes, 1);
    expect(
      container.read(ticketConversationProvider(_ticketId)).liveStatus,
      TicketLiveCommentStatus.paused,
    );

    controller.didChangeAppLifecycleState(AppLifecycleState.resumed);
    await tester.pump();
    expect(socket.connects, 2);
    expect(repository.reads, 2);
  });

  testWidgets('there is no periodic comments polling', (tester) async {
    final repository = _FakeSupportRepository();
    final socket = _FakeSocket();
    final container = _container(repository, socket);
    addTearDown(container.dispose);
    final subscription = container.listen(
      ticketConversationProvider(_ticketId),
      (_, __) {},
      fireImmediately: true,
    );
    addTearDown(subscription.close);
    await tester.pump();
    expect(repository.reads, 1);
    await tester.pump(const Duration(minutes: 5));
    expect(repository.reads, 1);
  });

  test('reconnect backoff is bounded', () {
    const timing = TicketConversationTiming();
    expect(timing.reconnectDelay(0, 0), const Duration(milliseconds: 1600));
    expect(timing.reconnectDelay(4, 1), const Duration(seconds: 32));
    expect(timing.reconnectDelay(99, 1), const Duration(seconds: 32));
  });
}
