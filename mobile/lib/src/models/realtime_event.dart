enum AppRealtimeEventType {
  supportTicketCommentChanged,
  connectionAck,
  heartbeat,
  unknown;

  factory AppRealtimeEventType.fromWire(Object? value) => switch (value) {
        'support_ticket_comment_changed' =>
          AppRealtimeEventType.supportTicketCommentChanged,
        'connection_ack' => AppRealtimeEventType.connectionAck,
        'heartbeat' => AppRealtimeEventType.heartbeat,
        _ => AppRealtimeEventType.unknown,
      };
}

enum SupportTicketCommentRealtimeChange {
  commentCreated,
  commentUpdated,
  commentDeleted,
  commentVisibilityChanged;

  factory SupportTicketCommentRealtimeChange.fromWire(Object? value) =>
      switch (value) {
        'comment_created' => SupportTicketCommentRealtimeChange.commentCreated,
        'comment_updated' => SupportTicketCommentRealtimeChange.commentUpdated,
        'comment_deleted' => SupportTicketCommentRealtimeChange.commentDeleted,
        'comment_visibility_changed' =>
          SupportTicketCommentRealtimeChange.commentVisibilityChanged,
        _ => throw const FormatException('Unknown ticket comment change'),
      };
}

class SupportTicketCommentRealtimeHint {
  const SupportTicketCommentRealtimeHint({
    required this.ticketId,
    required this.change,
    this.commentId,
  });

  final String ticketId;
  final SupportTicketCommentRealtimeChange change;
  final String? commentId;

  factory SupportTicketCommentRealtimeHint.fromJson(
    Map<String, dynamic> json,
  ) {
    final ticketId = _requiredUuid(json['ticket_id'], 'ticket_id');
    final rawCommentId = json['comment_id'];
    return SupportTicketCommentRealtimeHint(
      ticketId: ticketId,
      change: SupportTicketCommentRealtimeChange.fromWire(json['change']),
      commentId: rawCommentId == null
          ? null
          : _requiredUuid(rawCommentId, 'comment_id'),
    );
  }
}

/// Safe subset of the backend's versioned realtime envelope.
///
/// The arbitrary `data` map is deliberately not retained. Only the approved
/// identifier-only Support hint is decoded, so socket payloads can never be
/// rendered as ticket-comment content.
class AppRealtimeEvent {
  const AppRealtimeEvent({
    required this.eventId,
    required this.type,
    required this.topic,
    required this.timestamp,
    required this.refreshRequired,
    this.ticketCommentHint,
  });

  final String eventId;
  final AppRealtimeEventType type;
  final String topic;
  final DateTime timestamp;
  final bool refreshRequired;
  final SupportTicketCommentRealtimeHint? ticketCommentHint;

  factory AppRealtimeEvent.fromJson(Map<String, dynamic> json) {
    if (json['schema_version'] != 1) {
      throw const FormatException('Unsupported realtime schema version');
    }
    final eventId = _requiredUuid(json['event_id'], 'event_id');
    final topic = json['topic'];
    if (topic is! String || topic.trim().isEmpty) {
      throw const FormatException('Missing realtime topic');
    }
    final timestamp = DateTime.tryParse('${json['timestamp']}');
    if (timestamp == null) {
      throw const FormatException('Invalid realtime timestamp');
    }
    final refreshRequired = json['refresh_required'];
    if (refreshRequired is! bool) {
      throw const FormatException('Invalid refresh requirement');
    }

    final type = AppRealtimeEventType.fromWire(json['event']);
    SupportTicketCommentRealtimeHint? hint;
    if (type == AppRealtimeEventType.supportTicketCommentChanged) {
      final data = json['data'];
      if (data is! Map) {
        throw const FormatException('Missing ticket comment hint');
      }
      hint = SupportTicketCommentRealtimeHint.fromJson(
        data.cast<String, dynamic>(),
      );
    }

    return AppRealtimeEvent(
      eventId: eventId,
      type: type,
      topic: topic,
      timestamp: timestamp.toUtc(),
      refreshRequired: refreshRequired,
      ticketCommentHint: hint,
    );
  }
}

final RegExp _uuid = RegExp(
  r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$',
);

String _requiredUuid(Object? value, String field) {
  final text = value?.toString() ?? '';
  if (!_uuid.hasMatch(text)) throw FormatException('Invalid $field');
  return text.toLowerCase();
}
