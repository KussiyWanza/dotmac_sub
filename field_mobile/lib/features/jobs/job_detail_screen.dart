import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../app/theme.dart';
import '../execution/execution_controller.dart';
import 'job_models.dart';
import 'jobs_providers.dart';
import 'location_pin_screen.dart';

/// Launcher abstraction so widget tests assert the URI without opening apps.
typedef UriLauncher = Future<bool> Function(Uri uri);

final uriLauncherProvider = Provider<UriLauncher>((ref) => launchUrl);

class JobDetailScreen extends ConsumerWidget {
  const JobDetailScreen({super.key, required this.jobId});

  final String jobId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detail = ref.watch(jobDetailProvider(jobId));

    return detail.when(
      data: (data) => _JobDetailView(detail: data),
      loading: () =>
          const Scaffold(body: Center(child: CircularProgressIndicator())),
      error: (error, _) => Scaffold(
        appBar: AppBar(),
        body: const Center(child: Text('Could not load this job')),
      ),
    );
  }
}

class _JobDetailView extends ConsumerStatefulWidget {
  const _JobDetailView({required this.detail});

  final JobDetail detail;

  @override
  ConsumerState<_JobDetailView> createState() => _JobDetailViewState();
}

class _JobDetailViewState extends ConsumerState<_JobDetailView> {
  late List<Map<String, dynamic>> _notes;
  final _noteController = TextEditingController();
  final _noteComposerKey = GlobalKey();
  final _noteFocusNode = FocusNode();
  bool _isAddingNote = false;
  bool _isSavingNote = false;
  bool _isInternalNote = true;
  String _noteError = '';
  JobDestination? _activeDestination;

  @override
  void initState() {
    super.initState();
    _notes = [...widget.detail.notes];
  }

  @override
  void didUpdateWidget(covariant _JobDetailView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.detail != widget.detail) {
      _notes = [...widget.detail.notes];
    }
  }

  @override
  void dispose() {
    _noteController.dispose();
    _noteFocusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final detail = widget.detail;
    final job = detail.job;
    final recordedEvents = detail.history
        .map((entry) => entry['event']?.toString())
        .whereType<String>()
        .toSet();
    final actions = switch (job.status) {
      'scheduled' ||
      'dispatched' when !recordedEvents.contains('en_route') => ['en_route'],
      'scheduled' ||
      'dispatched' when !recordedEvents.contains('arrived') => ['arrived'],
      'scheduled' || 'dispatched' => ['start'],
      'in_progress' => ['pause', 'complete'],
      'paused' => ['resume'],
      _ => const <String>[],
    };
    final travelActions = actions
        .where((action) => action == 'en_route' || action == 'arrived')
        .toList();
    final executionActions = actions
        .where((action) => action != 'en_route' && action != 'arrived')
        .toList();
    final statusColor = AppColors.statusTone(
      context,
      job.statusPresentation.tone,
    );
    final projectId = detail.customerExperience.project?.id;
    final ticketId =
        detail.customerExperience.originTicket?.id ??
        detail.customerExperience.projectTaskTicket?.id;
    final linkedContext =
        '${projectId == null ? '' : '&projectId=${Uri.encodeComponent(projectId)}'}'
        '${ticketId == null ? '' : '&ticketId=${Uri.encodeComponent(ticketId)}'}';

    return Scaffold(
      appBar: AppBar(
        title: Text(job.workType.toUpperCase()),
        actions: [
          IconButton(
            tooltip: 'Request materials',
            onPressed: () => context.push(
              '/materials/new?workOrderId=${Uri.encodeComponent(job.id)}'
              '&workOrderLabel=${Uri.encodeComponent(job.title)}'
              '$linkedContext',
            ),
            icon: const Icon(Icons.inventory_2_outlined),
          ),
          IconButton(
            tooltip: 'Request expense',
            onPressed: () => context.push(
              '/expenses/new?workOrderId=${Uri.encodeComponent(job.id)}'
              '&workOrderLabel=${Uri.encodeComponent(job.title)}'
              '$linkedContext',
            ),
            icon: const Icon(Icons.receipt_long_outlined),
          ),
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: Center(
              child: Row(
                children: [
                  Icon(Icons.circle, size: 10, color: statusColor),
                  const SizedBox(width: 6),
                  Text(job.statusPresentation.label),
                ],
              ),
            ),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        key: const Key('add-note-action'),
        onPressed: _isAddingNote ? null : _openNoteComposer,
        icon: const Icon(Icons.note_add_outlined),
        label: const Text('Add note'),
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              job.title,
              style: Theme.of(
                context,
              ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700),
            ),
            if (!detail.customerExperience.isEmpty) ...[
              const SizedBox(height: 8),
              _ReferenceChips(detail: detail),
            ],
            const SizedBox(height: 16),
            _LocationCard(jobId: job.id, location: detail.location),
            _DestinationsCard(jobId: job.id),
            if (detail.customer != null) ...[
              const SizedBox(height: 12),
              _CustomerCard(jobId: job.id, customer: detail.customer!),
            ],
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Text(
                      'Fiber evidence',
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                    const SizedBox(height: 6),
                    const Text(
                      'View only the exact fiber features backed by immutable '
                      'observations for this job.',
                    ),
                    const SizedBox(height: 10),
                    OutlinedButton.icon(
                      key: const Key('open-fiber-evidence-map'),
                      onPressed: () => context.push(
                        '/jobs/${Uri.encodeComponent(job.id)}/fiber-evidence',
                      ),
                      icon: const Icon(Icons.layers_outlined),
                      label: const Text('Open evidence map'),
                    ),
                  ],
                ),
              ),
            ),
            if (_hasJobContext(detail)) ...[
              const SizedBox(height: 12),
              _JobContextCard(detail: detail),
            ],
            if (travelActions.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Wrap(
                    spacing: 10,
                    runSpacing: 10,
                    children: [
                      for (final action in travelActions)
                        OutlinedButton.icon(
                          key: Key('work-action-$action'),
                          onPressed: () => _runWorkAction(job.id, action),
                          icon: Icon(
                            action == 'en_route'
                                ? Icons.navigation_outlined
                                : Icons.place_outlined,
                          ),
                          label: Text(actionLabel(action)),
                        ),
                    ],
                  ),
                ),
              ),
            ],
            if (job.description != null && job.description!.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Scope of work',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      Text(job.description!),
                    ],
                  ),
                ),
              ),
            ],
            if (detail.materialRequests.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Material requests',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      for (final request in detail.materialRequests)
                        _MaterialRequestListTile(request: request),
                    ],
                  ),
                ),
              ),
            ],
            if (detail.history.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'History',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      for (final item in detail.history)
                        _HistoryTile(item: item),
                    ],
                  ),
                ),
              ),
            ],
            if (_isAddingNote) ...[
              const SizedBox(height: 12),
              Card(
                key: _noteComposerKey,
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text(
                        'Add note',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      TextField(
                        key: const Key('note-body-field'),
                        controller: _noteController,
                        focusNode: _noteFocusNode,
                        minLines: 4,
                        maxLines: 6,
                        textInputAction: TextInputAction.newline,
                        decoration: InputDecoration(
                          hintText: 'What happened on site?',
                          errorText: _noteError.isEmpty ? null : _noteError,
                        ),
                      ),
                      const SizedBox(height: 12),
                      CheckboxListTile(
                        key: const Key('internal-note-checkbox'),
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Internal note'),
                        subtitle: Text(
                          _isInternalNote
                              ? 'Visible to staff only'
                              : 'External note for customer-facing history',
                        ),
                        value: _isInternalNote,
                        onChanged: _isSavingNote
                            ? null
                            : (value) => setState(
                                () => _isInternalNote = value ?? true,
                              ),
                      ),
                      const SizedBox(height: 12),
                      OverflowBar(
                        alignment: MainAxisAlignment.end,
                        spacing: 8,
                        children: [
                          TextButton(
                            onPressed: _isSavingNote
                                ? null
                                : () => setState(() {
                                    _isAddingNote = false;
                                    _isInternalNote = true;
                                    _noteError = '';
                                    _noteFocusNode.unfocus();
                                    _noteController.clear();
                                  }),
                            child: const Text('Cancel'),
                          ),
                          FilledButton(
                            key: const Key('save-note-action'),
                            onPressed: _isSavingNote
                                ? null
                                : () => _saveInlineNote(job.id),
                            child: _isSavingNote
                                ? const SizedBox(
                                    width: 18,
                                    height: 18,
                                    child: CircularProgressIndicator(
                                      strokeWidth: 2,
                                    ),
                                  )
                                : const Text('Save'),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ],
            if (detail.materials.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Materials',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      for (final material in detail.materials)
                        Padding(
                          padding: const EdgeInsets.symmetric(vertical: 4),
                          child: Row(
                            children: [
                              Expanded(
                                child: Text(
                                  material['item_name'] as String? ?? 'Item',
                                ),
                              ),
                              Text('×${material['quantity']}'),
                            ],
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            ],
            if (_notes.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Notes',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 8),
                      for (final note in _notes)
                        Padding(
                          padding: const EdgeInsets.symmetric(vertical: 4),
                          child: _NoteTile(note: note),
                        ),
                    ],
                  ),
                ),
              ),
            ],
            const SizedBox(height: 96),
          ],
        ),
      ),
      bottomNavigationBar: executionActions.isEmpty
          ? null
          : SafeArea(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    for (final action in executionActions) ...[
                      FilledButton(
                        key: Key('work-action-$action'),
                        onPressed: () => _runWorkAction(job.id, action),
                        child: Text(actionLabel(action)),
                      ),
                      if (action != executionActions.last)
                        const SizedBox(height: 8),
                    ],
                    TextButton(
                      key: const Key('unable-action'),
                      onPressed: () =>
                          promptUnableToComplete(context, ref, job.id),
                      child: const Text("Can't complete this job"),
                    ),
                  ],
                ),
              ),
            ),
    );
  }

  void _openNoteComposer() {
    setState(() {
      _isAddingNote = true;
      _isInternalNote = true;
      _noteError = '';
    });
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (!mounted) return;
      _noteFocusNode.requestFocus();
      await Future<void>.delayed(const Duration(milliseconds: 300));
      if (!mounted) return;
      final fieldContext = _noteFocusNode.context;
      if (fieldContext != null && fieldContext.mounted) {
        await Scrollable.ensureVisible(
          fieldContext,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOutCubic,
          alignment: 0.1,
        );
      }
    });
  }

  Future<void> _runWorkAction(String jobId, String action) async {
    String? travelEventId;
    if (action == 'complete') {
      await context.push<bool>('/jobs/$jobId/complete');
    } else if (action == 'en_route') {
      final destination = await _pickDestination(jobId);
      if (destination == null) return;
      _activeDestination = destination;
      travelEventId = await ref
          .read(executionControllerProvider.notifier)
          .transition(
            jobId,
            action,
            payload: destination.toTransitionPayload(),
          );
    } else if (action == 'arrived') {
      final destination = _activeDestination ?? await _pickDestination(jobId);
      if (destination == null) return;
      _activeDestination = destination;
      travelEventId = await ref
          .read(executionControllerProvider.notifier)
          .transition(
            jobId,
            action,
            payload: destination.toTransitionPayload(),
          );
    } else {
      await ref
          .read(executionControllerProvider.notifier)
          .transition(jobId, action);
    }
    if (!mounted) return;
    ref.invalidate(jobDetailProvider(jobId));
    if (travelEventId != null) {
      final entry = await ref
          .read(syncServiceProvider)
          .outboxEntry(travelEventId);
      if (!mounted) return;
      final message = entry?.status == 'conflict'
          ? entry?.lastError ?? 'The server rejected this action.'
          : entry?.status == 'pending'
          ? '${actionLabel(action)} queued for sync'
          : '${actionLabel(action)} recorded';
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(message)));
    }
  }

  Future<JobDestination?> _pickDestination(String jobId) async {
    List<JobDestination> destinations;
    try {
      destinations = await ref
          .read(jobsRepositoryProvider)
          .fetchDestinations(jobId);
    } catch (_) {
      destinations = const [
        JobDestination(destinationType: 'customer', label: 'Customer site'),
        JobDestination(destinationType: 'other', label: 'Other location'),
      ];
    }
    if (!mounted) return null;
    return showModalBottomSheet<JobDestination>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            const Padding(
              padding: EdgeInsets.all(16),
              child: Text(
                'Select destination',
                style: TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
            for (final destination in destinations)
              ListTile(
                key: Key(
                  'destination-${destination.destinationType}-${destination.destinationId ?? destination.label}',
                ),
                leading: Icon(_destinationIcon(destination.destinationType)),
                title: Text(destination.label),
                subtitle:
                    destination.addressText == null ||
                        destination.addressText!.isEmpty
                    ? Text(destination.destinationType.replaceAll('_', ' '))
                    : Text(destination.addressText!),
                onTap: () => Navigator.of(sheetContext).pop(destination),
              ),
          ],
        ),
      ),
    );
  }

  Future<void> _saveInlineNote(String jobId) async {
    final body = _noteController.text.trim();
    if (body.isEmpty) {
      setState(() => _noteError = 'Enter a note');
      return;
    }
    setState(() {
      _isSavingNote = true;
      _noteError = '';
    });
    try {
      final clientRef = await ref
          .read(executionControllerProvider.notifier)
          .addNote(jobId, body, isInternal: _isInternalNote);
      if (!mounted) return;
      final isInternal = _isInternalNote;
      _noteController.clear();
      setState(() {
        _isAddingNote = false;
        _isSavingNote = false;
        _isInternalNote = true;
      });
      _addLocalNote(clientRef, body, isInternal: isInternal);
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Note saved')));
      unawaited(_refreshJobDetail(jobId));
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _isSavingNote = false;
        _noteError = 'Could not save note';
      });
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Could not save note')));
    }
  }

  void _addLocalNote(
    String clientRef,
    String body, {
    required bool isInternal,
  }) {
    setState(() {
      _notes = [
        {
          'id': clientRef,
          'body': body,
          'is_internal': isInternal,
          'author_name': 'You',
          'created_at': DateTime.now().toUtc().toIso8601String(),
        },
        ..._notes,
      ];
    });
  }

  Future<void> _refreshJobDetail(String jobId) async {
    try {
      final detail = await ref.read(jobsRepositoryProvider).fetchDetail(jobId);
      if (!mounted) return;
      setState(() => _notes = [...detail.notes]);
    } catch (_) {
      // The note is saved/queued; a refresh problem should not show as save failure.
    }
  }
}

IconData _destinationIcon(String type) {
  return switch (type) {
    'customer' => Icons.home_outlined,
    'cabinet' || 'fdh' => Icons.dns_outlined,
    'closure' || 'splice_closure' => Icons.hub_outlined,
    'pop' || 'olt' => Icons.router_outlined,
    _ => Icons.place_outlined,
  };
}

bool _hasJobContext(JobDetail detail) {
  return _hasText(detail.accessNotes) ||
      detail.additionalContacts.isNotEmpty ||
      detail.openTickets.isNotEmpty ||
      detail.recentVisits.isNotEmpty;
}

bool _hasText(String? value) => value != null && value.trim().isNotEmpty;

String _shortId(String value) {
  if (value.length <= 8) return value;
  return value.substring(0, 8);
}

String _formatDate(DateTime? value) {
  if (value == null) return '';
  final local = value.toLocal();
  final month = local.month.toString().padLeft(2, '0');
  final day = local.day.toString().padLeft(2, '0');
  return '${local.year}-$month-$day';
}

String? _phoneDigits(String? value) {
  if (!_hasText(value)) return null;
  final digits = value!.replaceAll(RegExp(r'\D'), '');
  return digits.isEmpty ? null : digits;
}

Uri? _phoneUri(String? value) =>
    _hasText(value) ? Uri.parse('tel:$value') : null;

Uri? _whatsAppUri(String? value) {
  final digits = _phoneDigits(value);
  return digits == null ? null : Uri.parse('https://wa.me/$digits');
}

Uri? _emailUri(String? value) {
  if (!_hasText(value)) return null;
  return Uri.parse('mailto:${Uri.encodeComponent(value!.trim())}');
}

class _ReferenceChips extends StatelessWidget {
  const _ReferenceChips({required this.detail});

  final JobDetail detail;

  @override
  Widget build(BuildContext context) {
    final lifecycle = detail.customerExperience;
    final taskTicket = lifecycle.projectTaskTicket;
    final originTicket = lifecycle.originTicket;
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        if (originTicket != null)
          Chip(
            avatar: const Icon(Icons.confirmation_number_outlined, size: 18),
            label: Text(
              'Ticket ${originTicket.number ?? _shortId(originTicket.id)} · ${originTicket.status.replaceAll('_', ' ')}',
            ),
          ),
        if (taskTicket != null && taskTicket.id != originTicket?.id)
          Chip(
            avatar: const Icon(Icons.support_agent_outlined, size: 18),
            label: Text(
              'Task ticket ${taskTicket.number ?? _shortId(taskTicket.id)} · ${taskTicket.status.replaceAll('_', ' ')}',
            ),
          ),
        if (lifecycle.project != null)
          Chip(
            avatar: const Icon(Icons.account_tree_outlined, size: 18),
            label: Text(
              'Project ${lifecycle.project!.number ?? lifecycle.project!.title} · ${lifecycle.project!.status.replaceAll('_', ' ')}',
            ),
          ),
        if (lifecycle.projectTask != null)
          Chip(
            avatar: const Icon(Icons.task_alt_outlined, size: 18),
            label: Text(
              'Task ${lifecycle.projectTask!.number ?? lifecycle.projectTask!.title} · ${lifecycle.projectTask!.status.replaceAll('_', ' ')}',
            ),
          ),
      ],
    );
  }
}

class _DestinationsCard extends ConsumerWidget {
  const _DestinationsCard({required this.jobId});

  final String jobId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final destinations = ref.watch(jobDestinationsProvider(jobId));
    return destinations.maybeWhen(
      data: (items) {
        final navigable = items
            .where((item) => item.destinationType != 'other')
            .toList();
        if (navigable.isEmpty) return const SizedBox.shrink();
        return Padding(
          padding: const EdgeInsets.only(top: 12),
          child: Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Navigation targets',
                    style: Theme.of(context).textTheme.titleSmall,
                  ),
                  const SizedBox(height: 8),
                  for (final destination in navigable)
                    _DestinationTile(destination: destination),
                ],
              ),
            ),
          ),
        );
      },
      orElse: () => const SizedBox.shrink(),
    );
  }
}

class _DestinationTile extends ConsumerWidget {
  const _DestinationTile({required this.destination});

  final JobDestination destination;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final uri = destination.mapsUri;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Icon(_destinationIcon(destination.destinationType)),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(destination.label),
                Text(
                  _hasText(destination.addressText)
                      ? destination.addressText!
                      : destination.destinationType.replaceAll('_', ' '),
                  style: Theme.of(context).textTheme.bodySmall,
                ),
                if (uri != null) ...[
                  const SizedBox(height: 6),
                  OutlinedButton.icon(
                    key: Key(
                      'navigate-destination-${destination.destinationType}',
                    ),
                    onPressed: () => ref.read(uriLauncherProvider)(uri),
                    icon: const Icon(Icons.navigation_outlined),
                    label: const Text('Navigate'),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

String _noteBody(Map<String, dynamic> note) {
  for (final key in ['body', 'text', 'comment', 'note']) {
    final value = note[key];
    if (value is String && value.trim().isNotEmpty) return value;
  }
  return '';
}

class _NoteTile extends StatelessWidget {
  const _NoteTile({required this.note});

  final Map<String, dynamic> note;

  @override
  Widget build(BuildContext context) {
    final meta = _noteMeta(note);
    final isInternal = note['is_internal'];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (isInternal is bool)
          Align(
            alignment: Alignment.centerLeft,
            child: Chip(
              visualDensity: VisualDensity.compact,
              label: Text(isInternal ? 'Internal' : 'External'),
            ),
          ),
        if (meta != null)
          Text(
            meta,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
              color: Theme.of(context).colorScheme.onSurfaceVariant,
            ),
          ),
        Text(_noteBody(note)),
      ],
    );
  }
}

class _MaterialRequestListTile extends StatelessWidget {
  const _MaterialRequestListTile({required this.request});

  final Map<String, dynamic> request;

  @override
  Widget build(BuildContext context) {
    final id = request['id']?.toString();
    final number = request['number']?.toString();
    final status = request['status']?.toString().replaceAll('_', ' ');
    final items = request['items'];
    final itemCount = items is List ? items.length : 0;
    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: const Icon(Icons.assignment_outlined),
      title: Text(
        number == null || number.isEmpty ? 'Material request' : number,
      ),
      subtitle: Text(
        [
          if (status != null && status.isNotEmpty) status,
          '$itemCount item${itemCount == 1 ? '' : 's'}',
        ].join(' · '),
      ),
      trailing: id == null ? null : const Icon(Icons.chevron_right),
      onTap: id == null ? null : () => context.push('/materials/$id'),
    );
  }
}

class _HistoryTile extends StatelessWidget {
  const _HistoryTile({required this.item});

  final Map<String, dynamic> item;

  @override
  Widget build(BuildContext context) {
    final type = item['type']?.toString() ?? 'activity';
    final title = item['title']?.toString() ?? 'Activity';
    final description = item['description']?.toString();
    final actor = item['actor_name']?.toString();
    final status = item['status']?.toString().replaceAll('_', ' ');
    final occurredAt = item['occurred_at']?.toString();
    final isInternal = item['is_internal'];
    final meta = [
      if (actor != null && actor.isNotEmpty) actor,
      if (status != null && status.isNotEmpty) status,
      if (occurredAt != null && occurredAt.isNotEmpty) occurredAt,
    ].join(' · ');

    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: Icon(_historyIcon(type)),
      title: Row(
        children: [
          Expanded(child: Text(title)),
          if (isInternal is bool)
            Padding(
              padding: const EdgeInsets.only(left: 8),
              child: Chip(
                visualDensity: VisualDensity.compact,
                label: Text(isInternal ? 'Internal' : 'External'),
              ),
            ),
        ],
      ),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (description != null && description.isNotEmpty) Text(description),
          if (meta.isNotEmpty)
            Text(
              meta,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
            ),
        ],
      ),
    );
  }
}

IconData _historyIcon(String type) {
  return switch (type) {
    'note' => Icons.sticky_note_2_outlined,
    'material_request' => Icons.assignment_outlined,
    'work_event' => Icons.timeline_outlined,
    'worklog' => Icons.timer_outlined,
    'attachment' => Icons.attach_file,
    _ => Icons.history,
  };
}

String? _noteMeta(Map<String, dynamic> note) {
  final author = _noteString(note, const [
    'author_name',
    'author',
    'created_by_name',
    'created_by',
  ]);
  final createdAt = _noteString(note, const ['created_at', 'createdAt']);
  if (author == null && createdAt == null) return null;
  if (author != null && createdAt != null) return '$author · $createdAt';
  return author ?? createdAt;
}

String? _noteString(Map<String, dynamic> note, List<String> keys) {
  for (final key in keys) {
    final value = note[key];
    if (value is String && value.trim().isNotEmpty) return value.trim();
  }
  return null;
}

/// Field outcomes for a visit that can't be completed. Keys mirror the backend
/// ``unable_to_complete`` reasons; labels are tech-facing.
const List<({String key, String label})> kUnableReasons = [
  (key: 'customer_absent', label: 'Customer not home'),
  (key: 'no_access', label: 'Could not access site'),
  (key: 'site_not_ready', label: 'Site not ready'),
  (key: 'needs_parts', label: 'Missing parts/materials'),
  (key: 'unsafe', label: 'Unsafe conditions'),
  (key: 'other', label: 'Other'),
];

/// Ask why the job can't be completed, then record the failed visit (which
/// cancels the job server-side with the chosen reason).
Future<void> promptUnableToComplete(
  BuildContext context,
  WidgetRef ref,
  String jobId,
) async {
  final reason = await showModalBottomSheet<String>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Padding(
            padding: EdgeInsets.all(16),
            child: Text(
              "Why can't this job be completed?",
              style: TextStyle(fontWeight: FontWeight.w600),
            ),
          ),
          for (final reasonOption in kUnableReasons)
            ListTile(
              key: Key('reason-${reasonOption.key}'),
              title: Text(reasonOption.label),
              onTap: () => Navigator.of(sheetContext).pop(reasonOption.key),
            ),
        ],
      ),
    ),
  );
  if (reason == null) return;
  if (!context.mounted) return;
  await ref
      .read(executionControllerProvider.notifier)
      .unableToComplete(jobId, reason: reason);
  if (!context.mounted) return;
  ref.invalidate(jobDetailProvider(jobId));
  Navigator.of(context).maybePop();
}

class _LocationCard extends ConsumerWidget {
  const _LocationCard({required this.jobId, required this.location});

  final String jobId;
  final JobLocation location;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final uri = location.mapsUri;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.place_outlined, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(location.addressText ?? 'No address on file'),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 10,
              runSpacing: 10,
              children: [
                if (uri != null)
                  OutlinedButton.icon(
                    key: const Key('navigate-button'),
                    onPressed: () => ref.read(uriLauncherProvider)(uri),
                    icon: const Icon(Icons.navigation_outlined),
                    label: const Text('Navigate'),
                  ),
                OutlinedButton.icon(
                  key: const Key('edit-location-button'),
                  onPressed: () async {
                    final changed = await Navigator.of(context).push<bool>(
                      MaterialPageRoute(
                        builder: (_) => LocationPinScreen(
                          jobId: jobId,
                          initialLocation: location,
                        ),
                      ),
                    );
                    if (context.mounted && changed == true) {
                      ref.invalidate(jobDetailProvider(jobId));
                    }
                  },
                  icon: const Icon(Icons.push_pin_outlined),
                  label: Text(
                    location.hasCoordinates ? 'Edit pin' : 'Pin location',
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _JobContextCard extends ConsumerWidget {
  const _JobContextCard({required this.detail});

  final JobDetail detail;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Job context', style: Theme.of(context).textTheme.titleSmall),
            if (_hasText(detail.accessNotes)) ...[
              const SizedBox(height: 12),
              _ContextBlock(
                icon: Icons.vpn_key_outlined,
                title: 'Access notes',
                body: detail.accessNotes!.trim(),
              ),
            ],
            if (detail.additionalContacts.isNotEmpty) ...[
              const SizedBox(height: 12),
              Text(
                'Site contacts',
                style: Theme.of(context).textTheme.labelLarge,
              ),
              const SizedBox(height: 4),
              for (final contact in detail.additionalContacts)
                _SiteContactTile(contact: contact),
            ],
            if (detail.openTickets.isNotEmpty) ...[
              const SizedBox(height: 12),
              Text(
                'Open tickets',
                style: Theme.of(context).textTheme.labelLarge,
              ),
              const SizedBox(height: 4),
              for (final ticket in detail.openTickets)
                _OpenTicketTile(ticket: ticket),
            ],
            if (detail.recentVisits.isNotEmpty) ...[
              const SizedBox(height: 12),
              Text(
                'Recent visits',
                style: Theme.of(context).textTheme.labelLarge,
              ),
              const SizedBox(height: 4),
              for (final visit in detail.recentVisits)
                _RecentVisitTile(visit: visit),
            ],
          ],
        ),
      ),
    );
  }
}

class _ContextBlock extends StatelessWidget {
  const _ContextBlock({
    required this.icon,
    required this.title,
    required this.body,
  });

  final IconData icon;
  final String title;
  final String body;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, size: 20),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title, style: Theme.of(context).textTheme.labelLarge),
              const SizedBox(height: 2),
              Text(body),
            ],
          ),
        ),
      ],
    );
  }
}

class _SiteContactTile extends ConsumerWidget {
  const _SiteContactTile({required this.contact});

  final JobSiteContact contact;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final phoneUri = _phoneUri(contact.phone);
    final whatsAppUri = _whatsAppUri(contact.phone);
    final emailUri = _emailUri(contact.email);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.person_outline),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(_hasText(contact.name) ? contact.name! : 'Site contact'),
                Text(
                  [
                    if (_hasText(contact.relationship)) contact.relationship,
                    if (_hasText(contact.phone)) contact.phone,
                    if (_hasText(contact.email)) contact.email,
                  ].join(' · '),
                  style: Theme.of(context).textTheme.bodySmall,
                ),
                const SizedBox(height: 6),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    if (phoneUri != null)
                      OutlinedButton.icon(
                        onPressed: () =>
                            ref.read(uriLauncherProvider)(phoneUri),
                        icon: const Icon(Icons.call_outlined),
                        label: const Text('Call'),
                      ),
                    if (whatsAppUri != null)
                      OutlinedButton.icon(
                        onPressed: () =>
                            ref.read(uriLauncherProvider)(whatsAppUri),
                        icon: const Icon(Icons.chat_outlined),
                        label: const Text('Chat'),
                      ),
                    if (emailUri != null)
                      OutlinedButton.icon(
                        onPressed: () =>
                            ref.read(uriLauncherProvider)(emailUri),
                        icon: const Icon(Icons.mail_outline),
                        label: const Text('Email'),
                      ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _OpenTicketTile extends StatelessWidget {
  const _OpenTicketTile({required this.ticket});

  final JobOpenTicketItem ticket;

  @override
  Widget build(BuildContext context) {
    final meta = [
      if (_hasText(ticket.ref)) ticket.ref,
      if (_hasText(ticket.status)) ticket.status!.replaceAll('_', ' '),
    ].join(' · ');
    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: const Icon(Icons.report_problem_outlined),
      title: Text(_hasText(ticket.subject) ? ticket.subject! : 'Open ticket'),
      subtitle: meta.isEmpty ? null : Text(meta),
    );
  }
}

class _RecentVisitTile extends StatelessWidget {
  const _RecentVisitTile({required this.visit});

  final JobVisitHistoryItem visit;

  @override
  Widget build(BuildContext context) {
    final completedAt = _formatDate(visit.completedAt);
    final meta = [
      if (_hasText(visit.workType)) visit.workType!.replaceAll('_', ' '),
      if (_hasText(visit.status)) visit.status!.replaceAll('_', ' '),
      if (completedAt.isNotEmpty) completedAt,
    ].join(' · ');
    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: const Icon(Icons.history_outlined),
      title: Text(visit.title),
      subtitle: meta.isEmpty ? null : Text(meta),
    );
  }
}

class _CustomerCard extends ConsumerWidget {
  const _CustomerCard({required this.jobId, required this.customer});

  final String jobId;
  final JobCustomer customer;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final phoneUri = _phoneUri(customer.phone);
    final emailUri = _emailUri(customer.email);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                CircleAvatar(
                  child: Text(
                    (customer.name ?? '?').substring(0, 1).toUpperCase(),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        customer.name ?? 'Customer',
                        style: Theme.of(context).textTheme.titleSmall?.copyWith(
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      if (customer.servicePlan != null)
                        Text(
                          customer.servicePlan!,
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      if (_hasText(customer.accountNumber) ||
                          _hasText(customer.status))
                        Text(
                          [
                            if (_hasText(customer.accountNumber))
                              customer.accountNumber,
                            if (_hasText(customer.status))
                              customer.status!.replaceAll('_', ' '),
                          ].join(' · '),
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      if (_hasText(customer.addressText))
                        Text(
                          customer.addressText!,
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                    ],
                  ),
                ),
              ],
            ),
            if (phoneUri != null || emailUri != null || jobId.isNotEmpty) ...[
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  if (phoneUri != null)
                    OutlinedButton.icon(
                      key: const Key('call-button'),
                      onPressed: () => ref.read(uriLauncherProvider)(phoneUri),
                      icon: const Icon(Icons.call_outlined),
                      label: const Text('Call'),
                    ),
                  OutlinedButton.icon(
                    key: const Key('field-chat-button'),
                    onPressed: () => context.push('/jobs/$jobId/chat'),
                    icon: const Icon(Icons.forum_outlined),
                    label: const Text('Chat'),
                  ),
                  if (emailUri != null)
                    OutlinedButton.icon(
                      key: const Key('email-customer-button'),
                      onPressed: () => ref.read(uriLauncherProvider)(emailUri),
                      icon: const Icon(Icons.mail_outline),
                      label: const Text('Email'),
                    ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}
