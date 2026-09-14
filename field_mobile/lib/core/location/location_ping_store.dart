import 'dart:convert';
import 'dart:io';

import '../../features/location/location_cadence.dart';
import '../secure/evidence_cipher.dart';
import '../secure/evidence_files.dart';
import '../secure/store_work_gate.dart';

class LocationPingPayload {
  const LocationPingPayload({
    required this.latitude,
    required this.longitude,
    required this.capturedAt,
    required this.shift,
    this.workOrderId,
    this.accuracyM,
    this.capturedAtIsClockDerived = false,
    this.clientObservationId,
  });

  final double latitude;
  final double longitude;
  final DateTime capturedAt;
  final ShiftState shift;
  final String? workOrderId;

  /// Horizontal accuracy of the fix, in metres — matches the server's
  /// `accuracy_m` (see `LocationPingInput` in `app/schemas/field.py`). Null
  /// when the source fix carried none.
  final double? accuracyM;

  /// True when [capturedAt] came from the app clock at buffer-append time
  /// rather than the GPS fix's own timestamp — the explicit fallback path
  /// for a fix whose source genuinely had no capture time. Local-only
  /// diagnostic; deliberately never sent to the server (see [toJson]), so it
  /// does not persist across an app restart via the encrypted queue either.
  final bool capturedAtIsClockDerived;

  /// A UUID minted once, at capture time, identifying this specific fix
  /// attempt — matches the server's `client_observation_id` on
  /// `LocationPingInput` (`app/schemas/field.py`) exactly, wire key and
  /// nullability both: a ping already queued before this field existed
  /// decodes with null, which the server treats as "dedup doesn't run for
  /// this ping" rather than an error. Persisted through the encrypted queue
  /// so a retry after an ambiguous network failure replays the same id
  /// instead of minting a new one and risking a duplicate.
  final String? clientObservationId;

  Map<String, dynamic> toJson() => {
    'latitude': latitude,
    'longitude': longitude,
    'accuracy_m': ?accuracyM,
    'captured_at': capturedAt.toUtc().toIso8601String(),
    'status': shift.apiValue,
    'crm_work_order_id': ?workOrderId,
    'client_observation_id': ?clientObservationId,
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
      accuracyM: (json['accuracy_m'] as num?)?.toDouble(),
      clientObservationId: json['client_observation_id']?.toString(),
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

/// The queue of pings the device has recorded but not yet delivered. Each ping
/// is a customer premises the technician stood at, so the file on disk is an
/// AES-GCM envelope bound to the scope, never readable JSON. There is no
/// plaintext fallback: [cipher] is required, because a queue we cannot encrypt
/// is a queue we must not write.
class FileLocationPingStore implements LocationPingStore {
  FileLocationPingStore(
    this.file, {
    required this.cipher,
    required this.scopeKey,
    required this.work,
    DateTime Function()? clock,
  }) : _clock = clock ?? (() => DateTime.now().toUtc());

  final File file;
  final EvidenceCipher cipher;
  final String scopeKey;
  final StoreWorkGate work;
  final DateTime Function() _clock;

  String get _context => evidenceContext(scopeKey, 'location', 'queue');

  @override
  Future<List<LocationPingPayload>> load() => work.run(() async {
    if (!await file.exists()) return const [];
    try {
      final envelope = await file.readAsBytes();
      final decoded = jsonDecode(
        utf8.decode(cipher.open(envelope, context: _context)),
      );
      if (decoded is! List) {
        throw const FormatException('Persisted location queue is not a list');
      }
      return [
        for (final item in decoded)
          if (item is Map)
            LocationPingPayload.fromJson(item.cast<String, dynamic>()),
      ];
    } on Object {
      // Includes an envelope belonging to a key we no longer hold: unreadable
      // is treated exactly like corrupt, and neither is ever surfaced.
      await _quarantineCorruptQueue();
      return const [];
    }
  });

  Future<void> _quarantineCorruptQueue() async {
    final marker = File('${file.path}.corrupt');
    await marker.writeAsString(
      'discarded_at=${_clock().toIso8601String()}\n',
      flush: true,
    );
    if (await file.exists()) await file.delete();
  }

  @override
  Future<void> save(List<LocationPingPayload> pings) => work.run(() async {
    await file.parent.create(recursive: true);
    if (pings.isEmpty) {
      if (await file.exists()) await file.delete();
      return;
    }
    final temporary = File('${file.path}.tmp');
    await temporary.writeAsBytes(
      cipher.seal(
        utf8.encode(jsonEncode(pings.map((ping) => ping.toJson()).toList())),
        context: _context,
      ),
      flush: true,
    );
    if (await file.exists()) await file.delete();
    await temporary.rename(file.path);
  });
}
