import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../jobs/jobs_providers.dart';
import 'location_cadence.dart';
import 'location_ping_service.dart';

final fieldShiftProvider = StateProvider<ShiftState>(
  (ref) => ShiftState.offShift,
);

String? _activeWorkOrderId(JobList? list) {
  if (list == null) return null;
  for (final job in list.jobs) {
    if (job.status == 'in_progress' ||
        job.status == 'paused' ||
        job.status == 'dispatched') {
      return job.id;
    }
  }
  return null;
}

class LocationTrackingHost extends ConsumerStatefulWidget {
  const LocationTrackingHost({super.key, required this.child});

  final Widget child;

  @override
  ConsumerState<LocationTrackingHost> createState() =>
      _LocationTrackingHostState();
}

class _LocationTrackingHostState extends ConsumerState<LocationTrackingHost>
    with WidgetsBindingObserver {
  Timer? _timer;
  bool _foreground = true;
  ShiftState? _lastShift;
  String? _lastWorkOrderId;
  // Captured during build so dispose can stop tracking without touching `ref`
  // (which is invalid once the element is unmounted).
  LocationPingService? _service;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    Future.microtask(_restoreShift);
  }

  Future<void> _restoreShift() async {
    final service = ref.read(locationPingServiceProvider);
    await service.restoreBufferedPings();
    await service.flush();
    final restored = await service.restoreShift();
    if (!mounted || restored == null) return;
    ref.read(fieldShiftProvider.notifier).state = restored;
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    // Teardown/logout: release the background location subscription. (Plain
    // backgrounding does not dispose this widget, so tracking survives that.)
    // Use the captured service — `ref` is no longer usable during dispose.
    _service?.stopBackgroundTracking();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _scheduleNext();
  }

  void _scheduleNext({bool immediate = false}) {
    _timer?.cancel();
    if (!_foreground) return;
    final service = ref.read(locationPingServiceProvider);
    final hasActiveJob = _lastWorkOrderId != null;
    final interval = service.nextInterval(hasActiveJob: hasActiveJob);
    if (interval == null) return;
    _timer = Timer(immediate ? Duration.zero : interval, () async {
      await service.captureOnce(
        hasActiveJob: hasActiveJob,
        workOrderId: _lastWorkOrderId,
      );
      await service.flush();
      if (mounted) _scheduleNext();
    });
  }

  @override
  Widget build(BuildContext context) {
    final shift = ref.watch(fieldShiftProvider);
    final workOrderId = _activeWorkOrderId(ref.watch(jobsListProvider).value);
    if (shift != _lastShift || workOrderId != _lastWorkOrderId) {
      final shiftChanged = shift != _lastShift;
      _lastShift = shift;
      _lastWorkOrderId = workOrderId;
      final service = ref.read(locationPingServiceProvider);
      _service = service;
      service.setShift(shift);
      service.setActiveWorkOrder(workOrderId);
      // Native background stream keeps fixes flowing when backgrounded; the
      // foreground timer below still covers the stationary heartbeat in-app.
      if (shift == ShiftState.onShift) {
        service.startBackgroundTracking(workOrderId: workOrderId);
      } else {
        service.stopBackgroundTracking();
      }
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          _scheduleNext(immediate: shiftChanged && shift == ShiftState.onShift);
        }
      });
    }
    return widget.child;
  }
}

class LocationSharingControls extends ConsumerStatefulWidget {
  const LocationSharingControls({super.key});

  @override
  ConsumerState<LocationSharingControls> createState() =>
      _LocationSharingControlsState();
}

class _LocationSharingControlsState
    extends ConsumerState<LocationSharingControls> {
  bool _updating = false;

  Future<void> _setShift(ShiftState shift) async {
    setState(() => _updating = true);
    final ok = await ref.read(locationPingServiceProvider).updateShift(shift);
    if (!mounted) return;
    if (ok) {
      ref.read(fieldShiftProvider.notifier).state = shift;
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Could not update location sharing')),
      );
    }
    setState(() => _updating = false);
  }

  @override
  Widget build(BuildContext context) {
    final shift = ref.watch(fieldShiftProvider);
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Location sharing', style: theme.textTheme.titleSmall),
            const SizedBox(height: 8),
            SegmentedButton<ShiftState>(
              segments: const [
                ButtonSegment(
                  value: ShiftState.onShift,
                  icon: Icon(Icons.location_on_outlined),
                  label: Text('Shift'),
                ),
                ButtonSegment(
                  value: ShiftState.onBreak,
                  icon: Icon(Icons.pause_circle_outline),
                  label: Text('Break'),
                ),
                ButtonSegment(
                  value: ShiftState.offShift,
                  icon: Icon(Icons.location_off_outlined),
                  label: Text('Off'),
                ),
              ],
              selected: {shift},
              onSelectionChanged: _updating
                  ? null
                  : (values) => _setShift(values.first),
            ),
            const SizedBox(height: 8),
            Text(
              shift == ShiftState.onShift
                  ? 'Sharing while the app is open.'
                  : shift == ShiftState.onBreak
                  ? 'Paused for break.'
                  : 'Not sharing.',
              style: theme.textTheme.bodySmall,
            ),
          ],
        ),
      ),
    );
  }
}
