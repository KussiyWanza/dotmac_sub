import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../attendance/attendance_models.dart';
import '../attendance/attendance_repository.dart';
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
    final attendance = ref.watch(attendanceControllerProvider).asData?.value;
    final effectiveShift = attendance?.isCheckedIn == true
        ? shift
        : ShiftState.offShift;
    if (attendance != null &&
        !attendance.isCheckedIn &&
        shift != ShiftState.offShift) {
      WidgetsBinding.instance.addPostFrameCallback((_) async {
        if (!mounted) return;
        ref.read(fieldShiftProvider.notifier).state = ShiftState.offShift;
        final service = ref.read(locationPingServiceProvider);
        service.setShift(ShiftState.offShift);
        await service.updateShift(ShiftState.offShift);
      });
    }
    final jobs = ref.watch(jobsListProvider);
    final workOrderId = jobs.when(
      data: _activeWorkOrderId,
      loading: () => _lastWorkOrderId,
      error: (_, _) => _lastWorkOrderId,
    );
    if (effectiveShift != _lastShift || workOrderId != _lastWorkOrderId) {
      final shiftChanged = effectiveShift != _lastShift;
      _lastShift = effectiveShift;
      _lastWorkOrderId = workOrderId;
      final service = ref.read(locationPingServiceProvider);
      _service = service;
      service.setShift(effectiveShift);
      service.setActiveWorkOrder(workOrderId);
      // Native background stream keeps fixes flowing when backgrounded; the
      // foreground timer below still covers the stationary heartbeat in-app.
      if (effectiveShift == ShiftState.onShift) {
        service.startBackgroundTracking(workOrderId: workOrderId);
      } else {
        service.stopBackgroundTracking();
      }
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          _scheduleNext(
            immediate: shiftChanged && effectiveShift == ShiftState.onShift,
          );
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

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _remindForLocation());
  }

  Future<void> _remindForLocation() async {
    if (ref.read(locationReminderShownProvider)) return;
    ref.read(locationReminderShownProvider.notifier).state = true;
    final source = ref.read(attendanceLocationSourceProvider);
    if (await source.isReady() || !mounted) return;
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('Enable location'),
        content: const Text(
          'Location must be turned on and allowed before you can check in and share your location.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(),
            child: const Text('Not now'),
          ),
          FilledButton(
            onPressed: () async {
              Navigator.of(dialogContext).pop();
              await source.requestAccess();
            },
            child: const Text('Enable location'),
          ),
        ],
      ),
    );
  }

  Future<bool> _setShift(ShiftState shift) async {
    final ok = await ref.read(locationPingServiceProvider).updateShift(shift);
    if (!mounted) return false;
    if (ok) ref.read(fieldShiftProvider.notifier).state = shift;
    return ok;
  }

  Future<void> _select(_LocationCardAction action) async {
    setState(() => _updating = true);
    String? message;
    try {
      switch (action) {
        case _LocationCardAction.checkIn:
          final attendance = await ref
              .read(attendanceControllerProvider.notifier)
              .punch(AttendanceAction.checkIn);
          if (attendance.state == AttendanceState.checkedIn) {
            final started = await _setShift(ShiftState.onShift);
            message = started
                ? 'Checked in. You are on shift and sharing your location.'
                : 'Checked in, but location sharing could not start. Tap On shift to retry.';
          } else {
            message = attendance.reason;
          }
          break;
        case _LocationCardAction.onShift:
          if (!await _setShift(ShiftState.onShift)) {
            await ref.read(attendanceControllerProvider.notifier).refresh();
            message =
                'Could not start location sharing. Confirm that you are checked in.';
          }
          break;
        case _LocationCardAction.checkOut:
          final attendance = await ref
              .read(attendanceControllerProvider.notifier)
              .punch(AttendanceAction.checkOut);
          if (attendance.state == AttendanceState.checkedOut) {
            // Stop capture immediately, then reconcile the server presence.
            ref.read(fieldShiftProvider.notifier).state = ShiftState.offShift;
            ref.read(locationPingServiceProvider).setShift(ShiftState.offShift);
            final stopped = await _setShift(ShiftState.offShift);
            message = stopped
                ? 'Checked out. Location sharing is off.'
                : 'Checked out. Tracking stopped on this device; server sync will retry.';
          }
          break;
      }
    } on AttendanceFailure catch (error) {
      message = error.message;
    } catch (_) {
      message = 'Attendance is temporarily unavailable. Please try again.';
    }
    if (!mounted) return;
    setState(() => _updating = false);
    if (message != null) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(message)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final shift = ref.watch(fieldShiftProvider);
    final attendanceAsync = ref.watch(attendanceControllerProvider);
    final attendance = attendanceAsync.asData?.value;
    final canCheckIn =
        attendance?.allowedActions.contains(AttendanceAction.checkIn) == true;
    final canShift = attendance?.isCheckedIn == true;
    final canCheckOut =
        attendance?.allowedActions.contains(AttendanceAction.checkOut) == true;
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Location sharing', style: theme.textTheme.titleSmall),
            const SizedBox(height: 8),
            SegmentedButton<_LocationCardAction>(
              emptySelectionAllowed: true,
              showSelectedIcon: false,
              segments: [
                ButtonSegment(
                  value: _LocationCardAction.checkIn,
                  enabled: canCheckIn && !_updating,
                  label: const Text('Check In'),
                ),
                ButtonSegment(
                  value: _LocationCardAction.onShift,
                  enabled: canShift && !_updating,
                  label: Text('On shift'),
                ),
                ButtonSegment(
                  value: _LocationCardAction.checkOut,
                  enabled: canCheckOut && !_updating,
                  label: Text('Check Out'),
                ),
              ],
              selected: canShift && shift == ShiftState.onShift
                  ? const {_LocationCardAction.onShift}
                  : const {},
              onSelectionChanged: _updating || attendance == null
                  ? null
                  : (values) {
                      if (values.isNotEmpty) _select(values.first);
                    },
            ),
            const SizedBox(height: 8),
            Text(
              _statusText(attendanceAsync, shift),
              style: theme.textTheme.bodySmall,
            ),
          ],
        ),
      ),
    );
  }
}

enum _LocationCardAction { checkIn, onShift, checkOut }

String _statusText(
  AsyncValue<AttendanceView> attendanceAsync,
  ShiftState shift,
) {
  final attendance = attendanceAsync.asData?.value;
  if (attendance == null) {
    return attendanceAsync.hasError
        ? 'Attendance is unavailable. Pull down to retry.'
        : 'Checking attendance…';
  }
  return switch (attendance.state) {
    AttendanceState.notCheckedIn =>
      'Check in to go on shift and start location sharing.',
    AttendanceState.checkedIn when shift == ShiftState.onShift =>
      'On shift · sharing location with dispatch.',
    AttendanceState.checkedIn =>
      'Checked in · location sharing could not start. Tap On shift to retry.',
    AttendanceState.checkedOut => 'Checked out · location sharing is off.',
    AttendanceState.ineligible =>
      attendance.reason ?? 'Attendance is not available for this account.',
  };
}
