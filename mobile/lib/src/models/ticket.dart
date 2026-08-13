import 'status_presentation.dart';

/// An uploaded file attached to a ticket or comment (mirrors the attachment
/// objects the support API returns under `attachments`).
class TicketAttachment {
  TicketAttachment({
    required this.id,
    required this.filename,
    this.url,
    this.contentType,
    this.size,
  });

  final String id;
  final String filename;
  final String? url;
  final String? contentType;
  final int? size;

  bool get isImage => (contentType ?? '').startsWith('image/');
  bool get isPdf => (contentType ?? '').contains('pdf');

  factory TicketAttachment.fromJson(Map<String, dynamic> json) =>
      TicketAttachment(
        id: json['id'].toString(),
        filename: (json['filename'] ?? json['name'] ?? 'attachment').toString(),
        url: (json['url'] ?? json['download_url'])?.toString(),
        contentType: (json['content_type'] ?? json['mime_type'])?.toString(),
        size: (json['size'] as num?)?.toInt(),
      );
}

class TicketSelfCareAction {
  const TicketSelfCareAction({required this.key, required this.allowed});

  final String key;
  final bool allowed;

  factory TicketSelfCareAction.fromJson(Map<String, dynamic> json) =>
      TicketSelfCareAction(
        key: (json['key'] ?? '').toString(),
        allowed: json['allowed'] as bool? ?? false,
      );
}

List<TicketAttachment> _toAttachments(dynamic v) {
  if (v is! List) return const [];
  return v
      .whereType<Map>()
      .map((e) => TicketAttachment.fromJson(e.cast<String, dynamic>()))
      .toList();
}

/// Mirrors TicketRead and TicketCommentRead from app/schemas/support.py.
class Ticket {
  Ticket({
    required this.id,
    required this.title,
    required this.status,
    required this.priority,
    this.number,
    this.description,
    this.ticketType,
    this.channel,
    this.tags = const [],
    this.attachments = const [],
    this.createdAt,
    this.updatedAt,
    this.resolvedAt,
    this.closedAt,
    this.csatRating,
    this.resolutionActions = const [],
    StatusPresentation? statusPresentation,
  }) : statusPresentation =
            statusPresentation ?? StatusPresentation.neutralFallback(status);

  final String id;
  final String title;
  final String status;
  final StatusPresentation statusPresentation;
  final String priority;
  final String? number;
  final String? description;
  final String? ticketType;
  final String? channel;
  final List<String> tags;
  final List<TicketAttachment> attachments;
  final DateTime? createdAt;
  final DateTime? updatedAt;
  final DateTime? resolvedAt;
  final DateTime? closedAt;

  /// Support-satisfaction score (1-5) if the customer has rated this ticket.
  final int? csatRating;
  final List<TicketSelfCareAction> resolutionActions;

  bool get isOpen => closedAt == null && status != 'closed';

  /// The server decides whether CSAT is currently available.
  bool get canRate => resolutionActions.any(
        (action) => action.key == 'rate_support' && action.allowed,
      );

  bool get canConfirmResolution => resolutionActions.any(
        (action) => action.key == 'confirm_resolution' && action.allowed,
      );

  bool get canDisputeResolution => resolutionActions.any(
        (action) => action.key == 'dispute_resolution' && action.allowed,
      );

  factory Ticket.fromJson(Map<String, dynamic> json) => Ticket(
        id: json['id'].toString(),
        title: json['title'] as String? ?? '(no title)',
        status: json['status'] as String? ?? 'open',
        statusPresentation: json['status_presentation'] is Map
            ? StatusPresentation.fromJson(
                (json['status_presentation'] as Map).cast<String, dynamic>(),
              )
            : null,
        priority: json['priority'] as String? ?? 'normal',
        number: json['number'] as String?,
        description: json['description'] as String?,
        ticketType: json['ticket_type'] as String?,
        channel: json['channel'] as String?,
        tags: (json['tags'] as List? ?? const [])
            .map((e) => e.toString())
            .toList(),
        attachments: _toAttachments(json['attachments']),
        createdAt: _toDate(json['created_at']),
        updatedAt: _toDate(json['updated_at']),
        resolvedAt: _toDate(json['resolved_at']),
        closedAt: _toDate(json['closed_at']),
        csatRating: (json['csat_rating'] as num?)?.toInt(),
        resolutionActions: ((json['resolution_actions'] as List?) ?? const [])
            .whereType<Map>()
            .map(
              (item) =>
                  TicketSelfCareAction.fromJson(item.cast<String, dynamic>()),
            )
            .toList(),
      );
}

class TicketComment {
  TicketComment({
    required this.id,
    required this.ticketId,
    required this.body,
    this.isInternal = false,
    this.attachments = const [],
    this.createdAt,
  });

  final String id;
  final String ticketId;
  final String body;
  final bool isInternal;
  final List<TicketAttachment> attachments;
  final DateTime? createdAt;

  factory TicketComment.fromJson(Map<String, dynamic> json) => TicketComment(
        id: json['id'].toString(),
        ticketId: json['ticket_id'].toString(),
        body: json['body'] as String? ?? '',
        isInternal: json['is_internal'] as bool? ?? false,
        attachments: _toAttachments(json['attachments']),
        createdAt: _toDate(json['created_at']),
      );
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
