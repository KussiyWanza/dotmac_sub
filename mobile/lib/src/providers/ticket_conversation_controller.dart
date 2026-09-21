import 'dart:async';
import 'dart:math';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../config/env.dart';
import '../models/page.dart' as models;
import '../models/realtime_event.dart';
import '../models/ticket.dart';
import '../repositories/app_realtime_socket.dart';
import 'auth_controller.dart';
import 'data_providers.dart';

enum TicketLiveCommentStatus {
  connecting,
  connected,
  reconnecting,
  unavailable,
  paused,
}

class TicketConversationState {
  const TicketConversationState({
    this.comments = const AsyncLoading(),
    this.liveStatus = TicketLiveCommentStatus.connecting,
    this.isRefreshing = false,
    this.lastSuccessfulRefresh,
  });

  final AsyncValue<models.Page<TicketComment>> comments;
  final TicketLiveCommentStatus liveStatus;
  final bool isRefreshing;
  final DateTime? lastSuccessfulRefresh;

  TicketConversationState copyWith({
    AsyncValue<models.Page<TicketComment>>? comments,
    TicketLiveCommentStatus? liveStatus,
    bool? isRefreshing,
    DateTime? lastSuccessfulRefresh,
  }) =>
      TicketConversationState(
        comments: comments ?? this.comments,
        liveStatus: liveStatus ?? this.liveStatus,
        isRefreshing: isRefreshing ?? this.isRefreshing,
        lastSuccessfulRefresh:
            lastSuccessfulRefresh ?? this.lastSuccessfulRefresh,
      );
}

class TicketConversationTiming {
  const TicketConversationTiming({
    this.eventDebounce = const Duration(milliseconds: 250),
    this.unavailableAfter = const Duration(seconds: 30),
  });

  final Duration eventDebounce;
  final Duration unavailableAfter;

  Duration reconnectDelay(int attempt, double jitterUnit) {
    const seconds = [2, 4, 8, 16, 32];
    final base = seconds[min(attempt, seconds.length - 1)];
    final boundedJitter = jitterUnit.clamp(0.0, 1.0);
    final milliseconds = (base * 1000 * (0.8 + boundedJitter * 0.2)).round();
    return Duration(milliseconds: min(milliseconds, 32000));
  }
}

typedef AppRealtimeSocketFactory = AppRealtimeSocket Function();

final appRealtimeSocketFactoryProvider = Provider<AppRealtimeSocketFactory>((
  ref,
) {
  final tokenStorage = ref.watch(tokenStorageProvider);
  final endpoint = appRealtimeWebSocketUri(Env.apiBaseUrl);
  return () => AuthenticatedAppRealtimeSocket(
        endpoint: endpoint,
        tokenStorage: tokenStorage,
      );
});

final ticketConversationTimingProvider = Provider<TicketConversationTiming>(
  (_) => const TicketConversationTiming(),
);

final ticketConversationJitterProvider = Provider<double Function()>(
  (_) {
    final random = Random();
    return random.nextDouble;
  },
);

class TicketConversationController
    extends StateNotifier<TicketConversationState> with WidgetsBindingObserver {
  TicketConversationController({
    required this.ref,
    required this.ticketId,
  }) : super(const TicketConversationState()) {
    _socket = ref.read(appRealtimeSocketFactoryProvider)();
    _signalSubscription = _socket.signals.listen(_handleSignal);
    WidgetsBinding.instance.addObserver(this);
    ref.onDispose(_dispose);
    scheduleMicrotask(_start);
  }

  final Ref<TicketConversationState> ref;
  final String ticketId;

  late final AppRealtimeSocket _socket;
  late final StreamSubscription<AppRealtimeSignal> _signalSubscription;
  Timer? _eventDebounce;
  Timer? _reconnect;
  Timer? _unavailable;
  bool _foreground = true;
  bool _disposed = false;
  bool _connecting = false;
  bool _refreshing = false;
  bool _refreshPending = false;
  bool _commentChangePending = false;
  bool _prolongedDisconnect = false;
  int _reconnectAttempt = 0;
  DateTime? _connectionStartedAt;
  Future<void>? _activeRefresh;

  Future<void> _start() async {
    unawaited(refreshComments());
    await _connect();
  }

  Future<void> _connect() async {
    if (_disposed || !_foreground || _connecting) return;
    _connecting = true;
    _connectionStartedAt = DateTime.now().toUtc();
    if (state.liveStatus != TicketLiveCommentStatus.connecting) {
      state = state.copyWith(
        liveStatus: _prolongedDisconnect
            ? TicketLiveCommentStatus.unavailable
            : TicketLiveCommentStatus.reconnecting,
      );
    }
    try {
      await _socket.connect();
    } catch (_) {
      _beginReconnect();
    } finally {
      _connecting = false;
    }
  }

  void _handleSignal(AppRealtimeSignal signal) {
    if (_disposed || !_foreground) return;
    switch (signal) {
      case AppRealtimeConnectionSignal(:final status):
        if (status == AppRealtimeConnectionStatus.connected) {
          _reconnectAttempt = 0;
          _reconnect?.cancel();
          _reconnect = null;
          _unavailable?.cancel();
          _unavailable = null;
          _prolongedDisconnect = false;
          state = state.copyWith(liveStatus: TicketLiveCommentStatus.connected);
        } else if (status == AppRealtimeConnectionStatus.disconnected) {
          state = state.copyWith(
            liveStatus: _prolongedDisconnect
                ? TicketLiveCommentStatus.unavailable
                : TicketLiveCommentStatus.reconnecting,
          );
        }
      case AppRealtimeEventSignal(:final event):
        if (event.type == AppRealtimeEventType.connectionAck) {
          _requestConnectionCatchUp();
        } else if (event.type ==
                AppRealtimeEventType.supportTicketCommentChanged &&
            event.refreshRequired &&
            event.topic.startsWith('principal:') &&
            event.ticketCommentHint?.ticketId == ticketId.toLowerCase()) {
          _requestCommentsRefresh();
        }
      case AppRealtimeClosedSignal(:final intentional):
        if (!intentional) _beginReconnect();
      case AppRealtimeErrorSignal():
        // Closure drives reconnect. A malformed unrelated frame is ignorable.
        break;
    }
  }

  void _requestCommentsRefresh() {
    _commentChangePending = true;
    _eventDebounce?.cancel();
    _eventDebounce = Timer(
      ref.read(ticketConversationTimingProvider).eventDebounce,
      () {
        _commentChangePending = false;
        unawaited(refreshComments());
      },
    );
  }

  void _requestConnectionCatchUp() {
    if (_commentChangePending) return;
    final connectionStartedAt = _connectionStartedAt;
    _eventDebounce?.cancel();
    _eventDebounce = Timer(
      ref.read(ticketConversationTimingProvider).eventDebounce,
      () {
        final refreshedAt = state.lastSuccessfulRefresh;
        if (connectionStartedAt != null &&
            refreshedAt != null &&
            !refreshedAt.isBefore(connectionStartedAt)) {
          return;
        }
        unawaited(refreshComments());
      },
    );
  }

  Future<void> refreshComments() {
    if (_disposed) return Future.value();
    if (_refreshing) {
      _refreshPending = true;
      return _activeRefresh ?? Future.value();
    }
    final refresh = _runRefreshLoop();
    _activeRefresh = refresh;
    return refresh;
  }

  Future<void> _runRefreshLoop() async {
    _refreshing = true;
    try {
      do {
        _refreshPending = false;
        await _refreshOnce();
      } while (_refreshPending && !_disposed);
    } finally {
      _refreshing = false;
      _activeRefresh = null;
    }
  }

  Future<void> _refreshOnce() async {
    final previous = state.comments;
    state = state.copyWith(
      comments: const AsyncLoading<models.Page<TicketComment>>()
          .copyWithPrevious(previous),
      isRefreshing: true,
    );
    try {
      final comments =
          await ref.read(supportRepositoryProvider).comments(ticketId);
      if (_disposed) return;
      state = state.copyWith(
        comments: AsyncData(comments),
        isRefreshing: false,
        lastSuccessfulRefresh: DateTime.now().toUtc(),
      );
    } catch (error, stackTrace) {
      if (_disposed) return;
      state = state.copyWith(
        comments: AsyncError<models.Page<TicketComment>>(error, stackTrace)
            .copyWithPrevious(previous),
        isRefreshing: false,
      );
    }
  }

  void _beginReconnect() {
    if (_disposed || !_foreground || _reconnect?.isActive == true) return;
    state = state.copyWith(
      liveStatus: _prolongedDisconnect
          ? TicketLiveCommentStatus.unavailable
          : TicketLiveCommentStatus.reconnecting,
    );
    _unavailable ??= Timer(
      ref.read(ticketConversationTimingProvider).unavailableAfter,
      () {
        if (!_disposed &&
            _foreground &&
            state.liveStatus != TicketLiveCommentStatus.connected) {
          _prolongedDisconnect = true;
          state = state.copyWith(
            liveStatus: TicketLiveCommentStatus.unavailable,
          );
        }
      },
    );
    final timing = ref.read(ticketConversationTimingProvider);
    final jitter = ref.read(ticketConversationJitterProvider)();
    final delay = timing.reconnectDelay(_reconnectAttempt, jitter);
    _reconnectAttempt += 1;
    _reconnect = Timer(delay, () {
      _reconnect = null;
      unawaited(_connect());
    });
  }

  @override
  // Not named `state` to avoid shadowing the StateNotifier's state property.
  // ignore: avoid_renaming_method_parameters
  void didChangeAppLifecycleState(AppLifecycleState lifecycle) {
    if (lifecycle == AppLifecycleState.resumed) {
      if (_foreground) return;
      _foreground = true;
      _reconnectAttempt = 0;
      state = state.copyWith(liveStatus: TicketLiveCommentStatus.reconnecting);
      unawaited(refreshComments());
      unawaited(_connect());
    } else if (lifecycle == AppLifecycleState.paused ||
        lifecycle == AppLifecycleState.hidden ||
        lifecycle == AppLifecycleState.detached) {
      if (!_foreground) return;
      _foreground = false;
      _eventDebounce?.cancel();
      _commentChangePending = false;
      _reconnect?.cancel();
      _reconnect = null;
      _unavailable?.cancel();
      _unavailable = null;
      _prolongedDisconnect = false;
      state = state.copyWith(liveStatus: TicketLiveCommentStatus.paused);
      unawaited(_socket.close());
    }
  }

  void _dispose() {
    if (_disposed) return;
    _disposed = true;
    _eventDebounce?.cancel();
    _commentChangePending = false;
    _reconnect?.cancel();
    _unavailable?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    unawaited(_signalSubscription.cancel());
    unawaited(_socket.dispose());
  }
}

final ticketConversationProvider = StateNotifierProvider.autoDispose
    .family<TicketConversationController, TicketConversationState, String>(
  (ref, ticketId) => TicketConversationController(
    ref: ref,
    ticketId: ticketId,
  ),
);
