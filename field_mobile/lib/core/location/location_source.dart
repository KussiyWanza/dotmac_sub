import 'dart:async';

/// GPS abstraction. The device implementation lands with the geolocator
/// plugin at device-testing time; everything upstream depends only on this
/// interface so transitions and capture flows are testable headless.
typedef GeoPoint = ({double latitude, double longitude});

abstract class LocationSource {
  /// Best-effort current position; null when unavailable/denied.
  Future<GeoPoint?> current();

  /// Continuous fixes for background-capable tracking. The device source backs
  /// this with a platform location stream (Android foreground service / iOS
  /// background updates) when the user has granted background access.
  /// Sources that can't stream return an empty stream.
  Stream<GeoPoint> positions() => Stream<GeoPoint>.empty();
}

class UnavailableLocation implements LocationSource {
  const UnavailableLocation();

  @override
  Future<GeoPoint?> current() async => null;

  @override
  Stream<GeoPoint> positions() => Stream<GeoPoint>.empty();
}

class FakeLocation implements LocationSource {
  FakeLocation(this.point);

  GeoPoint? point;
  final StreamController<GeoPoint> _positions =
      StreamController<GeoPoint>.broadcast();

  /// Push a fix to subscribers of [positions]; lets tests drive background flow.
  void emit(GeoPoint fix) => _positions.add(fix);

  @override
  Future<GeoPoint?> current() async => point;

  @override
  Stream<GeoPoint> positions() => _positions.stream;
}
