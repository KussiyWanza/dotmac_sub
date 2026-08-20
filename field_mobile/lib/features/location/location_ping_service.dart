import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/location/location_ping_store.dart';
import '../../core/location/location_source.dart';
import '../auth/auth_state.dart';
import '../execution/execution_controller.dart' show locationSourceProvider;
import 'location_cadence.dart';

/// Posts a batch of location pings; returns true on success. Injected so the
/// service is testable without dio, and so the production wiring can route
/// through the offline-tolerant API client.
typedef PingPoster = Future<bool> Function(List<LocationPingPayload> pings);
typedef SharingUpdater =
    Future<bool> Function({required bool enabled, required ShiftState shift});
typedef SharingReader = Future<LocationSharingSnapshot?> Function();

bool locationBatchWasResolved(Object? responseData, int sentCount) {
  if (responseData is! Map || sentCount < 0) return false;
  final accepted = responseData['accepted'];
  final errors = responseData['errors'];
  if (accepted is! int || accepted < 0 || errors is! List) return false;
  final rejectedIndexes = <int>{};
  for (final error in errors) {
    if (error is! Map) return false;
    final index = error['index'];
    final code = error['code'];
    if (index is! int || index < 0 || index >= sentCount) return false;
    if (code is! String || code.isEmpty || !rejectedIndexes.add(index)) {
      return false;
    }
  }
  return accepted + rejectedIndexes.length == sentCount;
}

class LocationSharingSnapshot {
  const LocationSharingSnapshot({required this.enabled, required this.shift});

  final bool enabled;
  final ShiftState shift;
}

/// Captures GPS fixes on a shift-scoped adaptive cadence and flushes them to
/// the backend in batches. Buffers across failures so a dropped network never
/// loses a fix; the buffer is bounded so a long offline stretch can't grow
/// without limit (oldest fixes are dropped first — recent location matters most).
class LocationPingService {
  LocationPingService({
    required this.location,
    required this.poster,
    this.sharingUpdater,
    this.sharingReader,
    LocationPingStore? store,
    DateTime Function()? clock,
    this.maxBuffer = 200,
  }) : store = store ?? MemoryLocationPingStore(),
       _clock = clock ?? (() => DateTime.now().toUtc());

  final LocationSource location;
  final PingPoster poster;
  final SharingUpdater? sharingUpdater;
  final SharingReader? sharingReader;
  final LocationPingStore store;
  final DateTime Function() _clock;
  final int maxBuffer;

  final List<LocationPingPayload> _buffer = [];
  bool _bufferRestored = false;
  ShiftState _shift = ShiftState.offShift;
  StreamSubscription<GeoPoint>? _backgroundSub;
  String? _activeWorkOrderId;

  ShiftState get shift => _shift;
  int get bufferedCount => _buffer.length;
  bool get isBackgroundTracking => _backgroundSub != null;

  /// Switching off shift / on break stops capture; going on break keeps a final
  /// status so the map can show the tech paused rather than vanish instantly.
  void setShift(ShiftState shift) => _shift = shift;

  Future<int> restoreBufferedPings() async {
    if (_bufferRestored) return _buffer.length;
    final restored = await store.load();
    _buffer
      ..clear()
      ..addAll(restored.takeLast(maxBuffer));
    _bufferRestored = true;
    if (restored.length != _buffer.length) await store.save(_buffer);
    return _buffer.length;
  }

  Future<ShiftState?> restoreShift() async {
    final reader = sharingReader;
    if (reader == null) return null;
    final snapshot = await reader();
    if (snapshot == null) return null;
    final restored = snapshot.enabled ? snapshot.shift : ShiftState.offShift;
    setShift(restored);
    return restored;
  }

  Future<bool> updateShift(ShiftState shift) async {
    final updater = sharingUpdater;
    if (updater != null) {
      final ok = await updater(
        enabled: shift != ShiftState.offShift,
        shift: shift,
      );
      if (!ok) return false;
    }
    setShift(shift);
    return true;
  }

  /// Capture a single fix into the buffer. No-op off shift, on break, or when
  /// the device gives no fix — GPS trouble never throws.
  Future<void> captureOnce({
    bool hasActiveJob = false,
    String? workOrderId,
  }) async {
    if (_shift != ShiftState.onShift) return;
    await restoreBufferedPings();
    final point = await location.current();
    if (point == null) return;
    await _appendFix(point, workOrderId: workOrderId);
  }

  Future<void> _appendFix(GeoPoint point, {String? workOrderId}) async {
    _buffer.add(
      LocationPingPayload(
        latitude: point.latitude,
        longitude: point.longitude,
        capturedAt: _clock(),
        shift: _shift,
        workOrderId: workOrderId,
      ),
    );
    if (_buffer.length > maxBuffer) {
      _buffer.removeRange(0, _buffer.length - maxBuffer);
    }
    await store.save(_buffer);
  }

  /// The work order pings should be tagged with while background tracking runs.
  void setActiveWorkOrder(String? workOrderId) =>
      _activeWorkOrderId = workOrderId;

  /// Subscribe to the platform's background-capable position stream so fixes
  /// keep arriving while the app is backgrounded or the screen is locked when
  /// the OS has granted background access. Each fix is buffered and flushed.
  /// Idempotent; off-shift fixes are ignored.
  void startBackgroundTracking({String? workOrderId}) {
    _activeWorkOrderId = workOrderId;
    if (_backgroundSub != null) return;
    _backgroundSub = location.positions().listen((point) async {
      if (_shift != ShiftState.onShift) return;
      await restoreBufferedPings();
      await _appendFix(point, workOrderId: _activeWorkOrderId);
      await flush();
    });
  }

  Future<void> stopBackgroundTracking() async {
    await _backgroundSub?.cancel();
    _backgroundSub = null;
  }

  /// Release the background subscription. Call when the host widget disposes.
  Future<void> dispose() => stopBackgroundTracking();

  /// Flush the buffer as one batch. On failure the buffer is retained for the
  /// next attempt; only the fixes actually sent are removed on success.
  Future<bool> flush() async {
    await restoreBufferedPings();
    if (_buffer.isEmpty) return true;
    final batch = List<LocationPingPayload>.of(_buffer);
    final ok = await poster(batch);
    if (ok) {
      _buffer.removeRange(0, batch.length);
      await store.save(_buffer);
    }
    return ok;
  }

  /// The delay until the next capture given the current state, or null to pause.
  Duration? nextInterval({bool hasActiveJob = false, bool moving = false}) =>
      pingInterval(shift: _shift, hasActiveJob: hasActiveJob, moving: moving);
}

/// Production wiring: posts to /field/locations through the auth-aware client.
final locationPingServiceProvider = Provider<LocationPingService>((ref) {
  final api = ref.watch(apiClientProvider);
  final location = ref.watch(locationSourceProvider);
  return LocationPingService(
    location: location,
    store: ref.watch(locationPingStoreProvider),
    sharingReader: () async {
      try {
        final response = await api.dio.get('/api/v1/field/locations/me');
        final data = response.data;
        if (data is! Map) return null;
        final enabled = data['location_sharing_enabled'] == true;
        final rawStatus = data['status']?.toString();
        final shift = rawStatus == null
            ? null
            : ShiftStateApi.fromApiValue(rawStatus);
        if (shift == null) return null;
        return LocationSharingSnapshot(enabled: enabled, shift: shift);
      } catch (_) {
        return null;
      }
    },
    sharingUpdater: ({required enabled, required shift}) async {
      try {
        await api.dio.put(
          '/api/v1/field/locations/sharing',
          data: {'enabled': enabled, 'status': shift.apiValue},
        );
        return true;
      } catch (_) {
        return false;
      }
    },
    poster: (pings) async {
      try {
        final response = await api.dio.post(
          '/api/v1/field/locations',
          data: {'pings': pings.map((ping) => ping.toJson()).toList()},
        );
        return locationBatchWasResolved(response.data, pings.length);
      } catch (_) {
        return false; // keep the buffer; retried on the next tick
      }
    },
  );
});

final locationPingStoreProvider = Provider<LocationPingStore>(
  (ref) => MemoryLocationPingStore(),
);

extension<T> on List<T> {
  Iterable<T> takeLast(int count) => skip(length > count ? length - count : 0);
}
