import '../../app/status_presentation.dart';
import '../../core/location/map_coordinates.dart';

/// Wire models for the field jobs API. Hand-rolled fromJson keeps the app
/// free of codegen for plain DTOs.
class JobSummary {
  JobSummary({
    required this.id,
    required this.title,
    required this.status,
    required this.workType,
    required this.priority,
    this.description,
    this.scheduledStart,
    this.scheduledEnd,
    this.estimatedDurationMinutes,
    this.startedAt,
    this.pausedAt,
    this.resumedAt,
    this.completedAt,
    this.totalActiveSeconds,
    StatusPresentation? statusPresentation,
  }) : statusPresentation =
           statusPresentation ?? StatusPresentation.neutralFallback(status);

  final String id;
  final String title;
  final String status;
  final StatusPresentation statusPresentation;
  final String workType;
  final String priority;
  final String? description;
  final DateTime? scheduledStart;
  final DateTime? scheduledEnd;
  final int? estimatedDurationMinutes;
  final DateTime? startedAt;
  final DateTime? pausedAt;
  final DateTime? resumedAt;
  final DateTime? completedAt;
  final int? totalActiveSeconds;

  factory JobSummary.fromJson(Map<String, dynamic> json) => JobSummary(
    id: json['id'] as String,
    title: json['title'] as String,
    status: json['status'] as String,
    statusPresentation: StatusPresentation.fromJsonOrFallback(
      json['status_presentation'],
      json['status'] as String,
    ),
    workType: json['work_type'] as String,
    priority: json['priority'] as String,
    description: json['description'] as String?,
    scheduledStart: _date(json['scheduled_start']),
    scheduledEnd: _date(json['scheduled_end']),
    estimatedDurationMinutes: json['estimated_duration_minutes'] as int?,
    startedAt: _date(json['started_at']),
    pausedAt: _date(json['paused_at']),
    resumedAt: _date(json['resumed_at']),
    completedAt: _date(json['completed_at']),
    totalActiveSeconds: json['total_active_seconds'] as int?,
  );
}

class JobCustomer {
  const JobCustomer({
    this.subscriberId,
    this.name,
    this.phone,
    this.email,
    this.addressText,
    this.servicePlan,
    this.accountNumber,
    this.status,
  });

  final String? subscriberId;
  final String? name;
  final String? phone;
  final String? email;
  final String? addressText;
  final String? servicePlan;
  final String? accountNumber;
  final String? status;

  factory JobCustomer.fromJson(Map<String, dynamic> json) => JobCustomer(
    subscriberId: json['subscriber_id']?.toString(),
    name: json['name'] as String?,
    phone: json['phone'] as String?,
    email: json['email'] as String?,
    addressText: json['address_text'] as String?,
    servicePlan: json['service_plan'] as String?,
    accountNumber: json['account_number'] as String?,
    status: json['status'] as String?,
  );
}

class JobLocation {
  const JobLocation({
    this.latitude,
    this.longitude,
    this.addressText,
    required this.source,
  });

  final double? latitude;
  final double? longitude;
  final String? addressText;
  final String source;

  factory JobLocation.fromJson(Map<String, dynamic> json) => JobLocation(
    latitude: (json['latitude'] as num?)?.toDouble(),
    longitude: (json['longitude'] as num?)?.toDouble(),
    addressText: json['address_text'] as String?,
    source: json['source'] as String? ?? 'none',
  );

  Map<String, dynamic> toJson() => {
    'latitude': latitude,
    'longitude': longitude,
    'address_text': addressText,
    'source': source,
  };

  bool get hasCoordinates {
    return isValidMapCoordinate(latitude, longitude);
  }

  /// Navigation handoff: precise coordinates when geocoded, otherwise a
  /// text search the maps app can resolve. Null when nothing is known.
  Uri? get mapsUri {
    if (hasCoordinates) {
      return Uri.https('www.google.com', '/maps/dir/', {
        'api': '1',
        'destination': '$latitude,$longitude',
      });
    }
    final address = addressText;
    if (address == null || address.isEmpty) return null;
    return Uri.https('www.google.com', '/maps/search/', {
      'api': '1',
      'query': address,
    });
  }
}

class JobDestination {
  const JobDestination({
    required this.destinationType,
    this.destinationId,
    required this.label,
    this.latitude,
    this.longitude,
    this.addressText,
  });

  final String destinationType;
  final String? destinationId;
  final String label;
  final double? latitude;
  final double? longitude;
  final String? addressText;

  factory JobDestination.fromJson(Map<String, dynamic> json) => JobDestination(
    destinationType: json['destination_type'] as String? ?? 'other',
    destinationId: json['destination_id']?.toString(),
    label: json['label'] as String? ?? 'Destination',
    latitude: (json['latitude'] as num?)?.toDouble(),
    longitude: (json['longitude'] as num?)?.toDouble(),
    addressText: json['address_text'] as String?,
  );

  bool get hasCoordinates => isValidMapCoordinate(latitude, longitude);

  Uri? get mapsUri {
    if (hasCoordinates) {
      return Uri.https('www.google.com', '/maps/dir/', {
        'api': '1',
        'destination': '$latitude,$longitude',
      });
    }
    final address = addressText;
    if (address == null || address.isEmpty) return null;
    return Uri.https('www.google.com', '/maps/search/', {
      'api': '1',
      'query': address,
    });
  }

  Map<String, dynamic> toTransitionPayload() => {
    'destination_type': destinationType,
    'destination_id': ?destinationId,
    'destination_label': label,
    'destination_latitude': ?latitude,
    'destination_longitude': ?longitude,
  };
}

class JobSiteContact {
  const JobSiteContact({this.name, this.phone, this.email, this.relationship});

  final String? name;
  final String? phone;
  final String? email;
  final String? relationship;

  factory JobSiteContact.fromJson(Map<String, dynamic> json) => JobSiteContact(
    name: json['name'] as String?,
    phone: json['phone'] as String?,
    email: json['email'] as String?,
    relationship: json['relationship'] as String?,
  );
}

class JobVisitHistoryItem {
  const JobVisitHistoryItem({
    required this.workOrderId,
    required this.title,
    this.workType,
    this.status,
    this.completedAt,
  });

  final String workOrderId;
  final String title;
  final String? workType;
  final String? status;
  final DateTime? completedAt;

  factory JobVisitHistoryItem.fromJson(Map<String, dynamic> json) =>
      JobVisitHistoryItem(
        workOrderId: json['work_order_id']?.toString() ?? '',
        title: json['title'] as String? ?? 'Previous visit',
        workType: json['work_type'] as String?,
        status: json['status'] as String?,
        completedAt: _date(json['completed_at']),
      );
}

class JobOpenTicketItem {
  const JobOpenTicketItem({
    required this.id,
    this.ref,
    this.subject,
    this.status,
  });

  final String id;
  final String? ref;
  final String? subject;
  final String? status;

  factory JobOpenTicketItem.fromJson(Map<String, dynamic> json) =>
      JobOpenTicketItem(
        id: json['id']?.toString() ?? '',
        ref: json['ref'] as String?,
        subject: json['subject'] as String?,
        status: json['status'] as String?,
      );
}

class JobChatMessage {
  const JobChatMessage({
    required this.id,
    required this.body,
    required this.direction,
    this.authorName,
    required this.createdAt,
    this.readAt,
  });

  final String id;
  final String body;
  final String direction;
  final String? authorName;
  final DateTime createdAt;
  final DateTime? readAt;

  bool get isCustomer => direction == 'customer';

  factory JobChatMessage.fromJson(Map<String, dynamic> json) => JobChatMessage(
    id: json['id'].toString(),
    body: json['body'] as String? ?? '',
    direction: json['direction'] as String? ?? 'staff',
    authorName: json['author_name'] as String?,
    createdAt: _date(json['created_at']) ?? DateTime.now().toUtc(),
    readAt: _date(json['read_at']),
  );
}

class JobChatThread {
  const JobChatThread({
    required this.available,
    this.canSend = false,
    this.conversationId,
    this.customerName,
    this.messages = const [],
  });

  final bool available;
  final bool canSend;
  final String? conversationId;
  final String? customerName;
  final List<JobChatMessage> messages;

  factory JobChatThread.fromJson(Map<String, dynamic> json) => JobChatThread(
    available: json['available'] as bool? ?? false,
    canSend: json['can_send'] as bool? ?? false,
    conversationId: json['conversation_id']?.toString(),
    customerName: json['customer_name'] as String?,
    messages: _typedList(json['messages'], JobChatMessage.fromJson),
  );
}

class JobCompletionRequirements {
  const JobCompletionRequirements({
    required this.evidenceRequired,
    required this.minimumPhotoCount,
    required this.customerSignoffRequired,
    required this.signatureUnavailableReasonAllowed,
  });

  /// Conservative fallback for an older server or cached detail that predates
  /// the contract. The server still revalidates every queued transition.
  static const safeFallback = JobCompletionRequirements(
    evidenceRequired: true,
    minimumPhotoCount: 1,
    customerSignoffRequired: true,
    signatureUnavailableReasonAllowed: true,
  );

  final bool evidenceRequired;
  final int minimumPhotoCount;
  final bool customerSignoffRequired;
  final bool signatureUnavailableReasonAllowed;

  factory JobCompletionRequirements.fromJson(Map<String, dynamic> json) =>
      JobCompletionRequirements(
        evidenceRequired: json['evidence_required'] as bool? ?? true,
        minimumPhotoCount: json['minimum_photo_count'] as int? ?? 1,
        customerSignoffRequired:
            json['customer_signoff_required'] as bool? ?? true,
        signatureUnavailableReasonAllowed:
            json['signature_unavailable_reason_allowed'] as bool? ?? true,
      );
}

class JobLifecycleReference {
  const JobLifecycleReference({
    required this.id,
    required this.title,
    required this.status,
    this.number,
  });

  final String id;
  final String title;
  final String status;
  final String? number;

  factory JobLifecycleReference.fromJson(Map<String, dynamic> json) =>
      JobLifecycleReference(
        id: json['id'].toString(),
        title: (json['name'] ?? json['title'] ?? '').toString(),
        status: (json['status'] ?? '').toString(),
        number: json['number'] as String?,
      );
}

class JobCustomerExperience {
  const JobCustomerExperience({
    this.project,
    this.projectTask,
    this.originTicket,
    this.projectTaskTicket,
  });

  final JobLifecycleReference? project;
  final JobLifecycleReference? projectTask;
  final JobLifecycleReference? originTicket;
  final JobLifecycleReference? projectTaskTicket;

  factory JobCustomerExperience.fromJson(Map<String, dynamic> json) =>
      JobCustomerExperience(
        project: _lifecycleReference(json['project']),
        projectTask: _lifecycleReference(json['project_task']),
        originTicket: _lifecycleReference(json['origin_ticket']),
        projectTaskTicket: _lifecycleReference(json['project_task_ticket']),
      );

  bool get isEmpty =>
      project == null &&
      projectTask == null &&
      originTicket == null &&
      projectTaskTicket == null;
}

JobLifecycleReference? _lifecycleReference(dynamic value) {
  if (value is! Map) return null;
  return JobLifecycleReference.fromJson(value.cast<String, dynamic>());
}

class JobDetail {
  const JobDetail({
    required this.job,
    required this.location,
    this.completionRequirements = JobCompletionRequirements.safeFallback,
    this.customerExperience = const JobCustomerExperience(),
    this.customer,
    this.accessNotes,
    this.additionalContacts = const [],
    this.recentVisits = const [],
    this.openTickets = const [],
    this.notes = const [],
    this.attachments = const [],
    this.materials = const [],
    this.materialRequests = const [],
    this.worklogs = const [],
    this.history = const [],
  });

  final JobSummary job;
  final JobLocation location;
  final JobCompletionRequirements completionRequirements;
  final JobCustomerExperience customerExperience;
  final JobCustomer? customer;
  final String? accessNotes;
  final List<JobSiteContact> additionalContacts;
  final List<JobVisitHistoryItem> recentVisits;
  final List<JobOpenTicketItem> openTickets;
  final List<Map<String, dynamic>> notes;
  final List<Map<String, dynamic>> attachments;
  final List<Map<String, dynamic>> materials;
  final List<Map<String, dynamic>> materialRequests;
  final List<Map<String, dynamic>> worklogs;
  final List<Map<String, dynamic>> history;

  int get completionPhotoCount =>
      attachments.where((item) => item['kind'] == 'photo').length;

  bool get hasCompletionSignature =>
      attachments.any((item) => item['kind'] == 'signature');

  factory JobDetail.fromJson(Map<String, dynamic> json) => JobDetail(
    job: JobSummary.fromJson((json['job'] as Map).cast<String, dynamic>()),
    location: JobLocation.fromJson(
      (json['location'] as Map).cast<String, dynamic>(),
    ),
    completionRequirements: json['completion_requirements'] is Map
        ? JobCompletionRequirements.fromJson(
            (json['completion_requirements'] as Map).cast<String, dynamic>(),
          )
        : JobCompletionRequirements.safeFallback,
    customerExperience: json['customer_experience'] is Map
        ? JobCustomerExperience.fromJson(
            (json['customer_experience'] as Map).cast<String, dynamic>(),
          )
        : const JobCustomerExperience(),
    customer: json['customer'] != null
        ? JobCustomer.fromJson(
            (json['customer'] as Map).cast<String, dynamic>(),
          )
        : null,
    accessNotes: json['access_notes'] as String?,
    additionalContacts: _typedList(
      json['additional_contacts'],
      JobSiteContact.fromJson,
    ),
    recentVisits: _typedList(
      json['recent_visits'],
      JobVisitHistoryItem.fromJson,
    ),
    openTickets: _typedList(json['open_tickets'], JobOpenTicketItem.fromJson),
    notes: _mapList(json['notes']),
    attachments: _mapList(json['attachments']),
    materials: _mapList(json['materials']),
    materialRequests: _mapList(json['material_requests']),
    worklogs: _mapList(json['worklogs']),
    history: _mapList(json['history']),
  );
}

String actionLabel(String action) => switch (action) {
  'accept' => 'Accept Work',
  'en_route' => 'En Route',
  'arrived' => 'Arrived',
  'start' => 'Start Work',
  'pause' => 'Pause Work',
  'hold' => 'Pause Work',
  'resume' => 'Resume Work',
  'complete' => 'Complete Work',
  _ => action,
};

DateTime? _date(Object? value) =>
    value is String ? DateTime.tryParse(value) : null;

List<Map<String, dynamic>> _mapList(Object? value) {
  final items = switch (value) {
    final List list => list,
    {'items': final List list} => list,
    _ => const [],
  };
  return [
    for (final item in items)
      if (item is Map) item.cast<String, dynamic>(),
  ];
}

List<T> _typedList<T>(Object? value, T Function(Map<String, dynamic>) build) {
  return [for (final item in _mapList(value)) build(item)];
}
