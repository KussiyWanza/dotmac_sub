import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/status.dart' as ws_status;
import 'package:web_socket_channel/web_socket_channel.dart';

typedef ChatEventHandler = void Function(Map<String, dynamic> event);

/// Thin wrapper over the selected native Selfcare or temporary CRM visitor
/// WebSocket. Decodes JSON events and exposes a simple send/close API.
/// Connection lifecycle, reconnection, and event interpretation live in
/// [ChatController].
class ChatSocket {
  ChatSocket({required this.wsUrl, required this.visitorToken});

  final Uri wsUrl;
  final String visitorToken;

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _sub;

  bool get isConnected => _channel != null;

  /// Open the socket and start delivering decoded events. Resolves once the
  /// handshake completes (throws on failure, so the caller can schedule a
  /// retry). [onEvent] receives each JSON object; [onClosed] fires once on
  /// disconnect or error.
  Future<void> connect({
    required ChatEventHandler onEvent,
    required void Function() onClosed,
  }) async {
    final uri = wsUrl.replace(queryParameters: {'token': visitorToken});
    final channel = WebSocketChannel.connect(uri);
    _channel = channel;

    var closed = false;
    void fireClosed() {
      if (closed) return;
      closed = true;
      onClosed();
    }

    _sub = channel.stream.listen(
      (data) {
        if (data is! String) return;
        try {
          final decoded = jsonDecode(data);
          if (decoded is Map<String, dynamic>) onEvent(decoded);
        } catch (_) {
          // Ignore malformed frames.
        }
      },
      onDone: fireClosed,
      onError: (_) => fireClosed(),
      cancelOnError: true,
    );

    await channel.ready; // throws on handshake failure
  }

  void send(Map<String, dynamic> message) {
    try {
      _channel?.sink.add(jsonEncode(message));
    } catch (_) {
      // Sink closed mid-send; the close handler will trigger a reconnect.
    }
  }

  Future<void> close() async {
    await _sub?.cancel();
    _sub = null;
    try {
      await _channel?.sink.close(ws_status.normalClosure);
    } catch (_) {
      // Already closing.
    }
    _channel = null;
  }
}
