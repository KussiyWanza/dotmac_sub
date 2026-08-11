import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/page.dart';
import '../models/ticket.dart';

/// Wraps the self-scoped support endpoints (app/api/me.py, prefix /me/support).
///
/// These require only authentication — the server forces every ticket to the
/// caller's own subscriber and strips staff-internal notes — unlike the
/// staff-gated /support/* endpoints, which return 403 for a subscriber token.
class SupportRepository {
  SupportRepository(this.dio);

  final Dio dio;

  /// GET /me/support/tickets?status=&limit=&offset=
  Future<Page<Ticket>> tickets({
    String? status,
    int limit = 50,
    int offset = 0,
  }) async {
    final data = await guard(
      () => dio.get(
        '/me/support/tickets',
        queryParameters: {
          if (status != null) 'status': status,
          'limit': limit,
          'offset': offset,
        },
      ),
    );
    return Page.fromJson(data as Map<String, dynamic>, Ticket.fromJson);
  }

  /// GET /me/support/tickets/{id}
  Future<Ticket> ticket(String id) async {
    final data = await guard(() => dio.get('/me/support/tickets/$id'));
    return Ticket.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/support/tickets/{id}/rate — CSAT on the support experience for a
  /// closed ticket (1-5 + optional comment). Returns the updated ticket.
  Future<Ticket> rateTicket(
    String id, {
    required int rating,
    String? comment,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/support/tickets/$id/rate',
        data: {
          'rating': rating,
          if (comment != null && comment.isNotEmpty) 'comment': comment,
        },
      ),
    );
    return Ticket.fromJson(data as Map<String, dynamic>);
  }

  Future<Ticket> confirmResolution(String id) async {
    final data = await guard(
      () => dio.post('/me/support/tickets/$id/confirm-resolution'),
    );
    return Ticket.fromJson(data as Map<String, dynamic>);
  }

  Future<Ticket> disputeResolution(String id, {String? reason}) async {
    final data = await guard(
      () => dio.post(
        '/me/support/tickets/$id/dispute-resolution',
        data: {if (reason != null && reason.isNotEmpty) 'reason': reason},
      ),
    );
    return Ticket.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/support/tickets — scoped to the caller; no subscriber id sent.
  ///
  /// When [attachmentPaths] is non-empty the request is sent as
  /// multipart/form-data with a repeatable `attachments` file field (FROZEN
  /// backend contract: images + PDF, ≤5 MB each, ≤5 files); otherwise a plain
  /// JSON body is posted as before.
  Future<Ticket> createTicket({
    required String title,
    String? description,
    String priority = 'normal',
    String? ticketType,
    List<String>? attachmentPaths,
  }) async {
    final fields = <String, dynamic>{
      'title': title,
      if (description != null) 'description': description,
      'priority': priority,
      if (ticketType != null) 'ticket_type': ticketType,
    };
    final data = await guard(
      () => dio.post(
        '/me/support/tickets',
        data: _bodyFor(fields, attachmentPaths),
      ),
    );
    return Ticket.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/support/tickets/{id}/comments
  Future<Page<TicketComment>> comments(
    String ticketId, {
    int limit = 100,
    int offset = 0,
  }) async {
    final data = await guard(
      () => dio.get(
        '/me/support/tickets/$ticketId/comments',
        queryParameters: {'limit': limit, 'offset': offset},
      ),
    );
    return Page.fromJson(data as Map<String, dynamic>, TicketComment.fromJson);
  }

  /// POST /me/support/tickets/{id}/comments
  ///
  /// Same multipart rules as [createTicket] when [attachmentPaths] is supplied.
  Future<TicketComment> addComment(
    String ticketId,
    String body, {
    List<String>? attachmentPaths,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/support/tickets/$ticketId/comments',
        data: _bodyFor({'body': body}, attachmentPaths),
      ),
    );
    return TicketComment.fromJson(data as Map<String, dynamic>);
  }

  /// Build either a plain JSON map (no files) or a [FormData] carrying the text
  /// [fields] plus a repeatable `attachments` file field — one entry per path,
  /// the frozen backend contract for ticket/comment uploads.
  Object _bodyFor(Map<String, dynamic> fields, List<String>? attachmentPaths) {
    if (attachmentPaths == null || attachmentPaths.isEmpty) return fields;
    final form = FormData();
    fields.forEach((k, v) => form.fields.add(MapEntry(k, v.toString())));
    for (final path in attachmentPaths) {
      form.files.add(
        MapEntry(
          'attachments',
          MultipartFile.fromFileSync(
            path,
            filename: path.split('/').last,
            contentType: _mediaTypeFor(path),
          ),
        ),
      );
    }
    return form;
  }

  /// Guess the upload content-type from the file extension. The server is the
  /// authority; this just helps it (and image-only galleries return jpg/png).
  static DioMediaType? _mediaTypeFor(String path) {
    final ext = path.toLowerCase().split('.').last;
    return switch (ext) {
      'jpg' || 'jpeg' => DioMediaType('image', 'jpeg'),
      'png' => DioMediaType('image', 'png'),
      'gif' => DioMediaType('image', 'gif'),
      'webp' => DioMediaType('image', 'webp'),
      'heic' => DioMediaType('image', 'heic'),
      'pdf' => DioMediaType('application', 'pdf'),
      _ => null,
    };
  }
}
