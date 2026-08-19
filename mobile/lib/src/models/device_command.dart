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
  bool get accepted =>
      const {'queued', 'waiting', 'succeeded'}.contains(status);

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
