import 'dart:async';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/app.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/features/support/ticket_detail_screen.dart';
import 'package:dotmac_portal/src/models/page.dart' as models;
import 'package:dotmac_portal/src/models/ticket.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:dotmac_portal/src/providers/ticket_conversation_controller.dart';
import 'package:dotmac_portal/src/repositories/app_realtime_socket.dart';
import 'package:dotmac_portal/src/repositories/support_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

const _ticketId = 'e94a93e9-bdf0-49e7-a300-9d5226ba86b7';

TicketComment _comment(
  String id,
  TicketCommentAuthorType authorType,
  String body, {
  bool isInternal = false,
}) =>
    TicketComment(
      id: id,
      ticketId: _ticketId,
      authorType: authorType,
      body: body,
      isInternal: isInternal,
      createdAt: DateTime.utc(2026, 9, 14, 12),
    );

class _FakeSupportRepository extends SupportRepository {
  _FakeSupportRepository(this.currentComments) : super(Dio());

  List<TicketComment> currentComments;
  Object? commentsError;
  int commentReads = 0;

  @override
  Future<Ticket> ticket(String id) async => Ticket(
        id: id,
        title: 'Test ticket',
        status: 'open',
        priority: 'normal',
        number: 'TKT-100',
        createdAt: DateTime.utc(2026, 9, 14, 11),
      );

  @override
  Future<models.Page<TicketComment>> comments(
    String ticketId, {
    int limit = 100,
    int offset = 0,
  }) async {
    commentReads += 1;
    final error = commentsError;
    if (error != null) throw error;
    return models.Page(
      items: List.of(currentComments),
      count: currentComments.length,
      limit: limit,
      offset: offset,
    );
  }
}

class _IdleRealtimeSocket implements AppRealtimeSocket {
  @override
  Stream<AppRealtimeSignal> get signals => const Stream.empty();

  @override
  Future<void> close() async {}

  @override
  Future<void> connect() async {}

  @override
  Future<void> dispose() async {}
}

class _FailingRealtimeSocket extends _IdleRealtimeSocket {
  @override
  Future<void> connect() => Future.error(StateError('offline'));
}

Widget _app(
  _FakeSupportRepository repository, {
  AppRealtimeSocketFactory socketFactory = _IdleRealtimeSocket.new,
  TicketConversationTiming timing = const TicketConversationTiming(),
}) =>
    ProviderScope(
      overrides: [
        supportRepositoryProvider.overrideWithValue(repository),
        appRealtimeSocketFactoryProvider.overrideWithValue(socketFactory),
        ticketConversationTimingProvider.overrideWithValue(timing),
      ],
      child: MaterialApp(
        theme: dotmacThemeFor(Brightness.light),
        home: const TicketDetailScreen(ticketId: _ticketId),
      ),
    );

void main() {
  testWidgets('ticket replies identify customer, staff, and system senders',
      (tester) async {
    final repository = _FakeSupportRepository([
      _comment('customer', TicketCommentAuthorType.customer, 'My reply'),
      _comment('staff', TicketCommentAuthorType.staff, 'Support reply'),
      _comment('system', TicketCommentAuthorType.system, 'Service notice'),
      _comment('unknown', TicketCommentAuthorType.unknown, 'Legacy sender'),
      _comment(
        'internal',
        TicketCommentAuthorType.staff,
        'Private staff note',
        isInternal: true,
      ),
    ]);

    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    expect(find.text('You'), findsOneWidget);
    expect(find.text('Support Team'), findsOneWidget);
    expect(find.text('Service update'), findsOneWidget);
    expect(find.text('Sender unavailable'), findsOneWidget);
    expect(find.text('Private staff note'), findsNothing);

    Align alignmentFor(String id) => tester.widget<Align>(
          find.byKey(ValueKey('ticket-comment-$id')),
        );
    expect(alignmentFor('customer').alignment, Alignment.centerRight);
    expect(alignmentFor('staff').alignment, Alignment.centerLeft);
    expect(alignmentFor('system').alignment, Alignment.center);

    expect(
      find.bySemanticsLabel(RegExp(r'^Message from Support Team')),
      findsOneWidget,
    );
  });

  testWidgets('manual refresh displays a new public staff reply',
      (tester) async {
    final repository = _FakeSupportRepository([
      _comment('customer', TicketCommentAuthorType.customer, 'Any update?'),
    ]);
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();
    expect(repository.commentReads, 1);

    repository.currentComments = [
      ...repository.currentComments,
      _comment('staff', TicketCommentAuthorType.staff, 'We fixed the line.'),
    ];
    await tester.tap(find.byTooltip('Refresh ticket'));
    await tester.pumpAndSettle();

    expect(repository.commentReads, 2);
    expect(find.text('We fixed the line.'), findsOneWidget);
    expect(find.text('Support Team'), findsOneWidget);
  });

  testWidgets('refresh failure retains the last conversation and offers retry',
      (tester) async {
    final repository = _FakeSupportRepository([
      _comment('customer', TicketCommentAuthorType.customer, 'Still visible'),
    ]);
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    repository.commentsError = ApiException('Temporary network problem');
    await tester.tap(find.byTooltip('Refresh ticket'));
    await tester.pumpAndSettle();

    expect(find.text('Still visible'), findsOneWidget);
    expect(
      find.text('Could not refresh replies. Showing the last update.'),
      findsOneWidget,
    );
    expect(find.text('Retry'), findsOneWidget);
  });

  testWidgets('prolonged socket failure shows the manual-refresh fallback', (
    tester,
  ) async {
    final repository = _FakeSupportRepository([]);
    await tester.pumpWidget(
      _app(
        repository,
        socketFactory: _FailingRealtimeSocket.new,
        timing: const TicketConversationTiming(
          unavailableAfter: Duration(milliseconds: 10),
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 11));

    expect(
      find.text('Live comment updates unavailable. Pull to refresh.'),
      findsOneWidget,
    );
  });
}
