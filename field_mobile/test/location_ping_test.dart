import 'dart:io';

import 'package:dotmac_field/core/location/location_ping_store.dart';
import 'package:dotmac_field/core/location/location_source.dart';
import 'package:dotmac_field/core/secure/evidence_cipher.dart';
import 'package:dotmac_field/core/secure/store_work_gate.dart';
import 'package:dotmac_field/features/location/location_cadence.dart';
import 'package:dotmac_field/features/location/location_ping_service.dart';
import 'package:flutter_test/flutter_test.dart';

/// Builds a [LocationFix] for tests that don't care about accuracy/timestamp
/// beyond having *some* value — gives a stable default timestamp so
/// unrelated assertions (buffer counts, work-order tagging, etc.) don't
/// depend on wall-clock time.
LocationFix _fix({
  required double latitude,
  required double longitude,
  double? accuracy,
  DateTime? timestamp,
}) => (
  latitude: latitude,
  longitude: longitude,
  accuracy: accuracy,
  timestamp: timestamp ?? DateTime.utc(2026, 6, 13, 9, 0, 0),
);

void main() {
  // The queue on disk is an envelope, so every file store in these tests is
  // built with a real key and a real scope, exactly as the app builds it.
  const scopeKey = 'scope-under-test';
  EvidenceCipher cipher() => EvidenceCipher(EvidenceCipher.newKey());

  group('location batch response', () {
    test('accepts a fully accounted mixed response', () {
      expect(
        locationBatchWasResolved({
          'accepted': 1,
          'errors': [
            {'index': 1, 'code': 'technician_not_assigned'},
          ],
        }, 2),
        isTrue,
      );
    });

    test('retains the queue when response accounting is incomplete', () {
      expect(
        locationBatchWasResolved({'accepted': 1, 'errors': []}, 2),
        isFalse,
      );
      expect(
        locationBatchWasResolved({
          'accepted': 0,
          'errors': [
            {'index': 0, 'code': 'invalid_ping'},
            {'index': 0, 'code': 'invalid_ping'},
          ],
        }, 2),
        isFalse,
      );
    });
  });

  test('shift states use the backend presence status contract', () {
    expect(ShiftState.offShift.apiValue, 'off_shift');
    expect(ShiftState.onBreak.apiValue, 'break');
    expect(ShiftState.onShift.apiValue, 'on_shift');
    expect(ShiftStateApi.fromApiValue('off_shift'), ShiftState.offShift);
    expect(ShiftStateApi.fromApiValue('break'), ShiftState.onBreak);
    expect(ShiftStateApi.fromApiValue('on_shift'), ShiftState.onShift);
    expect(ShiftStateApi.fromApiValue('busy'), ShiftState.onShift);
    expect(ShiftStateApi.fromApiValue('unknown'), isNull);
  });

  group('pingInterval cadence', () {
    test('no pinging off shift or on break', () {
      expect(
        pingInterval(
          shift: ShiftState.offShift,
          hasActiveJob: true,
          moving: true,
        ),
        isNull,
      );
      expect(
        pingInterval(
          shift: ShiftState.onBreak,
          hasActiveJob: true,
          moving: true,
        ),
        isNull,
      );
    });

    test('tight cadence for active job or movement, relaxed when idle', () {
      expect(
        pingInterval(
          shift: ShiftState.onShift,
          hasActiveJob: true,
          moving: false,
        ),
        activePingInterval,
      );
      expect(
        pingInterval(
          shift: ShiftState.onShift,
          hasActiveJob: false,
          moving: true,
        ),
        activePingInterval,
      );
      expect(
        pingInterval(
          shift: ShiftState.onShift,
          hasActiveJob: false,
          moving: false,
        ),
        idlePingInterval,
      );
    });
  });

  group('LocationPingService capture', () {
    test('does not capture off shift', () async {
      final svc = LocationPingService(
        location: FakeLocation(_fix(latitude: 6.5, longitude: 3.3)),
        poster: (_) async => true,
      );
      await svc.captureOnce();
      expect(svc.bufferedCount, 0);
    });

    test('captures a fix on shift with status and work order', () async {
      List<LocationPingPayload>? posted;
      final svc = LocationPingService(
        location: FakeLocation(_fix(latitude: 6.5, longitude: 3.3)),
        poster: (pings) async {
          posted = pings;
          return true;
        },
        clock: () => DateTime.utc(2026, 6, 13, 9, 0, 0),
      )..setShift(ShiftState.onShift);

      await svc.captureOnce(hasActiveJob: true, workOrderId: 'wo-1');
      expect(svc.bufferedCount, 1);
      await svc.flush();
      expect(posted, hasLength(1));
      final payload = posted!.single.toJson();
      expect(payload['crm_work_order_id'], 'wo-1');
      expect(payload, isNot(contains('work_order_id')));
    });

    test('a null fix is skipped without error', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
      )..setShift(ShiftState.onShift);
      await svc.captureOnce();
      expect(svc.bufferedCount, 0);
    });

    test('buffer is bounded, dropping oldest', () async {
      final svc = LocationPingService(
        location: FakeLocation(_fix(latitude: 1, longitude: 1)),
        poster: (_) async => true,
        maxBuffer: 3,
      )..setShift(ShiftState.onShift);
      for (var i = 0; i < 5; i++) {
        await svc.captureOnce();
      }
      expect(svc.bufferedCount, 3);
    });

    test(
      "captured_at is the fix's own timestamp, not \"now\" at capture time",
      () async {
        // A stale-but-still-fresh cached fix: taken 90s before capture.
        final fixTime = DateTime.utc(2026, 6, 13, 8, 58, 30);
        List<LocationPingPayload>? posted;
        final svc = LocationPingService(
          location: FakeLocation(
            _fix(latitude: 6.5, longitude: 3.3, timestamp: fixTime),
          ),
          poster: (pings) async {
            posted = pings;
            return true;
          },
          // Deliberately far from fixTime: if capturedAt ever silently fell
          // back to the app clock instead of the fix's own timestamp, this
          // assertion catches it immediately rather than passing by
          // coincidence.
          clock: () => DateTime.utc(2030, 1, 1),
        )..setShift(ShiftState.onShift);

        await svc.captureOnce();
        await svc.flush();
        expect(posted, hasLength(1));
        expect(posted!.single.capturedAt, fixTime);
        expect(posted!.single.capturedAtIsClockDerived, isFalse);
      },
    );

    test(
      'accuracy survives from the fix through to the wire payload',
      () async {
        List<LocationPingPayload>? posted;
        final svc = LocationPingService(
          location: FakeLocation(
            _fix(latitude: 6.5, longitude: 3.3, accuracy: 12.5),
          ),
          poster: (pings) async {
            posted = pings;
            return true;
          },
        )..setShift(ShiftState.onShift);

        await svc.captureOnce();
        await svc.flush();
        expect(posted!.single.accuracyM, 12.5);
        expect(posted!.single.toJson()['accuracy_m'], 12.5);
      },
    );

    test(
      'a fix with no accuracy omits accuracy_m from the wire payload',
      () async {
        List<LocationPingPayload>? posted;
        final svc = LocationPingService(
          location: FakeLocation(_fix(latitude: 6.5, longitude: 3.3)),
          poster: (pings) async {
            posted = pings;
            return true;
          },
        )..setShift(ShiftState.onShift);

        await svc.captureOnce();
        await svc.flush();
        expect(posted!.single.toJson(), isNot(contains('accuracy_m')));
      },
    );

    test(
      'a fix with no timestamp falls back to the clock and is flagged clock-derived',
      () async {
        // A source that genuinely has no capture time to offer (see
        // LocationFix's doc comment) — the fallback path must still produce
        // a valid, sendable ping, distinguishably marked as clock-derived.
        final LocationFix noTimestampFix = (
          latitude: 6.5,
          longitude: 3.3,
          accuracy: 8.0,
          timestamp: null,
        );
        final fallbackClock = DateTime.utc(2026, 6, 13, 9, 0, 0);
        List<LocationPingPayload>? posted;
        final svc = LocationPingService(
          location: FakeLocation(noTimestampFix),
          poster: (pings) async {
            posted = pings;
            return true;
          },
          clock: () => fallbackClock,
        )..setShift(ShiftState.onShift);

        await svc.captureOnce();
        expect(svc.bufferedCount, 1);
        await svc.flush();
        expect(posted, hasLength(1));
        final payload = posted!.single;
        expect(payload.capturedAt, fallbackClock);
        expect(payload.capturedAtIsClockDerived, isTrue);
        // Local diagnostic only — never sent to the server.
        expect(payload.toJson(), isNot(contains('capturedAtIsClockDerived')));
      },
    );
  });

  group('LocationPingService sharing', () {
    test('restores the server-owned sharing state', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
        sharingReader: () async => const LocationSharingSnapshot(
          enabled: true,
          shift: ShiftState.onBreak,
        ),
      );

      expect(await svc.restoreShift(), ShiftState.onBreak);
      expect(svc.shift, ShiftState.onBreak);
    });

    test('disabled server sharing restores off shift', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
        sharingReader: () async => const LocationSharingSnapshot(
          enabled: false,
          shift: ShiftState.onShift,
        ),
      );

      expect(await svc.restoreShift(), ShiftState.offShift);
      expect(svc.shift, ShiftState.offShift);
    });

    test('failed restore leaves the local state unchanged', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
        sharingReader: () async => null,
      )..setShift(ShiftState.onBreak);

      expect(await svc.restoreShift(), isNull);
      expect(svc.shift, ShiftState.onBreak);
    });

    test('updateShift calls sharing updater and updates local shift', () async {
      final calls = <({bool enabled, ShiftState shift})>[];
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
        sharingUpdater: ({required enabled, required shift}) async {
          calls.add((enabled: enabled, shift: shift));
          return true;
        },
      );

      expect(await svc.updateShift(ShiftState.onShift), isTrue);
      expect(svc.shift, ShiftState.onShift);
      expect(calls.single.enabled, isTrue);
      expect(calls.single.shift, ShiftState.onShift);
    });

    test('updateShift keeps prior shift on sharing failure', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
        sharingUpdater: ({required enabled, required shift}) async => false,
      )..setShift(ShiftState.offShift);

      expect(await svc.updateShift(ShiftState.onShift), isFalse);
      expect(svc.shift, ShiftState.offShift);
    });
  });

  group('LocationPingService flush', () {
    test('clears the buffer on success', () async {
      var posted = 0;
      final svc = LocationPingService(
        location: FakeLocation(_fix(latitude: 6.5, longitude: 3.3)),
        poster: (pings) async {
          posted = pings.length;
          return true;
        },
      )..setShift(ShiftState.onShift);
      await svc.captureOnce();
      await svc.captureOnce();
      expect(await svc.flush(), isTrue);
      expect(posted, 2);
      expect(svc.bufferedCount, 0);
    });

    test('retains the buffer on failure', () async {
      final svc = LocationPingService(
        location: FakeLocation(_fix(latitude: 6.5, longitude: 3.3)),
        poster: (_) async => false,
      )..setShift(ShiftState.onShift);
      await svc.captureOnce();
      expect(await svc.flush(), isFalse);
      expect(svc.bufferedCount, 1);
    });

    test('flush with empty buffer is a no-op success', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => false,
      );
      expect(await svc.flush(), isTrue);
    });
  });

  group('durable location buffer', () {
    test('corrupt queue is discarded with payload-free evidence', () async {
      final directory = await Directory.systemTemp.createTemp(
        'field-location-corrupt',
      );
      final file = File('${directory.path}/pending.json');
      await file.writeAsString('{private malformed payload');
      final store = FileLocationPingStore(
        file,
        cipher: cipher(),
        scopeKey: scopeKey,
        work: StoreWorkGate(),
        clock: () => DateTime.utc(2026, 8, 18, 12),
      );
      addTearDown(() async {
        if (await directory.exists()) await directory.delete(recursive: true);
      });

      expect(await store.load(), isEmpty);
      expect(await file.exists(), isFalse);
      final marker = File('${file.path}.corrupt');
      expect(await marker.exists(), isTrue);
      expect(
        await marker.readAsString(),
        'discarded_at=2026-08-18T12:00:00.000Z\n',
      );
    });

    test(
      'file store survives service recreation and clears after sync',
      () async {
        final directory = await Directory.systemTemp.createTemp(
          'field-location-pings',
        );
        final file = File('${directory.path}/pending.json');
        final store = FileLocationPingStore(
          file,
          cipher: cipher(),
          scopeKey: scopeKey,
          work: StoreWorkGate(),
        );
        addTearDown(() async {
          if (await directory.exists()) await directory.delete(recursive: true);
        });

        final first = LocationPingService(
          location: FakeLocation(
            _fix(latitude: 6.5, longitude: 3.3, accuracy: 9.5),
          ),
          poster: (_) async => false,
          store: store,
        )..setShift(ShiftState.onShift);
        await first.captureOnce(workOrderId: 'wo-1');
        expect(await file.exists(), isTrue);

        List<LocationPingPayload>? restored;
        final restarted = LocationPingService(
          location: FakeLocation(null),
          poster: (pings) async {
            restored = pings;
            return true;
          },
          store: store,
        );

        expect(await restarted.restoreBufferedPings(), 1);
        expect(await restarted.flush(), isTrue);
        expect(restored!.single.workOrderId, 'wo-1');
        // Accuracy must round-trip through the encrypted on-disk queue, not
        // just serialize once for immediate send.
        expect(restored!.single.accuracyM, 9.5);
        expect(restarted.bufferedCount, 0);
        expect(await file.exists(), isFalse);
      },
    );

    test(
      'LocationPingPayload.accuracyM round-trips through toJson/fromJson',
      () {
        final payload = LocationPingPayload(
          latitude: 6.5,
          longitude: 3.3,
          capturedAt: DateTime.utc(2026, 6, 13, 9),
          shift: ShiftState.onShift,
          accuracyM: 4.2,
        );
        final roundTripped = LocationPingPayload.fromJson(payload.toJson());
        expect(roundTripped.accuracyM, 4.2);
      },
    );

    test(
      'LocationPingPayload.clientObservationId round-trips through toJson/fromJson '
      'under the exact server wire key',
      () {
        final payload = LocationPingPayload(
          latitude: 6.5,
          longitude: 3.3,
          capturedAt: DateTime.utc(2026, 6, 13, 9),
          shift: ShiftState.onShift,
          clientObservationId: 'a1b2c3d4-0000-0000-0000-000000000000',
        );
        final json = payload.toJson();
        expect(
          json['client_observation_id'],
          'a1b2c3d4-0000-0000-0000-000000000000',
        );
        final roundTripped = LocationPingPayload.fromJson(json);
        expect(
          roundTripped.clientObservationId,
          'a1b2c3d4-0000-0000-0000-000000000000',
        );
      },
    );

    test(
      'a ping with no client_observation_id (queued before the field existed) '
      'decodes with null rather than failing',
      () {
        final payload = LocationPingPayload(
          latitude: 6.5,
          longitude: 3.3,
          capturedAt: DateTime.utc(2026, 6, 13, 9),
          shift: ShiftState.onShift,
        );
        expect(payload.toJson(), isNot(contains('client_observation_id')));
        final roundTripped = LocationPingPayload.fromJson(payload.toJson());
        expect(roundTripped.clientObservationId, isNull);
      },
    );

    test('client_observation_id is minted once at capture and stays identical '
        'across a failed retry and a second flush attempt', () async {
      final postedIds = <String?>[];
      var succeed = false;
      final svc = LocationPingService(
        location: FakeLocation(_fix(latitude: 6.5, longitude: 3.3)),
        poster: (pings) async {
          postedIds.add(pings.single.clientObservationId);
          return succeed;
        },
        store: MemoryLocationPingStore(),
      )..setShift(ShiftState.onShift);

      await svc.captureOnce();

      // First flush fails (ambiguous network failure); the buffered ping
      // is retained rather than regenerated.
      expect(await svc.flush(), isFalse);
      // Second flush of the SAME buffered ping — the id sent must be
      // identical, not a freshly minted one.
      succeed = true;
      expect(await svc.flush(), isTrue);

      expect(postedIds, hasLength(2));
      expect(postedIds[0], isNotNull);
      expect(postedIds[0], isNotEmpty);
      expect(postedIds[1], postedIds[0]);
    });

    test("client_observation_id survives a save-to-queue/load-from-queue round "
        "trip through the encrypted on-disk store — the same id a simulated "
        "app restart would see", () async {
      final directory = await Directory.systemTemp.createTemp(
        'field-location-dedup',
      );
      final file = File('${directory.path}/pending.json');
      final store = FileLocationPingStore(
        file,
        cipher: cipher(),
        scopeKey: scopeKey,
        work: StoreWorkGate(),
      );
      addTearDown(() async {
        if (await directory.exists()) await directory.delete(recursive: true);
      });

      final first = LocationPingService(
        location: FakeLocation(_fix(latitude: 6.5, longitude: 3.3)),
        poster: (_) async => false, // keep it queued on disk
        store: store,
      )..setShift(ShiftState.onShift);
      await first.captureOnce();
      final mintedId = (await store.load()).single.clientObservationId;
      expect(mintedId, isNotNull);
      expect(mintedId, isNotEmpty);

      // A fresh service instance reading the same encrypted queue (an app
      // restart) and then two separate flush attempts of that same
      // restored ping must all see the identical id.
      final postedIds = <String?>[];
      var succeed = false;
      final restarted = LocationPingService(
        location: FakeLocation(null),
        poster: (pings) async {
          postedIds.add(pings.single.clientObservationId);
          return succeed;
        },
        store: store,
      );
      expect(await restarted.restoreBufferedPings(), 1);
      expect(await restarted.flush(), isFalse);
      succeed = true;
      expect(await restarted.flush(), isTrue);

      expect(postedIds, [mintedId, mintedId]);
    });

    test('restored buffer retains only the newest configured fixes', () async {
      final store = MemoryLocationPingStore();
      await store.save([
        for (var i = 0; i < 4; i++)
          LocationPingPayload(
            latitude: i.toDouble(),
            longitude: i.toDouble(),
            capturedAt: DateTime.utc(2026, 1, 1, 0, i),
            shift: ShiftState.onShift,
          ),
      ]);
      final service = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => false,
        store: store,
        maxBuffer: 2,
      );

      expect(await service.restoreBufferedPings(), 2);
      expect(await store.load(), hasLength(2));
    });
  });

  group('background tracking', () {
    test('streamed fixes are buffered and flushed while on shift', () async {
      final fake = FakeLocation(_fix(latitude: 6.5, longitude: 3.3));
      var posted = 0;
      final svc = LocationPingService(
        location: fake,
        poster: (pings) async {
          posted += pings.length;
          return true;
        },
      )..setShift(ShiftState.onShift);

      svc.startBackgroundTracking(workOrderId: 'wo-1');
      expect(svc.isBackgroundTracking, isTrue);
      fake.emit(_fix(latitude: 6.51, longitude: 3.31));
      await Future<void>.delayed(const Duration(milliseconds: 20));

      expect(posted, 1);
      expect(svc.bufferedCount, 0); // flushed on success
      await svc.stopBackgroundTracking();
      expect(svc.isBackgroundTracking, isFalse);
    });

    test('streamed fixes are ignored off shift', () async {
      final fake = FakeLocation(null);
      final svc = LocationPingService(
        location: fake,
        poster: (_) async => true,
      );
      svc.startBackgroundTracking();
      fake.emit(_fix(latitude: 1, longitude: 1));
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(svc.bufferedCount, 0);
      await svc.stopBackgroundTracking();
    });

    test('startBackgroundTracking is idempotent', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
      );
      svc.startBackgroundTracking();
      svc.startBackgroundTracking();
      expect(svc.isBackgroundTracking, isTrue);
      await svc.stopBackgroundTracking();
    });
  });
}
