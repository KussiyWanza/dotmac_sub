import 'package:dotmac_field/core/location/device_location.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:geolocator/geolocator.dart';

void main() {
  test('permission decision logic', () {
    expect(
      decideForPermission(LocationPermission.whileInUse, serviceEnabled: true),
      LocationDecision.proceed,
    );
    expect(
      decideForPermission(LocationPermission.always, serviceEnabled: true),
      LocationDecision.proceed,
    );
    expect(
      decideForPermission(
        LocationPermission.whileInUse,
        serviceEnabled: true,
        requireBackground: true,
      ),
      LocationDecision.request,
    );
    expect(
      decideForPermission(
        LocationPermission.always,
        serviceEnabled: true,
        requireBackground: true,
      ),
      LocationDecision.proceed,
    );
    expect(
      decideForPermission(LocationPermission.denied, serviceEnabled: true),
      LocationDecision.request,
    );
    expect(
      decideForPermission(
        LocationPermission.deniedForever,
        serviceEnabled: true,
      ),
      LocationDecision.unavailable,
    );
    // Location services off trumps everything.
    expect(
      decideForPermission(LocationPermission.always, serviceEnabled: false),
      LocationDecision.unavailable,
    );
  });
}
