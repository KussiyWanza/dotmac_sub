import 'dart:async';

/// Bare coordinate pair. Kept intentionally narrow for consumers that only
/// ever need lat/lng and have no use for accuracy or capture time — e.g. the
/// vendor as-built [TraceRecorder], which does jitter-filtering and haversine
/// distance over a walked route and would otherwise be forced to fabricate
/// values it doesn't have and never sends anywhere. Do not widen this type;
/// add fields to [LocationFix] instead (see that type's doc comment).
typedef GeoPoint = ({double latitude, double longitude});

/// A GPS fix carrying the device's own accuracy (metres) and capture
/// timestamp alongside the coordinates. This is the type the
/// location-tracking feature (shift-scoped background pings, staleness
/// checks) depends on: accuracy and the fix's own capture time cannot survive
/// a reduction to [GeoPoint], and both are needed downstream (server-side
/// `accuracy_m`, and freshness gating that must key off when the fix was
/// actually taken rather than when the app happened to buffer it).
///
/// [timestamp] is non-null for every fix `geolocator`'s `Position` produces
/// — the only case a [LocationSource] would ever construct one with a null
/// timestamp is a source that genuinely has no capture time to offer (there
/// is no such source today, but the type stays honest about that
/// possibility rather than forcing every implementation to fabricate a
/// value). Callers that persist [timestamp] downstream must treat a null
/// value as an explicit, visible fallback case — never silently substitute
/// the wall clock without marking that they did.
typedef LocationFix = ({
  double latitude,
  double longitude,
  double? accuracy,
  DateTime? timestamp,
});

abstract class LocationSource {
  /// Best-effort current position; null when unavailable/denied.
  Future<LocationFix?> current();

  /// Continuous fixes for background-capable tracking. The device source backs
  /// this with a platform location stream (Android foreground service / iOS
  /// background updates) when the user has granted background access.
  /// Sources that can't stream return an empty stream.
  Stream<LocationFix> positions() => Stream<LocationFix>.empty();
}

class UnavailableLocation implements LocationSource {
  const UnavailableLocation();

  @override
  Future<LocationFix?> current() async => null;

  @override
  Stream<LocationFix> positions() => Stream<LocationFix>.empty();
}

class FakeLocation implements LocationSource {
  FakeLocation(this.point);

  LocationFix? point;
  final StreamController<LocationFix> _positions =
      StreamController<LocationFix>.broadcast();

  /// Push a fix to subscribers of [positions]; lets tests drive background flow.
  void emit(LocationFix fix) => _positions.add(fix);

  @override
  Future<LocationFix?> current() async => point;

  @override
  Stream<LocationFix> positions() => _positions.stream;
}
