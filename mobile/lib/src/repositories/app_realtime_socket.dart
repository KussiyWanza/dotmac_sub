import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/status.dart' as ws_status;
import 'package:web_socket_channel/web_socket_channel.dart';

import '../core/token_storage.dart';
import '../models/realtime_event.dart';

enum AppRealtimeConnectionStatus { connecting, connected, disconnected }

sealed class AppRealtimeSignal {
  const AppRealtimeSignal();
}

class AppRealtimeConnectionSignal extends AppRealtimeSignal {
  const AppRealtimeConnectionSignal(this.status);

  final AppRealtimeConnectionStatus status;
}

class AppRealtimeEventSignal extends AppRealtimeSignal {
  const AppRealtimeEventSignal(this.event);

  final AppRealtimeEvent event;
}

class AppRealtimeErrorSignal extends AppRealtimeSignal {
  const AppRealtimeErrorSignal(this.error, this.stackTrace);

  final Object error;
  final StackTrace stackTrace;
}

class AppRealtimeClosedSignal extends AppRealtimeSignal {
  const AppRealtimeClosedSignal({required this.intentional});

  final bool intentional;
}

abstract interface class AppRealtimeSocket {
  Stream<AppRealtimeSignal> get signals;

  Future<void> connect();

  Future<void> close();

  Future<void> dispose();
}

abstract interface class AppRealtimeChannel {
  Stream<dynamic> get stream;

  Future<void> get ready;

  void add(Object data);

  Future<void> close([int? closeCode]);
}

class _WebSocketAppRealtimeChannel implements AppRealtimeChannel {
  _WebSocketAppRealtimeChannel(this.channel);

  final WebSocketChannel channel;

  @override
  Stream<dynamic> get stream => channel.stream;

  @override
  Future<void> get ready => channel.ready;

  @override
  void add(Object data) => channel.sink.add(data);

  @override
  Future<void> close([int? closeCode]) => channel.sink.close(closeCode);
}

typedef AppRealtimeChannelConnector = AppRealtimeChannel Function(
  Uri uri, {
  Iterable<String>? protocols,
});

AppRealtimeChannel _connectChannel(
  Uri uri, {
  Iterable<String>? protocols,
}) =>
    _WebSocketAppRealtimeChannel(
      WebSocketChannel.connect(uri, protocols: protocols),
    );

Uri appRealtimeWebSocketUri(String apiBaseUrl) {
  final base = Uri.parse(apiBaseUrl);
  final scheme = switch (base.scheme.toLowerCase()) {
    'https' => 'wss',
    'http' => 'ws',
    _ => throw ArgumentError.value(apiBaseUrl, 'apiBaseUrl'),
  };
  return Uri(
    scheme: scheme,
    host: base.host,
    port: base.hasPort ? base.port : null,
    path: '/ws/inbox',
  );
}

class AuthenticatedAppRealtimeSocket implements AppRealtimeSocket {
  AuthenticatedAppRealtimeSocket({
    required this.endpoint,
    required this.tokenStorage,
    AppRealtimeChannelConnector? connector,
    this.heartbeatInterval = const Duration(seconds: 30),
  }) : _connector = connector ?? _connectChannel;

  final Uri endpoint;
  final TokenStorage tokenStorage;
  final Duration heartbeatInterval;
  final AppRealtimeChannelConnector _connector;

  final StreamController<AppRealtimeSignal> _signals =
      StreamController<AppRealtimeSignal>.broadcast(sync: true);
  AppRealtimeChannel? _channel;
  StreamSubscription<dynamic>? _subscription;
  Timer? _heartbeat;
  int _generation = 0;
  int? _closedGeneration;
  bool _intentionalClose = false;
  bool _disposed = false;

  @override
  Stream<AppRealtimeSignal> get signals => _signals.stream;

  @override
  Future<void> connect() async {
    if (_disposed) throw StateError('Realtime socket is disposed');
    if (_channel != null) return;
    _intentionalClose = false;
    _signals.add(
      const AppRealtimeConnectionSignal(
        AppRealtimeConnectionStatus.connecting,
      ),
    );

    final accessToken = await tokenStorage.readAccessToken();
    if (accessToken == null || accessToken.isEmpty) {
      throw StateError('Authenticated realtime session is unavailable');
    }

    final generation = ++_generation;
    _closedGeneration = null;
    final channel = _connector(
      endpoint,
      protocols: ['dotmac-auth', accessToken],
    );
    _channel = channel;
    _subscription = channel.stream.listen(
      (frame) => _onFrame(frame),
      onDone: () => unawaited(_onClosed(generation)),
      onError: (Object error, StackTrace stackTrace) {
        _signals.add(AppRealtimeErrorSignal(error, stackTrace));
        unawaited(_onClosed(generation));
      },
      cancelOnError: true,
    );

    try {
      await channel.ready;
      if (_disposed || generation != _generation) return;
      _signals.add(
        const AppRealtimeConnectionSignal(
          AppRealtimeConnectionStatus.connected,
        ),
      );
      _heartbeat?.cancel();
      _heartbeat = Timer.periodic(heartbeatInterval, (_) => _sendPing());
    } catch (error, stackTrace) {
      _signals.add(AppRealtimeErrorSignal(error, stackTrace));
      await _onClosed(generation);
      rethrow;
    }
  }

  void _onFrame(Object? frame) {
    if (frame is! String) return;
    try {
      final decoded = jsonDecode(frame);
      if (decoded is! Map) {
        throw const FormatException('Invalid realtime frame');
      }
      final event = AppRealtimeEvent.fromJson(decoded.cast<String, dynamic>());
      _signals.add(AppRealtimeEventSignal(event));
    } catch (error, stackTrace) {
      _signals.add(AppRealtimeErrorSignal(error, stackTrace));
    }
  }

  void _sendPing() {
    try {
      _channel?.add(jsonEncode(const {'type': 'ping'}));
    } catch (error, stackTrace) {
      _signals.add(AppRealtimeErrorSignal(error, stackTrace));
    }
  }

  Future<void> _onClosed(int generation) async {
    if (generation != _generation || _closedGeneration == generation) return;
    _closedGeneration = generation;
    _heartbeat?.cancel();
    _heartbeat = null;
    await _subscription?.cancel();
    _subscription = null;
    _channel = null;
    if (!_disposed) {
      _signals.add(
        const AppRealtimeConnectionSignal(
          AppRealtimeConnectionStatus.disconnected,
        ),
      );
      _signals.add(AppRealtimeClosedSignal(intentional: _intentionalClose));
    }
  }

  @override
  Future<void> close() async {
    _intentionalClose = true;
    _generation += 1;
    _heartbeat?.cancel();
    _heartbeat = null;
    await _subscription?.cancel();
    _subscription = null;
    final channel = _channel;
    _channel = null;
    try {
      await channel?.close(ws_status.normalClosure);
    } catch (_) {
      // The transport may already have closed.
    }
    if (!_disposed) {
      _signals.add(
        const AppRealtimeConnectionSignal(
          AppRealtimeConnectionStatus.disconnected,
        ),
      );
      _signals.add(const AppRealtimeClosedSignal(intentional: true));
    }
  }

  @override
  Future<void> dispose() async {
    if (_disposed) return;
    await close();
    _disposed = true;
    await _signals.close();
  }
}
