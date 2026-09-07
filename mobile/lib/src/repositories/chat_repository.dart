import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/chat.dart';

/// Origin presented to temporary CRM widget endpoints (must be in the
/// ChatWidgetConfig.allowed_domains). Overridable at build time.
const String _crmOrigin = String.fromEnvironment(
  'CHAT_CRM_ORIGIN',
  defaultValue: 'https://app.dotmac.io',
);

/// Live chat. Opens a session through the authenticated Sub broker, then talks
/// to the selected native Selfcare or temporary CRM visitor transport with the
/// returned opaque token.
///
/// Foreground delivery uses WebSocket events with REST polling as a reliability
/// fallback; background delivery uses the server-owned push consequence.
class ChatRepository {
  ChatRepository(this._dio);

  /// The sub API client (carries the bearer/session auth).
  final Dio _dio;

  /// A separate visitor client scoped to one session's opaque token.
  Dio? _visitor;

  /// Open (or resume) a session. [endpoint] is the broker path:
  /// `/me/chat/session` (customer) or `/reseller/chat/session` (reseller).
  Future<ChatSession> openSession({
    String endpoint = '/me/chat/session',
  }) async {
    final data = await guard(() => _dio.post(endpoint, data: const {}));
    final session = ChatSession.fromJson(
      data as Map<String, dynamic>,
      brokerBaseUri: Uri.parse(_dio.options.baseUrl),
    );
    _visitor = Dio(
      BaseOptions(
        baseUrl: session.apiBase.toString(),
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(seconds: 20),
        contentType: Headers.jsonContentType,
        headers: {
          'X-Visitor-Token': session.visitorToken,
          // Native clients send no browser Origin. Temporary CRM widget endpoints
          // enforce an allowed-domains check, so present the configured app
          // origin; the native Selfcare transport safely ignores it.
          'Origin': _crmOrigin,
        },
        validateStatus: (s) => s != null && s < 500,
      ),
    );
    return session;
  }

  Dio get _visitorClient {
    final client = _visitor;
    if (client == null) {
      throw StateError(
        'openSession() must be called before using the chat visitor API',
      );
    }
    return client;
  }

  Future<List<ChatMessage>> history(ChatSession s, {int limit = 50}) async {
    final data = await guard(
      () => _visitorClient.get(
        '/session/${s.sessionId}/messages',
        queryParameters: {'limit': limit},
      ),
    );
    final list =
        (data as Map<String, dynamic>)['messages'] as List? ?? const [];
    return list
        .map((m) => ChatMessage.fromHistory(m as Map<String, dynamic>))
        .toList();
  }

  /// Returns the sent message plus the (possibly newly created) conversation id,
  /// so the caller can subscribe a brand-new conversation over the WebSocket.
  Future<({ChatMessage message, String? conversationId})> send(
    ChatSession s,
    String body,
  ) async {
    final data = await guard(
      () => _visitorClient.post(
        '/session/${s.sessionId}/message',
        data: {'body': body},
      ),
    ) as Map<String, dynamic>;
    return (
      message: ChatMessage.fromSendResponse(data),
      conversationId: data['conversation_id']?.toString(),
    );
  }

  Future<void> markRead(ChatSession s) async {
    await guard(() => _visitorClient.post('/session/${s.sessionId}/read'));
  }
}
