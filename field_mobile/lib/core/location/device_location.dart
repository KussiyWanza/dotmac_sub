import 'package:flutter/foundation.dart'
    show TargetPlatform, defaultTargetPlatform;
import 'package:geolocator/geolocator.dart';

import 'location_source.dart';

/// What to do given the platform's permission state. Pure so it's testable
/// without the plugin.
enum LocationDecision { proceed, request, unavailable }

LocationDecision decideForPermission(
  LocationPermission permission, {
  required bool serviceEnabled,
  bool requireBackground = false,
}) {
  if (!serviceEnabled) return LocationDecision.unavailable;
  return switch (permission) {
    LocationPermission.always => LocationDecision.proceed,
    LocationPermission.whileInUse =>
      requireBackground ? LocationDecision.request : LocationDecision.proceed,
    LocationPermission.denied => LocationDecision.request,
    // Re-prompting after deniedForever just irritates: the OS won't show
    // the dialog again. The app keeps working without GPS.
    LocationPermission.deniedForever ||
    LocationPermission.unableToDetermine => LocationDecision.unavailable,
  };
}

/// Geolocator-backed LocationSource: permission-aware, never throws, and
/// prefers the last known position (instant) over a fresh fix (5s budget).
class GeolocatorLocationSource implements LocationSource {
  bool _permanentlyDenied = false;

  @override
  Future<GeoPoint?> current() async {
    if (_permanentlyDenied) return null;
    try {
      final serviceEnabled = await Geolocator.isLocationServiceEnabled();
      var permission = await Geolocator.checkPermission();
      var decision = decideForPermission(
        permission,
        serviceEnabled: serviceEnabled,
      );
      if (decision == LocationDecision.request) {
        permission = await Geolocator.requestPermission();
        decision = decideForPermission(
          permission,
          serviceEnabled: serviceEnabled,
        );
      }
      if (decision != LocationDecision.proceed) {
        if (permission == LocationPermission.deniedForever) {
          _permanentlyDenied = true;
        }
        return null;
      }

      final lastKnown = await Geolocator.getLastKnownPosition();
      if (lastKnown != null &&
          DateTime.now().difference(lastKnown.timestamp).inMinutes < 2) {
        return (latitude: lastKnown.latitude, longitude: lastKnown.longitude);
      }
      final position = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 5),
        ),
      );
      return (latitude: position.latitude, longitude: position.longitude);
    } catch (_) {
      // GPS failure must never break a transition or capture.
      return null;
    }
  }

  @override
  Stream<GeoPoint> positions() async* {
    if (_permanentlyDenied) return;
    final serviceEnabled = await Geolocator.isLocationServiceEnabled();
    var permission = await Geolocator.checkPermission();
    var decision = decideForPermission(
      permission,
      serviceEnabled: serviceEnabled,
      requireBackground: true,
    );
    if (decision == LocationDecision.request) {
      permission = await Geolocator.requestPermission();
      decision = decideForPermission(
        permission,
        serviceEnabled: serviceEnabled,
        requireBackground: true,
      );
    }
    if (decision != LocationDecision.proceed) {
      if (permission == LocationPermission.deniedForever) {
        _permanentlyDenied = true;
      }
      return;
    }
    yield* Geolocator.getPositionStream(
      locationSettings: _backgroundSettings(),
    ).map((p) => (latitude: p.latitude, longitude: p.longitude));
  }

  /// Platform settings that keep fixes flowing while backgrounded: an Android
  /// foreground service with an ongoing notification, and iOS background
  /// location updates (also requires UIBackgroundModes=location + the Always
  /// usage description in Info.plist).
  LocationSettings _backgroundSettings() {
    const accuracy = LocationAccuracy.high;
    const distanceFilter = 15; // metres; the OS coalesces the rest
    if (defaultTargetPlatform == TargetPlatform.android) {
      return AndroidSettings(
        accuracy: accuracy,
        distanceFilter: distanceFilter,
        intervalDuration: const Duration(seconds: 30),
        foregroundNotificationConfig: const ForegroundNotificationConfig(
          notificationTitle: 'DotMac Field',
          notificationText:
              'Sharing your location with dispatch while on shift.',
          enableWakeLock: true,
        ),
      );
    }
    if (defaultTargetPlatform == TargetPlatform.iOS) {
      return AppleSettings(
        accuracy: accuracy,
        distanceFilter: distanceFilter,
        allowBackgroundLocationUpdates: true,
        pauseLocationUpdatesAutomatically: false,
        showBackgroundLocationIndicator: true,
      );
    }
    return const LocationSettings(
      accuracy: accuracy,
      distanceFilter: distanceFilter,
    );
  }
}
