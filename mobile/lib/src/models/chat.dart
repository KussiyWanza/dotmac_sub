/// Models for the live-chat bridge. The authenticated broker returns an opaque
/// visitor token plus either native Selfcare or temporary CRM transport URLs.
class ChatSession {
  ChatSession({
    required this.sessionId,
    required this.visitorToken,
    required this.apiBase,
    required this.wsUrl,
    this.conversationId,
  });

  final String sessionId;
  final String visitorToken;

  /// Absolute REST endpoint resolved at the API adapter boundary.
  final Uri apiBase;

  /// Absolute WebSocket endpoint resolved at the API adapter boundary.
  final Uri wsUrl;
  final String? conversationId;

  factory ChatSession.fromJson(
    Map<String, dynamic> json, {
    required Uri brokerBaseUri,
  }) =>
      ChatSession(
        sessionId: _requiredString(json, 'session_id'),
        visitorToken: _requiredString(json, 'visitor_token'),
        apiBase:
            _resolveHttpUri(_requiredString(json, 'api_base'), brokerBaseUri),
        wsUrl: _resolveWebSocketUri(
            _requiredString(json, 'ws_url'), brokerBaseUri),
        conversationId: _optionalString(json['conversation_id']),
      );

  static String _requiredString(Map<String, dynamic> json, String field) {
    final value = _optionalString(json[field]);
    if (value == null) {
      throw FormatException('Chat session is missing $field.');
    }
    return value;
  }

  static String? _optionalString(Object? value) {
    final text = value?.toString().trim();
    return text == null || text.isEmpty ? null : text;
  }

  static Uri _resolveHttpUri(String value, Uri brokerBaseUri) {
    final parsed = Uri.parse(value);
    final resolved =
        parsed.hasScheme ? parsed : brokerBaseUri.resolveUri(parsed);
    if (!resolved.hasScheme ||
        resolved.host.isEmpty ||
        (resolved.scheme != 'http' && resolved.scheme != 'https')) {
      throw const FormatException(
        'Chat REST endpoint is not a valid HTTP URL.',
      );
    }
    return resolved;
  }

  static Uri _resolveWebSocketUri(String value, Uri brokerBaseUri) {
    final parsed = Uri.parse(value);
    if (parsed.hasScheme) {
      if (parsed.host.isEmpty ||
          (parsed.scheme != 'ws' && parsed.scheme != 'wss')) {
        throw const FormatException(
          'Chat realtime endpoint is not a valid WebSocket URL.',
        );
      }
      return parsed;
    }

    final resolved = brokerBaseUri.resolveUri(parsed);
    final socketScheme = switch (resolved.scheme) {
      'http' => 'ws',
      'https' => 'wss',
      _ => null,
    };
    if (socketScheme == null || resolved.host.isEmpty) {
      throw const FormatException(
        'Chat realtime endpoint is not a valid WebSocket URL.',
      );
    }
    return resolved.replace(scheme: socketScheme);
  }
}

/// Delivery state of one of OUR messages (agent messages are always [sent]).
enum MessageStatus { sending, sent, failed }

class ChatMessage {
  ChatMessage({
    required this.id,
    required this.body,
    required this.fromAgent,
    this.authorName,
    this.authorAvatar,
    this.createdAt,
    this.readAt,
    this.status = MessageStatus.sent,
  });

  final String id;
  final String body;

  /// Delivery state — drives the "sending…/failed" indicator on our bubbles.
  final MessageStatus status;

  /// True when the message came from a support agent (CRM "outbound"); false
  /// for the subscriber's own messages.
  final bool fromAgent;
  final String? authorName;

  /// Agent's avatar URL (CRM `author_avatar`), present on outbound messages
  /// when the agent has a profile photo. Null for the subscriber's messages.
  final String? authorAvatar;
  final DateTime? createdAt;

  /// When an agent read this (our own) message — drives the "Seen" receipt.
  /// Null until read; only meaningful for the subscriber's own messages.
  final DateTime? readAt;

  ChatMessage copyWith({
    String? id,
    MessageStatus? status,
    DateTime? createdAt,
  }) =>
      ChatMessage(
        id: id ?? this.id,
        body: body,
        fromAgent: fromAgent,
        authorName: authorName,
        authorAvatar: authorAvatar,
        createdAt: createdAt ?? this.createdAt,
        readAt: readAt,
        status: status ?? this.status,
      );

  static DateTime? _parseDate(Object? v) =>
      v is String ? DateTime.tryParse(v) : null;

  static String? _str(Object? v) {
    final s = v?.toString();
    return (s == null || s.isEmpty) ? null : s;
  }

  /// From GET /session/{id}/messages (WidgetMessageRead).
  factory ChatMessage.fromHistory(Map<String, dynamic> j) => ChatMessage(
        id: (j['id'] ?? '').toString(),
        body: (j['body'] ?? '').toString(),
        fromAgent: j['direction'] == 'outbound',
        authorName: _str(j['author_name']),
        authorAvatar: _str(j['author_avatar']),
        createdAt: _parseDate(j['created_at']),
        readAt: _parseDate(j['read_at']),
      );

  /// From POST /session/{id}/message (WidgetMessageResponse) — our own message.
  factory ChatMessage.fromSendResponse(Map<String, dynamic> j) => ChatMessage(
        id: (j['message_id'] ?? '').toString(),
        body: (j['body'] ?? '').toString(),
        fromAgent: false,
        createdAt: _parseDate(j['created_at']),
      );

  /// From a `message_new` WebSocket event (broadcast_to_widget_visitor).
  factory ChatMessage.fromSocket(Map<String, dynamic> j) => ChatMessage(
        id: (j['message_id'] ?? j['id'] ?? '').toString(),
        body: (j['body'] ?? '').toString(),
        fromAgent: j['direction'] == 'outbound',
        authorName: _str(j['author_name']),
        authorAvatar: _str(j['author_avatar']),
        createdAt: _parseDate(j['created_at']),
      );
}
