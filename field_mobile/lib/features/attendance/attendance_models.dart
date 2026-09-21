enum AttendanceAction { checkIn, checkOut }

extension AttendanceActionApi on AttendanceAction {
  String get apiPath => switch (this) {
    AttendanceAction.checkIn => 'check-in',
    AttendanceAction.checkOut => 'check-out',
  };

  String get apiValue => switch (this) {
    AttendanceAction.checkIn => 'check_in',
    AttendanceAction.checkOut => 'check_out',
  };
}

enum AttendanceState { notCheckedIn, checkedIn, checkedOut, ineligible }

AttendanceState _stateFromApi(Object? value) => switch (value) {
  'not_checked_in' => AttendanceState.notCheckedIn,
  'checked_in' => AttendanceState.checkedIn,
  'checked_out' => AttendanceState.checkedOut,
  'ineligible' => AttendanceState.ineligible,
  _ => throw const FormatException('Invalid attendance state'),
};

AttendanceAction _actionFromApi(Object? value) => switch (value) {
  'check_in' => AttendanceAction.checkIn,
  'check_out' => AttendanceAction.checkOut,
  _ => throw const FormatException('Invalid attendance action'),
};

class AttendanceView {
  const AttendanceView({
    required this.state,
    required this.attendanceDate,
    required this.timezone,
    required this.allowedActions,
    this.checkInAt,
    this.checkOutAt,
    this.workingHours,
    this.status,
    this.reason,
  });

  factory AttendanceView.fromJson(Map<String, Object?> json) {
    final rawActions = json['allowed_actions'];
    if (rawActions is! List) {
      throw const FormatException('Invalid attendance actions');
    }
    final attendanceDate = json['attendance_date'];
    final timezone = json['timezone'];
    if (attendanceDate is! String || timezone is! String) {
      throw const FormatException('Invalid attendance response');
    }
    return AttendanceView(
      state: _stateFromApi(json['state']),
      attendanceDate: attendanceDate,
      timezone: timezone,
      checkInAt: _dateTime(json['check_in_at']),
      checkOutAt: _dateTime(json['check_out_at']),
      workingHours: _double(json['working_hours']),
      status: json['status']?.toString(),
      allowedActions: rawActions.map(_actionFromApi).toSet(),
      reason: json['reason']?.toString(),
    );
  }

  final AttendanceState state;
  final String attendanceDate;
  final String timezone;
  final DateTime? checkInAt;
  final DateTime? checkOutAt;
  final double? workingHours;
  final String? status;
  final Set<AttendanceAction> allowedActions;
  final String? reason;

  bool get isCheckedIn => state == AttendanceState.checkedIn;
}

DateTime? _dateTime(Object? value) {
  if (value == null) return null;
  if (value is! String) throw const FormatException('Invalid attendance time');
  return DateTime.tryParse(value)?.toUtc() ??
      (throw const FormatException('Invalid attendance time'));
}

double? _double(Object? value) {
  if (value == null) return null;
  if (value is num) return value.toDouble();
  return double.tryParse(value.toString()) ??
      (throw const FormatException('Invalid working hours'));
}

class AttendancePosition {
  const AttendancePosition({
    required this.latitude,
    required this.longitude,
    required this.accuracyM,
    required this.observedAt,
  });

  final double latitude;
  final double longitude;
  final double? accuracyM;
  final DateTime observedAt;

  Map<String, Object?> toJson() => {
    'latitude': latitude,
    'longitude': longitude,
    'accuracy_m': accuracyM,
    'observed_at': observedAt.toUtc().toIso8601String(),
  };
}

class AttendanceFailure implements Exception {
  const AttendanceFailure(this.code, this.message, {this.unavailable = false});

  final String code;
  final String message;
  final bool unavailable;

  @override
  String toString() => message;
}
