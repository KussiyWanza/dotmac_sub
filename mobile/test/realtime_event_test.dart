import 'package:dotmac_portal/src/models/realtime_event.dart';
import 'package:flutter_test/flutter_test.dart';

const _ticketId = 'e94a93e9-bdf0-49e7-a300-9d5226ba86b7';
const _commentId = 'ab9fa0cb-a06a-4ec7-a570-0025d67c713f';

Map<String, dynamic> _envelope({
  String event = 'support_ticket_comment_changed',
  Map<String, dynamic>? data,
}) =>
    {
      'schema_version': 1,
      'event_id': '4031e709-ceba-47c7-a024-0209c3cf7312',
      'event': event,
      'topic': 'principal:subscriber-id',
      'timestamp': '2026-09-14T12:00:00Z',
      'refresh_required': true,
      'data': data ??
          {
            'ticket_id': _ticketId,
            'change': 'comment_created',
            'comment_id': _commentId,
          },
    };

void main() {
  test('decodes the typed identifier-only ticket comment hint', () {
    final event = AppRealtimeEvent.fromJson(_envelope());

    expect(event.type, AppRealtimeEventType.supportTicketCommentChanged);
    expect(event.ticketCommentHint?.ticketId, _ticketId);
    expect(
      event.ticketCommentHint?.change,
      SupportTicketCommentRealtimeChange.commentCreated,
    );
    expect(event.ticketCommentHint?.commentId, _commentId);
  });

  test('does not retain arbitrary socket content', () {
    final event = AppRealtimeEvent.fromJson(
      _envelope(
        data: {
          'ticket_id': _ticketId,
          'change': 'comment_updated',
          'comment_id': _commentId,
          'body': 'must not reach the UI',
          'author': {'name': 'private'},
          'attachments': ['private'],
        },
      ),
    );

    expect(event.ticketCommentHint?.ticketId, _ticketId);
    expect(
      event.ticketCommentHint?.change,
      SupportTicketCommentRealtimeChange.commentUpdated,
    );
  });

  test('rejects malformed identifiers and unsupported schemas', () {
    expect(
      () => AppRealtimeEvent.fromJson(
        _envelope(data: {'ticket_id': 'bad', 'change': 'comment_created'}),
      ),
      throwsFormatException,
    );
    expect(
      () => AppRealtimeEvent.fromJson({..._envelope(), 'schema_version': 2}),
      throwsFormatException,
    );
  });
}
