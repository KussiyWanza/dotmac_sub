import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/api_exception.dart';
import '../models/chat.dart';
import '../repositories/chat_repository.dart';
import '../repositories/chat_socket.dart';
import 'data_providers.dart';

/// Immutable snapshot of one live-chat conversation.
class ChatState {
  const ChatState({
    this.session,
    this.messages = const [],
    this.loading = true,
    this.sending = false,
    this.error,
    this.agentTyping = false,
    this.agentReadAt,
    this.connected = false,
    this.unread = 0,
  });

  final ChatSession? session;
  final List<ChatMessage> messages;
  final bool loading;
  final bool sending;
  final String? error;

  /// True while a support agent is composing (pushed over the CRM widget
  /// WebSocket as USER_TYPING).
  final bool agentTyping;

  /// Watermark of the latest of OUR messages an agent has read — drives the
  /// "Seen" receipt. Sourced from message read_at (poll) and the
  /// CONVERSATION_READ WebSocket event (instant).
  final DateTime? agentReadAt;

  /// Whether the realtime WebSocket is currently connected. False while
  /// reconnecting/backing off — drives the "reconnecting…" indicator.
  final bool connected;

  /// Agent messages received while the chat view wasn't open — drives the
  /// Support tab / Live-chat badge. Reset to 0 when the chat view is shown.
  final int unread;

  ChatState copyWith({
    ChatSession? session,
    List<ChatMessage>? messages,
    bool? loading,
    bool? sending,
    Object? error = _unset,
    bool? agentTyping,
    Object? agentReadAt = _unset,
    bool? connected,
    int? unread,
  }) {
    return ChatState(
      session: session ?? this.session,
      messages: messages ?? this.messages,
      loading: loading ?? this.loading,
      sending: sending ?? this.sending,
      error: identical(error, _unset) ? this.error : error as String?,
      agentTyping: agentTyping ?? this.agentTyping,
      agentReadAt: identical(agentReadAt, _unset)
          ? this.agentReadAt
          : agentReadAt as DateTime?,
      connected: connected ?? this.connected,
      unread: unread ?? this.unread,
    );
  }

  static const _unset = Object();
}

/// Owns one chat conversation for the lifetime of the app session. Kept ALIVE
/// (a plain, non-autoDispose Notifier) so navigating away from the chat screen
/// — or switching bottom-nav tabs — does not tear the conversation down: the
/// session, history, WebSocket, and polling all survive.
///
/// Delivery uses a CRM widget WebSocket for immediacy (instant messages, "agent
/// is typing", and "Seen") with a slow REST poll as the reliability backbone
/// (reconciles anything missed while the socket was down). Both pause in the
/// background, where FCM push takes over.
///
/// Keyed by the broker endpoint so the customer (`/me/chat/session`) and
/// reseller (`/reseller/chat/session`) conversations are independent.
class ChatController extends FamilyNotifier<ChatState, String>
    with WidgetsBindingObserver {
  Timer? _poll;
  Timer? _reconnect;
  Timer? _agentTypingClear;
  Timer? _typingStop;
  ChatSocket? _socket;
  String? _conversationId;
  bool _typingActive = false;
  bool _starting = false;
  bool _foreground = true;
  int _retry = 0;
  int _tempSeq = 0;

  ChatRepository get _repo => ref.read(chatRepositoryProvider);

  @override
  ChatState build(String endpoint) {
    WidgetsBinding.instance.addObserver(this);
    ref.onDispose(() {
      _poll?.cancel();
      _reconnect?.cancel();
      _agentTypingClear?.cancel();
      _typingStop?.cancel();
      _socket?.close();
      WidgetsBinding.instance.removeObserver(this);
    });
    scheduleMicrotask(_start);
    return const ChatState();
  }

  @override
  // Not named `state` to avoid shadowing the Notifier's `state` property.
  // ignore: avoid_renaming_method_parameters
  void didChangeAppLifecycleState(AppLifecycleState lifecycle) {
    if (lifecycle == AppLifecycleState.resumed) {
      _foreground = true;
      _startPolling();
      _connectSocket();
      unawaited(_refresh());
    } else if (lifecycle == AppLifecycleState.paused) {
      _foreground = false;
      _poll?.cancel();
      _reconnect?.cancel();
      _socket?.close();
      _socket = null;
    }
  }

  Future<void> _start() async {
    if (_starting || state.session != null) return;
    _starting = true;
    try {
      final session = await _repo.openSession(endpoint: arg);
      final history = await _repo.history(session);
      _conversationId = session.conversationId;
      state = state.copyWith(
        session: session,
        messages: history,
        loading: false,
        error: null,
        agentReadAt: _readWatermark(history),
      );
      unawaited(_repo.markRead(session));
      _startPolling();
      _connectSocket();
    } on ApiException catch (error) {
      state = state.copyWith(loading: false, error: error.message);
    } catch (_) {
      state = state.copyWith(
        loading: false,
        error: 'Chat is unavailable right now. Please try again later.',
      );
    } finally {
      _starting = false;
    }
  }

  /// Re-broker after a failure (used by the screen's retry action).
  Future<void> retry() async {
    state = state.copyWith(loading: true, error: null);
    await _start();
  }

  // --- REST poll (reliability backbone) ---------------------------------

  void _startPolling() {
    _poll?.cancel();
    // Slow when the socket carries live updates; the socket reconnect logic
    // handles immediacy. This is the catch-up safety net.
    _poll = Timer.periodic(const Duration(seconds: 12), (_) => _refresh());
  }

  Future<void> _refresh() async {
    final session = state.session;
    if (session == null) return;
    try {
      final history = await _repo.history(session);
      final grew = history.length != state.messages.length;
      state = state.copyWith(
        messages: history,
        agentReadAt: _laterReadAt(state.agentReadAt, _readWatermark(history)),
      );
      if (grew) unawaited(_repo.markRead(session));
    } catch (_) {
      // Transient; the next tick retries.
    }
  }

  // --- WebSocket (immediacy) --------------------------------------------

  Future<void> _connectSocket() async {
    final session = state.session;
    if (!_foreground || session == null) return;
    if (_socket != null) return;
    final socket = ChatSocket(
      wsUrl: session.wsUrl,
      visitorToken: session.visitorToken,
    );
    _socket = socket;
    try {
      await socket.connect(onEvent: _onSocketEvent, onClosed: _onSocketClosed);
      _retry = 0;
      state = state.copyWith(connected: true);
      _subscribe();
    } catch (_) {
      _socket = null;
      state = state.copyWith(connected: false);
      _scheduleReconnect();
    }
  }

  void _onSocketClosed() {
    _socket = null;
    state = state.copyWith(connected: false);
    if (_foreground) _scheduleReconnect();
  }

  void _scheduleReconnect() {
    _reconnect?.cancel();
    _retry = (_retry + 1).clamp(1, 5);
    final delay = Duration(seconds: 2 << (_retry - 1)); // 2,4,8,16,32
    _reconnect = Timer(delay, () {
      unawaited(_refresh()); // catch up anything missed
      unawaited(_connectSocket());
    });
  }

  void _subscribe() {
    final id = _conversationId;
    if (id != null) _socket?.send({'type': 'subscribe', 'conversation_id': id});
  }

  void _onSocketEvent(Map<String, dynamic> event) {
    final type = event['event'];
    final data = (event['data'] as Map?)?.cast<String, dynamic>() ?? const {};
    switch (type) {
      case 'message_new':
        _onIncomingMessage(data);
        break;
      case 'user_typing':
        // Ignore our own echo; only agents drive the indicator.
        if (data['is_visitor'] == true) break;
        _setAgentTyping(data['is_typing'] != false);
        break;
      case 'conversation_read':
        final readUpTo = DateTime.tryParse('${data['read_up_to']}');
        if (readUpTo != null) {
          state = state.copyWith(
            agentReadAt: _laterReadAt(state.agentReadAt, readUpTo),
          );
        }
        break;
      default:
        break; // connection_ack, heartbeat, presence, …
    }
  }

  void _onIncomingMessage(Map<String, dynamic> data) {
    final msg = ChatMessage.fromSocket(data);
    if (msg.id.isEmpty) return;
    // Dedupe against the poll / our own optimistic append.
    if (state.messages.any((m) => m.id == msg.id)) return;
    state = state.copyWith(
      messages: [...state.messages, msg],
      agentTyping: msg.fromAgent ? false : state.agentTyping,
      // Bump the unread badge for agent replies; the chat view clears it.
      unread: msg.fromAgent ? state.unread + 1 : state.unread,
    );
    final session = state.session;
    if (msg.fromAgent && session != null) unawaited(_repo.markRead(session));
  }

  /// Called by the chat view while it's on screen — clears the unread badge and
  /// marks the conversation read.
  void markViewed() {
    if (state.unread != 0) state = state.copyWith(unread: 0);
    final session = state.session;
    if (session != null) unawaited(_repo.markRead(session));
  }

  void _setAgentTyping(bool typing) {
    state = state.copyWith(agentTyping: typing);
    _agentTypingClear?.cancel();
    if (typing) {
      // Self-heal if the "stopped typing" frame is dropped.
      _agentTypingClear = Timer(const Duration(seconds: 6), () {
        state = state.copyWith(agentTyping: false);
      });
    }
  }

  // --- Outbound typing (throttled) --------------------------------------

  /// Called by the composer on each keystroke. Emits a single typing=true and
  /// auto-sends typing=false after a short idle.
  void notifyTyping() {
    if (!_typingActive) {
      _typingActive = true;
      _socket?.send({'type': 'typing', 'is_typing': true});
    }
    _typingStop?.cancel();
    _typingStop = Timer(const Duration(seconds: 3), _stopTyping);
  }

  void _stopTyping() {
    _typingStop?.cancel();
    if (_typingActive) {
      _typingActive = false;
      _socket?.send({'type': 'typing', 'is_typing': false});
    }
  }

  // --- Helpers ----------------------------------------------------------

  /// Latest time an agent read one of OUR messages — the "Seen" watermark.
  static DateTime? _readWatermark(List<ChatMessage> messages) {
    DateTime? latest;
    for (final m in messages) {
      if (m.fromAgent) continue;
      final r = m.readAt;
      if (r != null && (latest == null || r.isAfter(latest))) latest = r;
    }
    return latest;
  }

  static DateTime? _laterReadAt(DateTime? a, DateTime? b) {
    if (a == null) return b;
    if (b == null) return a;
    return a.isAfter(b) ? a : b;
  }

  Future<void> send(String text) async {
    final session = state.session;
    final body = text.trim();
    if (session == null || body.isEmpty) return;
    _stopTyping();
    // Optimistic: show the message immediately as "sending", then flip to sent
    // on ACK or failed on error (a failed bubble can be tapped to retry).
    final tempId = 'temp-${++_tempSeq}';
    final optimistic = ChatMessage(
      id: tempId,
      body: body,
      fromAgent: false,
      createdAt: DateTime.now(),
      status: MessageStatus.sending,
    );
    state = state.copyWith(
      messages: [...state.messages, optimistic],
      sending: true,
    );
    try {
      final result = await _repo.send(session, body);
      final seen = <String>{};
      final msgs = <ChatMessage>[];
      for (final m in state.messages) {
        final mapped = m.id == tempId
            ? result.message.copyWith(status: MessageStatus.sent)
            : m;
        if (seen.add(mapped.id)) msgs.add(mapped); // dedupe vs WS echo
      }
      state = state.copyWith(messages: msgs, sending: false);
      final convId = result.conversationId;
      if (convId != null && convId != _conversationId) {
        _conversationId = convId;
        _subscribe();
      }
    } catch (_) {
      state = state.copyWith(
        messages: [
          for (final m in state.messages)
            if (m.id == tempId) m.copyWith(status: MessageStatus.failed) else m,
        ],
        sending: false,
      );
      rethrow; // the screen surfaces a snackbar
    }
  }

  /// Re-send a previously failed message (tapped in the log).
  Future<void> retryFailed(ChatMessage failed) async {
    state = state.copyWith(
      messages: state.messages.where((m) => m.id != failed.id).toList(),
    );
    await send(failed.body);
  }
}

/// Persistent per-endpoint chat conversation. Not autoDispose: the conversation
/// outlives any single screen so leaving Support never ends the chat.
final chatControllerProvider =
    NotifierProvider.family<ChatController, ChatState, String>(
  ChatController.new,
);
