/// Shift-scoped, adaptive location cadence (task #45).
///
/// Location is only collected while a tech is on shift, and the interval adapts
/// to what they're doing: tight while travelling to / working an active job,
/// relaxed when idle on shift, and *off entirely* off shift or on break. This
/// is the privacy and battery contract — encoded as a pure function so it can
/// be exhaustively tested without a device.
library;

enum ShiftState { offShift, onBreak, onShift }

extension ShiftStateApi on ShiftState {
  /// The presence status string the backend expects (matches FieldPresenceStatus).
  String get apiValue => switch (this) {
    ShiftState.offShift => 'off_shift',
    ShiftState.onBreak => 'break',
    ShiftState.onShift => 'on_shift',
  };

  static ShiftState? fromApiValue(String value) => switch (value) {
    'off_shift' => ShiftState.offShift,
    'break' => ShiftState.onBreak,
    'on_shift' || 'busy' => ShiftState.onShift,
    _ => null,
  };
}

const Duration activePingInterval = Duration(seconds: 30);
const Duration idlePingInterval = Duration(seconds: 120);

/// How long until the next fix, or null to stop pinging entirely.
Duration? pingInterval({
  required ShiftState shift,
  required bool hasActiveJob,
  required bool moving,
}) {
  if (shift != ShiftState.onShift) {
    return null; // off shift / on break → no location
  }
  if (hasActiveJob || moving) return activePingInterval;
  return idlePingInterval;
}
