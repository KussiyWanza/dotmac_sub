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
  /// The endpoint accepts multipart/form-data, including when no files are
  /// attached. Files use a repeatable `attachments` field (FROZEN backend
  /// contract: images + PDF, ≤5 MB each, ≤5 files).
  Future<Ticket> createTicket({
    required String title,
    String? description,
    String priority = 'normal',
    String? ticketType,
    List<String>? attachmentPaths,
  }) async {
    final fields = <String, String>{
      'title': title,
      if (description != null) 'description': description,
      'priority': priority,
      if (ticketType != null) 'ticket_type': ticketType,
    };
    final data = await guard(
      () => dio.post(
        '/me/support/tickets',
        data: _multipartBodyFor(fields, attachmentPaths),
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
  /// Uses the same multipart contract as [createTicket].
  Future<TicketComment> addComment(
    String ticketId,
    String body, {
    List<String>? attachmentPaths,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/support/tickets/$ticketId/comments',
        data: _multipartBodyFor({'body': body}, attachmentPaths),
      ),
    );
    return TicketComment.fromJson(data as Map<String, dynamic>);
  }

  /// Build the [FormData] required by the ticket and comment endpoints. The
  /// body stays multipart even without files because FastAPI declares the text
  /// inputs with `Form(...)`, not a JSON request model.
  FormData _multipartBodyFor(
    Map<String, String> fields,
    List<String>? attachmentPaths,
  ) {
    final form = FormData();
    form.fields.addAll(fields.entries);
    for (final path in attachmentPaths ?? const <String>[]) {
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
