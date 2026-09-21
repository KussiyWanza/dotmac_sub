class DeviceCommandOutcome {
  const DeviceCommandOutcome({
    required this.command,
    required this.status,
    required this.subscriptionId,
    required this.message,
    this.deviceId,
    this.operationId,
  });

  final String command;
  final String status;
  final String subscriptionId;
  final String? deviceId;
  final String? operationId;
  final String message;

  bool get succeeded => status == 'succeeded';

  /// Delivered to the device, but exact readback confirmation is
  /// unavailable -- distinct from [succeeded] so a caller that cares about
  /// the difference can show it, while [accepted] still treats it as a
  /// non-failure outcome.
  bool get needsVerification => status == 'needs_verification';

  bool get accepted => const {
        'queued',
        'waiting',
        'succeeded',
        'needs_verification',
      }.contains(status);

  factory DeviceCommandOutcome.fromJson(Map<String, dynamic> json) =>
      DeviceCommandOutcome(
        command: json['command'] as String,
        status: json['status'] as String,
        subscriptionId: json['subscription_id'] as String,
        deviceId: json['device_id'] as String?,
        operationId: json['operation_id'] as String?,
        message: json['message'] as String,
      );
}
