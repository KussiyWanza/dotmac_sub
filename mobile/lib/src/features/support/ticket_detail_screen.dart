import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../config/env.dart';
import '../../core/api_exception.dart';
import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../../models/ticket.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/attachment_picker.dart';
import '../../widgets/status_chip.dart';
import 'chat_screen.dart';

class TicketDetailScreen extends ConsumerStatefulWidget {
  const TicketDetailScreen({super.key, required this.ticketId});

  final String ticketId;

  @override
  ConsumerState<TicketDetailScreen> createState() => _TicketDetailScreenState();
}

class _TicketDetailScreenState extends ConsumerState<TicketDetailScreen> {
  final _reply = TextEditingController();
  bool _sending = false;
  List<PickedAttachment> _attachments = [];

  @override
  void dispose() {
    _reply.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final body = _reply.text.trim();
    // A reply needs either text or at least one attachment.
    if (body.isEmpty && _attachments.isEmpty) return;
    setState(() => _sending = true);
    try {
      await ref.read(supportRepositoryProvider).addComment(
            widget.ticketId,
            body,
            attachmentPaths: _attachments.isEmpty
                ? null
                : [for (final a in _attachments) a.path],
          );
      _reply.clear();
      setState(() => _attachments = []);
      ref.invalidate(ticketCommentsProvider(widget.ticketId));
      ref.invalidate(ticketProvider(widget.ticketId));
    } on ApiException catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(e.message)));
      }
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  /// Compact "attach" entry for the composer (the inline strip only renders once
  /// something is picked). Delegates to [AttachmentPicker.pickInto] so the
  /// camera/gallery sheet and the ≤5 files / ≤5 MB validation live in one place.
  Future<void> _openAttachSheet() async {
    final next = await AttachmentPicker.pickInto(context, _attachments);
    if (next != null && mounted) setState(() => _attachments = next);
  }

  Future<void> _rateSupport(Ticket t) async {
    final result = await showDialog<(int, String)>(
      context: context,
      builder: (_) => _SupportRatingDialog(initial: t.csatRating),
    );
    if (result == null) return;
    final (rating, comment) = result;
    try {
      await ref
          .read(supportRepositoryProvider)
          .rateTicket(t.id, rating: rating, comment: comment);
      ref.invalidate(ticketProvider(widget.ticketId));
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Thanks for your feedback!')),
        );
      }
    } on ApiException catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(e.message)));
      }
    }
  }

  Future<void> _confirmResolution(Ticket ticket) async {
    try {
      await ref.read(supportRepositoryProvider).confirmResolution(ticket.id);
      ref.invalidate(ticketProvider(widget.ticketId));
      ref.invalidate(ticketCommentsProvider(widget.ticketId));
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('Thanks for confirming the resolution.'),
          ),
        );
      }
    } on ApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.message)));
      }
    }
  }

  Future<void> _disputeResolution(Ticket ticket) async {
    final controller = TextEditingController();
    final reason = await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('What still needs attention?'),
        content: TextField(
          controller: controller,
          autofocus: true,
          maxLines: 4,
          maxLength: 2000,
          decoration: const InputDecoration(
            hintText: 'Describe what is still wrong',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () =>
                Navigator.pop(dialogContext, controller.text.trim()),
            child: const Text('Reopen ticket'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (reason == null) return;
    try {
      await ref
          .read(supportRepositoryProvider)
          .disputeResolution(ticket.id, reason: reason);
      ref.invalidate(ticketProvider(widget.ticketId));
      ref.invalidate(ticketCommentsProvider(widget.ticketId));
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('The ticket has been reopened.')),
        );
      }
    } on ApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.message)));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final ticket = ref.watch(ticketProvider(widget.ticketId));
    final comments = ref.watch(ticketCommentsProvider(widget.ticketId));

    return Scaffold(
      appBar: AppBar(
        title: const Text('Ticket'),
        actions: [
          IconButton(
            tooltip: 'Chat about this ticket',
            icon: const Icon(Icons.chat_bubble_outline),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(
                builder: (_) => ChatScreen(
                  sessionEndpoint:
                      '/me/chat/session?ticket_id=${widget.ticketId}',
                ),
              ),
            ),
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: AsyncValueView(
              value: ticket,
              onRetry: () => ref.invalidate(ticketProvider(widget.ticketId)),
              data: (t) => ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          t.title,
                          style: Theme.of(context).textTheme.titleLarge,
                        ),
                      ),
                      StatusChip.fromPresentation(t.statusPresentation),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    '${t.number ?? t.id.substring(0, 8)} · ${t.priority} priority · ${Fmt.date(t.createdAt)}',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  const SizedBox(height: 16),
                  if (t.description != null && t.description!.isNotEmpty)
                    Card(
                      child: Padding(
                        padding: const EdgeInsets.all(16),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(t.description!),
                            if (t.attachments.isNotEmpty) ...[
                              const SizedBox(height: 12),
                              _AttachmentStrip(attachments: t.attachments),
                            ],
                          ],
                        ),
                      ),
                    ),
                  const SizedBox(height: 16),
                  if (t.canConfirmResolution || t.canDisputeResolution) ...[
                    Card(
                      color: Theme.of(context).colorScheme.primaryContainer,
                      child: Padding(
                        padding: const EdgeInsets.all(16),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            Text(
                              'Is this issue fixed?',
                              style: Theme.of(context).textTheme.titleMedium,
                            ),
                            const SizedBox(height: 6),
                            const Text(
                              'Support has proposed a resolution. Confirm it or tell us what still needs attention.',
                            ),
                            const SizedBox(height: 12),
                            if (t.canConfirmResolution)
                              FilledButton.icon(
                                onPressed: () => _confirmResolution(t),
                                icon: const Icon(Icons.check_circle_outline),
                                label: const Text('Yes, it is fixed'),
                              ),
                            if (t.canDisputeResolution)
                              OutlinedButton.icon(
                                onPressed: () => _disputeResolution(t),
                                icon: const Icon(Icons.report_problem_outlined),
                                label: const Text('No, I still need help'),
                              ),
                          ],
                        ),
                      ),
                    ),
                    const SizedBox(height: 16),
                  ],
                  if (t.canRate) ...[
                    _CsatCard(
                      rating: t.csatRating,
                      onRate: () => _rateSupport(t),
                    ),
                    const SizedBox(height: 16),
                  ],
                  Text(
                    'Conversation',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                  const SizedBox(height: 8),
                  comments.when(
                    loading: () => const Padding(
                      padding: EdgeInsets.all(24),
                      child: Center(child: CircularProgressIndicator()),
                    ),
                    error: (e, _) => Text('Could not load replies: $e'),
                    data: (page) {
                      final visible =
                          page.items.where((c) => !c.isInternal).toList();
                      if (visible.isEmpty) {
                        return const Padding(
                          padding: EdgeInsets.symmetric(vertical: 16),
                          child: Text('No replies yet.'),
                        );
                      }
                      return Column(
                        children: [
                          for (final c in visible)
                            Card(
                              child: Padding(
                                padding: const EdgeInsets.fromLTRB(
                                  16,
                                  12,
                                  16,
                                  12,
                                ),
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    if (c.body.isNotEmpty) Text(c.body),
                                    if (c.attachments.isNotEmpty) ...[
                                      const SizedBox(height: 8),
                                      _AttachmentStrip(
                                        attachments: c.attachments,
                                      ),
                                    ],
                                    const SizedBox(height: 4),
                                    Text(
                                      Fmt.dateTime(c.createdAt),
                                      style: Theme.of(
                                        context,
                                      ).textTheme.bodySmall,
                                    ),
                                  ],
                                ),
                              ),
                            ),
                        ],
                      );
                    },
                  ),
                ],
              ),
            ),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (_attachments.isNotEmpty) ...[
                    AttachmentPicker(
                      attachments: _attachments,
                      enabled: !_sending,
                      onChanged: (a) => setState(() => _attachments = a),
                    ),
                    const SizedBox(height: 8),
                  ],
                  Row(
                    children: [
                      IconButton(
                        tooltip: 'Attach photo',
                        onPressed: _sending ? null : _openAttachSheet,
                        icon: const Icon(Icons.attach_file),
                      ),
                      Expanded(
                        child: TextField(
                          controller: _reply,
                          minLines: 1,
                          maxLines: 4,
                          decoration: const InputDecoration(
                            hintText: 'Write a reply…',
                            isDense: true,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      IconButton.filled(
                        tooltip: 'Send reply',
                        onPressed: _sending ? null : _send,
                        icon: _sending
                            ? const SizedBox(
                                height: 18,
                                width: 18,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              )
                            : const Icon(Icons.send),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Renders uploaded attachments as a horizontal row of thumbnails (images) /
/// file chips (PDFs/other). Tapping an image opens it full-screen; tapping a
/// file chip opens it in the device's default viewer/browser.
class _AttachmentStrip extends StatelessWidget {
  const _AttachmentStrip({required this.attachments});

  final List<TicketAttachment> attachments;

  Future<void> _openExternal(BuildContext context, TicketAttachment a) async {
    final raw = a.url;
    if (raw == null) return;
    final messenger = ScaffoldMessenger.of(context);
    final uri = Uri.parse(Env.resolveUrl(raw));
    final ok = await launchUrl(uri, mode: LaunchMode.externalApplication);
    if (!ok) {
      messenger.showSnackBar(
        const SnackBar(content: Text('Could not open this attachment.')),
      );
    }
  }

  void _openImage(BuildContext context, TicketAttachment a) {
    final url = a.url;
    if (url == null) return;
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => Scaffold(
          backgroundColor: Colors.black,
          appBar: AppBar(
            backgroundColor: Colors.black,
            foregroundColor: Colors.white,
            title: Text(a.filename, overflow: TextOverflow.ellipsis),
          ),
          body: Center(
            child: InteractiveViewer(
              child: Image.network(
                Env.resolveUrl(url),
                fit: BoxFit.contain,
                errorBuilder: (_, __, ___) => const Icon(
                  Icons.broken_image_outlined,
                  color: Colors.white54,
                  size: 48,
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        for (final a in attachments)
          if (a.isImage && a.url != null)
            InkWell(
              onTap: () => _openImage(context, a),
              borderRadius: BorderRadius.circular(8),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: Image.network(
                  Env.resolveUrl(a.url!),
                  width: 72,
                  height: 72,
                  fit: BoxFit.cover,
                  errorBuilder: (_, __, ___) => Container(
                    width: 72,
                    height: 72,
                    color: scheme.surfaceContainerHighest,
                    child: const Icon(Icons.broken_image_outlined),
                  ),
                ),
              ),
            )
          else
            ActionChip(
              avatar: Icon(
                a.isPdf
                    ? Icons.picture_as_pdf_outlined
                    : Icons.insert_drive_file_outlined,
                size: 18,
              ),
              label: Text(a.filename, overflow: TextOverflow.ellipsis),
              onPressed: a.url == null ? null : () => _openExternal(context, a),
            ),
      ],
    );
  }
}

/// Support-satisfaction (CSAT) card on a closed ticket: shows the
/// score once rated, otherwise invites a rating.
class _CsatCard extends StatelessWidget {
  const _CsatCard({required this.rating, required this.onRate});

  final int? rating;
  final VoidCallback onRate;

  @override
  Widget build(BuildContext context) {
    final rated = rating != null;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    rated ? 'You rated this support' : 'How was your support?',
                    style: const TextStyle(fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 4),
                  if (rated)
                    Row(
                      children: [
                        for (var i = 1; i <= 5; i++)
                          Icon(
                            i <= rating! ? Icons.star : Icons.star_border,
                            color: context.semantic.warning,
                            size: 20,
                          ),
                      ],
                    )
                  else
                    Text(
                      'Let us know how we did resolving your ticket.',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            rated
                ? TextButton(onPressed: onRate, child: const Text('Change'))
                : FilledButton.tonalIcon(
                    onPressed: onRate,
                    icon: const Icon(Icons.star_outline, size: 18),
                    label: const Text('Rate'),
                  ),
          ],
        ),
      ),
    );
  }
}

/// Star rating + optional comment for support CSAT. Pops `(rating, comment)`.
class _SupportRatingDialog extends StatefulWidget {
  const _SupportRatingDialog({this.initial});

  final int? initial;

  @override
  State<_SupportRatingDialog> createState() => _SupportRatingDialogState();
}

class _SupportRatingDialogState extends State<_SupportRatingDialog> {
  late int _rating = widget.initial ?? 0;
  final _comment = TextEditingController();

  @override
  void dispose() {
    _comment.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Rate your support'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              for (var i = 1; i <= 5; i++)
                IconButton(
                  onPressed: () => setState(() => _rating = i),
                  icon: Icon(
                    i <= _rating ? Icons.star : Icons.star_border,
                    color: context.semantic.warning,
                    size: 32,
                  ),
                ),
            ],
          ),
          TextField(
            controller: _comment,
            decoration: const InputDecoration(labelText: 'Comment (optional)'),
            maxLines: 3,
            maxLength: 2000,
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: _rating == 0
              ? null
              : () =>
                  Navigator.of(context).pop((_rating, _comment.text.trim())),
          child: const Text('Submit'),
        ),
      ],
    );
  }
}
