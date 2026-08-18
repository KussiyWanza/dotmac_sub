import 'dart:convert';
import 'dart:io';

import '../../features/location/location_cadence.dart';

class LocationPingPayload {
  const LocationPingPayload({
    required this.latitude,
    required this.longitude,
    required this.capturedAt,
    required this.shift,
    this.workOrderId,
  });

  final double latitude;
  final double longitude;
  final DateTime capturedAt;
  final ShiftState shift;
  final String? workOrderId;

  Map<String, dynamic> toJson() => {
    'latitude': latitude,
    'longitude': longitude,
    'captured_at': capturedAt.toUtc().toIso8601String(),
    'status': shift.apiValue,
    'crm_work_order_id': ?workOrderId,
  };

  factory LocationPingPayload.fromJson(Map<String, dynamic> json) {
    final latitude = (json['latitude'] as num?)?.toDouble();
    final longitude = (json['longitude'] as num?)?.toDouble();
    final capturedAt = DateTime.tryParse(json['captured_at']?.toString() ?? '');
    final shift = ShiftStateApi.fromApiValue(json['status']?.toString() ?? '');
    if (latitude == null ||
        longitude == null ||
        capturedAt == null ||
        shift == null) {
      throw const FormatException('Invalid persisted location ping');
    }
    return LocationPingPayload(
      latitude: latitude,
      longitude: longitude,
      capturedAt: capturedAt.toUtc(),
      shift: shift,
      workOrderId: json['crm_work_order_id']?.toString(),
    );
  }
}

abstract class LocationPingStore {
  Future<List<LocationPingPayload>> load();

  Future<void> save(List<LocationPingPayload> pings);
}

class MemoryLocationPingStore implements LocationPingStore {
  List<LocationPingPayload> _pings = const [];

  @override
  Future<List<LocationPingPayload>> load() async => List.of(_pings);

  @override
  Future<void> save(List<LocationPingPayload> pings) async {
    _pings = List.of(pings);
  }
}

class FileLocationPingStore implements LocationPingStore {
  FileLocationPingStore(this.file, {DateTime Function()? clock})
    : _clock = clock ?? (() => DateTime.now().toUtc());

  final File file;
  final DateTime Function() _clock;

  @override
  Future<List<LocationPingPayload>> load() async {
    if (!await file.exists()) return const [];
    try {
      final decoded = jsonDecode(await file.readAsString());
      if (decoded is! List) {
        throw const FormatException('Persisted location queue is not a list');
      }
      return [
        for (final item in decoded)
          if (item is Map)
            LocationPingPayload.fromJson(item.cast<String, dynamic>()),
      ];
    } on Object {
      await _quarantineCorruptQueue();
      return const [];
    }
  }

  Future<void> _quarantineCorruptQueue() async {
    final marker = File('${file.path}.corrupt');
    await marker.writeAsString(
      'discarded_at=${_clock().toIso8601String()}\n',
      flush: true,
    );
    if (await file.exists()) await file.delete();
  }

  @override
  Future<void> save(List<LocationPingPayload> pings) async {
    await file.parent.create(recursive: true);
    if (pings.isEmpty) {
      if (await file.exists()) await file.delete();
      return;
    }
    final temporary = File('${file.path}.tmp');
    await temporary.writeAsString(
      jsonEncode(pings.map((ping) => ping.toJson()).toList()),
      flush: true,
    );
    if (await file.exists()) await file.delete();
    await temporary.rename(file.path);
  }
}
