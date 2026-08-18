import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';
import 'package:uuid/uuid.dart';

import '../../core/offline/draft_store.dart';
import '../execution/execution_controller.dart';
import 'material_models.dart';
import 'materials_providers.dart';

const _priorities = ['low', 'medium', 'high', 'urgent'];
const _statusOrder = ['draft', 'submitted', 'approved', 'issued', 'fulfilled'];

class MaterialsScreen extends ConsumerWidget {
  const MaterialsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final requests = ref.watch(materialRequestsProvider);
    final drafts = ref.watch(materialRequestDraftsProvider);
    final inventory = ref.watch(inventorySearchProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Materials'),
        actions: [
          IconButton(
            tooltip: 'New request',
            onPressed: () => context.push('/materials/new'),
            icon: const Icon(Icons.add),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(materialRequestsProvider);
          ref.invalidate(inventorySearchProvider);
        },
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    'Requests',
                    style: Theme.of(context).textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                FilledButton.icon(
                  onPressed: () => context.push('/materials/new'),
                  icon: const Icon(Icons.add),
                  label: const Text('Request'),
                ),
              ],
            ),
            const SizedBox(height: 8),
            drafts.when(
              data: (items) => Column(
                children: [
                  for (final draft in items)
                    Card(
                      child: ListTile(
                        leading: const Icon(Icons.edit_note_outlined),
                        title: const Text('Saved material draft'),
                        subtitle: Text(
                          draft.payload['work_order_id']
                                      ?.toString()
                                      .isNotEmpty ==
                                  true
                              ? 'Work order ${draft.payload['work_order_id']}'
                              : 'Tap to continue editing',
                        ),
                        trailing: const Icon(Icons.chevron_right),
                        onTap: () => context.push('/materials/new'),
                      ),
                    ),
                ],
              ),
              loading: () => const SizedBox.shrink(),
              error: (_, _) => const SizedBox.shrink(),
            ),
            requests.when(
              data: (items) {
                if (items.isEmpty) {
                  return const Padding(
                    padding: EdgeInsets.symmetric(vertical: 48),
                    child: Center(child: Text('No material requests yet')),
                  );
                }
                return Column(
                  children: [
                    for (final request in items)
                      _MaterialRequestTile(request: request),
                  ],
                );
              },
              loading: () => const Padding(
                padding: EdgeInsets.only(top: 48),
                child: Center(child: CircularProgressIndicator()),
              ),
              error: (_, _) => const Padding(
                padding: EdgeInsets.only(top: 48),
                child: Center(child: Text('Could not load material requests')),
              ),
            ),
            const SizedBox(height: 24),
            Text(
              'Inventory',
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 8),
            TextField(
              key: const Key('inventory-search'),
              decoration: const InputDecoration(
                labelText: 'Search inventory',
                prefixIcon: Icon(Icons.search),
              ),
              onChanged: (value) =>
                  ref.read(inventorySearchQueryProvider.notifier).state = value,
            ),
            const SizedBox(height: 12),
            inventory.when(
              data: (items) => _InventoryPreview(items: items),
              loading: () => const LinearProgressIndicator(),
              error: (_, _) =>
                  const Text('Inventory is not available right now'),
            ),
          ],
        ),
      ),
    );
  }
}

class _InventoryPreview extends StatelessWidget {
  const _InventoryPreview({required this.items});

  final List<InventoryItem> items;

  @override
  Widget build(BuildContext context) {
    if (items.isEmpty) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 12),
        child: Text('No inventory items found'),
      );
    }
    return Column(
      children: [
        for (final item in items.take(5))
          ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.inventory_2_outlined),
            title: Text(item.name),
            subtitle: _InventoryAvailabilityText(
              item: item,
              fallback: [item.sku, item.unit].whereType<String>().join(' · '),
            ),
            trailing: item.availableQuantity == null
                ? null
                : Text('${item.availableQuantity} available'),
          ),
      ],
    );
  }
}

class _MaterialRequestTile extends StatelessWidget {
  const _MaterialRequestTile({required this.request});

  final MaterialRequest request;

  @override
  Widget build(BuildContext context) {
    final isLocal =
        request.status == 'queued' || request.status == 'sync failed';
    final date = request.createdAt == null
        ? null
        : DateFormat('d MMM, HH:mm').format(request.createdAt!.toLocal());
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: Icon(
          Icons.assignment_outlined,
          color: _materialStatusColor(context, request.status),
        ),
        title: Text(request.number ?? 'Request ${request.id}'),
        subtitle: Wrap(
          spacing: 6,
          runSpacing: 4,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            _MaterialStatusChip(status: request.status),
            if (request.supportStatus != null)
              Text('ERP ${request.supportStatus}'),
            if (request.priority != null) Text(request.priority!),
            if (date != null) Text(date),
          ],
        ),
        trailing: isLocal ? null : const Icon(Icons.chevron_right),
        onTap: isLocal ? null : () => context.push('/materials/${request.id}'),
      ),
    );
  }
}

class MaterialRequestDetailScreen extends ConsumerWidget {
  const MaterialRequestDetailScreen({super.key, required this.id});

  final String id;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final request = ref.watch(materialRequestProvider(id));
    return Scaffold(
      appBar: AppBar(title: const Text('Material request')),
      body: request.when(
        data: (data) => ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text(
              data.displayNumber,
              style: Theme.of(
                context,
              ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                _MaterialStatusChip(status: data.status),
                if (data.priority != null) Chip(label: Text(data.priority!)),
              ],
            ),
            const SizedBox(height: 16),
            _MaterialStatusTimeline(request: data),
            if (data.sourceLocationLabel != null ||
                data.destinationLocationLabel != null) ...[
              const SizedBox(height: 16),
              _MaterialLocationSummary(request: data),
            ],
            if (data.notes != null && data.notes!.isNotEmpty) ...[
              const SizedBox(height: 16),
              Text(data.notes!),
            ],
            if (data.approvalNotes != null ||
                data.rejectionReason != null ||
                data.issueNotes != null) ...[
              const SizedBox(height: 16),
              _MaterialStatusNotes(request: data),
            ],
            const SizedBox(height: 24),
            Text('Items', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            if (data.items.isEmpty)
              const Text('No items on this request')
            else
              for (final item in data.items)
                _MaterialRequestItemTile(item: item),
          ],
        ),
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) =>
            const Center(child: Text('Could not load this request')),
      ),
    );
  }
}

class _MaterialStatusChip extends StatelessWidget {
  const _MaterialStatusChip({required this.status});

  final String status;

  @override
  Widget build(BuildContext context) {
    final color = _materialStatusColor(context, status);
    return Chip(
      visualDensity: VisualDensity.compact,
      label: Text(_statusLabel(status)),
      backgroundColor: color.withValues(alpha: 0.16),
      side: BorderSide(color: color.withValues(alpha: 0.4)),
    );
  }
}

class _MaterialStatusTimeline extends StatelessWidget {
  const _MaterialStatusTimeline({required this.request});

  final MaterialRequest request;

  @override
  Widget build(BuildContext context) {
    final steps = _timelineSteps(request);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Status flow', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        for (final step in steps)
          _TimelineRow(
            label: step.label,
            date: step.date,
            active: step.active,
            complete: step.complete,
          ),
      ],
    );
  }
}

class _TimelineRow extends StatelessWidget {
  const _TimelineRow({
    required this.label,
    required this.active,
    required this.complete,
    this.date,
  });

  final String label;
  final DateTime? date;
  final bool active;
  final bool complete;

  @override
  Widget build(BuildContext context) {
    final color = active || complete
        ? Theme.of(context).colorScheme.primary
        : Theme.of(context).disabledColor;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(
        children: [
          Icon(
            complete ? Icons.check_circle : Icons.radio_button_unchecked,
            size: 18,
            color: color,
          ),
          const SizedBox(width: 8),
          Expanded(child: Text(label)),
          if (date != null)
            Text(
              DateFormat('d MMM, HH:mm').format(date!.toLocal()),
              style: Theme.of(context).textTheme.bodySmall,
            ),
        ],
      ),
    );
  }
}

class _MaterialLocationSummary extends StatelessWidget {
  const _MaterialLocationSummary({required this.request});

  final MaterialRequest request;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Locations', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        if (request.sourceLocationLabel != null)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.warehouse_outlined),
            title: const Text('Source'),
            subtitle: Text(request.sourceLocationLabel!),
          ),
        if (request.destinationLocationLabel != null)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.local_shipping_outlined),
            title: const Text('Destination'),
            subtitle: Text(request.destinationLocationLabel!),
          ),
      ],
    );
  }
}

class _MaterialStatusNotes extends StatelessWidget {
  const _MaterialStatusNotes({required this.request});

  final MaterialRequest request;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Status notes', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        if (request.approvalNotes != null)
          _StatusNote(label: 'Approval', body: request.approvalNotes!),
        if (request.rejectionReason != null)
          _StatusNote(label: 'Rejection', body: request.rejectionReason!),
        if (request.issueNotes != null)
          _StatusNote(label: 'Issue', body: request.issueNotes!),
      ],
    );
  }
}

class _StatusNote extends StatelessWidget {
  const _StatusNote({required this.label, required this.body});

  final String label;
  final String body;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: InputDecorator(
        decoration: InputDecoration(labelText: label),
        child: Text(body),
      ),
    );
  }
}

class _MaterialRequestItemTile extends StatelessWidget {
  const _MaterialRequestItemTile({required this.item});

  final MaterialRequestItem item;

  @override
  Widget build(BuildContext context) {
    final issued = item.issuedQuantity ?? item.fulfilledQuantity;
    final progress = issued == null ? null : '$issued/${item.quantity} issued';
    return ListTile(
      contentPadding: EdgeInsets.zero,
      title: Text(item.itemName ?? item.itemId),
      subtitle: Text(
        [
          if (item.approvedQuantity != null)
            '${item.approvedQuantity}/${item.quantity} approved',
          ?progress,
          if (item.notes != null && item.notes!.isNotEmpty) item.notes!,
        ].join(' · '),
      ),
      trailing: Text('x${item.quantity}'),
    );
  }
}

class NewMaterialRequestScreen extends ConsumerStatefulWidget {
  const NewMaterialRequestScreen({
    super.key,
    this.initialWorkOrderId,
    this.initialWorkOrderLabel,
    this.initialProjectId,
    this.initialTicketId,
  });

  final String? initialWorkOrderId;
  final String? initialWorkOrderLabel;
  final String? initialProjectId;
  final String? initialTicketId;

  @override
  ConsumerState<NewMaterialRequestScreen> createState() =>
      _NewMaterialRequestScreenState();
}

class _NewMaterialRequestScreenState
    extends ConsumerState<NewMaterialRequestScreen> {
  final _notes = TextEditingController();
  final _workOrderId = TextEditingController();
  final _projectId = TextEditingController();
  final _ticketId = TextEditingController();
  final _itemSearch = TextEditingController();
  final _quantity = TextEditingController(text: '1');
  final _itemNotes = TextEditingController();
  String _priority = 'medium';
  String? _sourceLocationId;
  String? _destinationLocationId;
  InventoryItem? _selectedItem;
  final _items = <MaterialRequestItemDraft>[];
  bool _saving = false;
  String _submitError = '';

  @override
  void initState() {
    super.initState();
    _workOrderId.text = widget.initialWorkOrderId ?? '';
    _projectId.text = widget.initialProjectId ?? '';
    _ticketId.text = widget.initialTicketId ?? '';
    Future.microtask(_loadDraft);
  }

  @override
  void dispose() {
    _notes.dispose();
    _workOrderId.dispose();
    _projectId.dispose();
    _ticketId.dispose();
    _itemSearch.dispose();
    _quantity.dispose();
    _itemNotes.dispose();
    super.dispose();
  }

  void _addItem() {
    final selected = _selectedItem;
    final quantity = int.tryParse(_quantity.text.trim()) ?? 0;
    if (selected == null || quantity < 1) return;
    final available = selected.availableQuantity;
    if (available != null && quantity > available) return;
    setState(() {
      _items.add(
        MaterialRequestItemDraft(
          item: selected,
          quantity: quantity,
          notes: _itemNotes.text,
        ),
      );
      _selectedItem = null;
      _itemSearch.clear();
      _quantity.text = '1';
      _itemNotes.clear();
    });
  }

  Future<void> _loadDraft() async {
    final draft = await ref
        .read(draftStoreProvider)
        .load(materialRequestDraftId);
    if (!mounted || draft == null) return;
    setState(() {
      _priority = draft['priority'] as String? ?? _priority;
      _sourceLocationId = draft['source_location_id'] as String?;
      _destinationLocationId = draft['destination_location_id'] as String?;
      _workOrderId.text =
          widget.initialWorkOrderId ?? draft['work_order_id'] as String? ?? '';
      _projectId.text =
          widget.initialProjectId ?? draft['project_id'] as String? ?? '';
      _ticketId.text =
          widget.initialTicketId ?? draft['ticket_id'] as String? ?? '';
      _notes.text = draft['notes'] as String? ?? '';
      _items
        ..clear()
        ..addAll(_materialDraftItems(draft['items']));
    });
    ref.read(inventorySourceLocationProvider.notifier).state =
        _sourceLocationId;
  }

  Future<void> _saveDraft() async {
    await ref
        .read(draftStoreProvider)
        .save(
          id: materialRequestDraftId,
          type: 'material_request',
          payload: {
            'priority': _priority,
            'source_location_id': _sourceLocationId,
            'destination_location_id': _destinationLocationId,
            'work_order_id': _workOrderId.text,
            'project_id': _projectId.text,
            'ticket_id': _ticketId.text,
            'notes': _notes.text,
            'items': _items.map(_materialDraftItemJson).toList(),
          },
        );
    ref.invalidate(materialRequestDraftsProvider);
    if (!mounted) return;
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(const SnackBar(content: Text('Draft saved')));
  }

  Future<void> _submit(List<InventoryLocation> locations) async {
    if (_items.isEmpty || _saving) return;
    if (_workOrderId.text.trim().isEmpty &&
        _projectId.text.trim().isEmpty &&
        _ticketId.text.trim().isEmpty) {
      setState(() {
        _submitError =
            'Open this from a job, or enter a ticket/project/work order ID.';
      });
      return;
    }
    final sourceLocation = locations
        .where((location) => location.id == _sourceLocationId)
        .firstOrNull;
    final sourceWarehouseCode = sourceLocation?.code?.trim();
    if (sourceWarehouseCode == null || sourceWarehouseCode.isEmpty) {
      setState(() => _submitError = 'Select a source warehouse with a code.');
      return;
    }
    final clientRef = const Uuid().v4();
    final payload = buildMaterialRequestPayload(
      priority: _priority,
      clientRef: clientRef,
      notes: _notes.text,
      workOrderId: _workOrderId.text,
      projectId: _projectId.text,
      ticketId: _ticketId.text,
      sourceLocationId: _sourceLocationId,
      sourceWarehouseCode: sourceWarehouseCode,
      destinationLocationId: _destinationLocationId,
      items: _items,
    );
    setState(() => _saving = true);
    try {
      final request = await ref
          .read(materialsRepositoryProvider)
          .createRequest(
            priority: _priority,
            clientRef: clientRef,
            notes: _notes.text,
            workOrderId: _workOrderId.text,
            projectId: _projectId.text,
            ticketId: _ticketId.text,
            sourceLocationId: _sourceLocationId,
            sourceWarehouseCode: sourceWarehouseCode,
            destinationLocationId: _destinationLocationId,
            items: _items,
          );
      ref.invalidate(materialRequestsProvider);
      try {
        await ref.read(materialRequestsProvider.future);
      } catch (_) {
        // The request was created; the list can still be refreshed manually.
      }
      await ref.read(draftStoreProvider).delete(materialRequestDraftId);
      ref.invalidate(materialRequestDraftsProvider);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('${request.displayNumber} submitted')),
        );
        context.go('/materials');
      }
    } on DioException catch (error) {
      if (!mounted) return;
      if (error.response == null) {
        await ref
            .read(syncServiceProvider)
            .enqueue(
              kind: 'material_request',
              clientRef: clientRef,
              payload: payload,
            );
        await ref.read(draftStoreProvider).delete(materialRequestDraftId);
        ref.invalidate(materialRequestDraftsProvider);
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Material request queued for sync')),
        );
        context.go('/materials');
        return;
      }
      final message = _materialSubmitError(error);
      setState(() => _submitError = message);
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(message)));
    } catch (_) {
      if (!mounted) return;
      const message = 'Could not submit material request';
      setState(() => _submitError = message);
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text(message)));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final inventory = ref.watch(inventorySearchProvider);
    final locations = ref.watch(inventoryLocationsProvider);
    final selectedAvailable = _selectedItem?.availableQuantity;
    final requestedQuantity = int.tryParse(_quantity.text.trim()) ?? 0;
    final quantityExceedsStock =
        selectedAvailable != null && requestedQuantity > selectedAvailable;
    return Scaffold(
      appBar: AppBar(title: const Text('New material request')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          DropdownButtonFormField<String>(
            initialValue: _priority,
            decoration: const InputDecoration(labelText: 'Priority'),
            items: [
              for (final priority in _priorities)
                DropdownMenuItem(value: priority, child: Text(priority)),
            ],
            onChanged: (value) =>
                setState(() => _priority = value ?? _priority),
          ),
          const SizedBox(height: 12),
          locations.when(
            data: (items) => _LocationSelectors(
              locations: items,
              sourceLocationId: _sourceLocationId,
              destinationLocationId: _destinationLocationId,
              onSourceChanged: (value) {
                setState(() {
                  _sourceLocationId = value;
                  _selectedItem = null;
                  _itemSearch.clear();
                });
                ref.read(inventorySourceLocationProvider.notifier).state =
                    value;
              },
              onDestinationChanged: (value) =>
                  setState(() => _destinationLocationId = value),
            ),
            loading: () => const LinearProgressIndicator(),
            error: (_, _) =>
                const Text('Inventory locations are not available'),
          ),
          const SizedBox(height: 12),
          if (widget.initialWorkOrderId != null &&
              widget.initialWorkOrderLabel != null) ...[
            TextFormField(
              initialValue: widget.initialWorkOrderLabel,
              readOnly: true,
              decoration: const InputDecoration(labelText: 'Linked work order'),
            ),
          ] else ...[
            TextField(
              controller: _workOrderId,
              decoration: const InputDecoration(labelText: 'Work order ID'),
            ),
          ],
          const SizedBox(height: 12),
          TextField(
            controller: _projectId,
            decoration: const InputDecoration(labelText: 'Project ID'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _ticketId,
            decoration: const InputDecoration(labelText: 'Ticket ID'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _notes,
            decoration: const InputDecoration(labelText: 'Notes'),
            maxLines: 3,
          ),
          const SizedBox(height: 24),
          Text('Add items', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          TextField(
            controller: _itemSearch,
            decoration: const InputDecoration(
              labelText: 'Search inventory',
              prefixIcon: Icon(Icons.search),
            ),
            onChanged: (value) {
              setState(() => _selectedItem = null);
              ref.read(inventorySearchQueryProvider.notifier).state = value;
            },
          ),
          const SizedBox(height: 8),
          inventory.when(
            data: (items) => _InventorySuggestions(
              items: items,
              selectedItem: _selectedItem,
              onSelected: (item) => setState(() {
                _selectedItem = item;
                _itemSearch.text = item.displayName;
              }),
            ),
            loading: () => const LinearProgressIndicator(),
            error: (_, _) => const Text('Inventory search failed'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _quantity,
            keyboardType: TextInputType.number,
            onChanged: (_) => setState(() {}),
            decoration: InputDecoration(
              labelText: 'Quantity',
              helperText: selectedAvailable == null
                  ? null
                  : '$selectedAvailable available at selected source',
              errorText: quantityExceedsStock
                  ? 'Quantity is more than available stock'
                  : null,
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _itemNotes,
            decoration: const InputDecoration(labelText: 'Item notes'),
          ),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            onPressed: quantityExceedsStock ? null : _addItem,
            icon: const Icon(Icons.add),
            label: const Text('Add item'),
          ),
          const SizedBox(height: 16),
          for (final item in _items)
            ListTile(
              contentPadding: EdgeInsets.zero,
              title: Text(item.item.name),
              subtitle: Text(
                [
                  if (item.item.availableQuantity != null)
                    '${item.item.availableQuantity} available at source',
                  if (item.notes != null && item.notes!.isNotEmpty) item.notes!,
                ].join(' · '),
              ),
              trailing: Text('x${item.quantity}'),
            ),
          if (_submitError.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text(
              _submitError,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
          const SizedBox(height: 96),
        ],
      ),
      bottomNavigationBar: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              FilledButton(
                onPressed: _items.isEmpty || _saving
                    ? null
                    : () => _submit(locations.valueOrNull ?? const []),
                child: Text(_saving ? 'Submitting...' : 'Submit request'),
              ),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                onPressed: _saving ? null : _saveDraft,
                icon: const Icon(Icons.save_outlined),
                label: const Text('Save draft'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

String _materialSubmitError(DioException error) {
  final data = error.response?.data;
  if (data is Map) {
    final detail = data['detail'];
    if (detail is String && detail.trim().isNotEmpty) return detail.trim();
    if (detail is List && detail.isNotEmpty) {
      return detail
          .map((item) {
            if (item is Map) {
              final location = item['loc'];
              final message = item['msg'];
              final locationText = location is List ? location.join('.') : null;
              if (message is String && locationText != null) {
                return '$locationText: $message';
              }
              if (message is String) return message;
            }
            return item.toString();
          })
          .join('\n');
    }
  }
  return 'Could not submit material request';
}

class _LocationSelectors extends StatelessWidget {
  const _LocationSelectors({
    required this.locations,
    required this.sourceLocationId,
    required this.destinationLocationId,
    required this.onSourceChanged,
    required this.onDestinationChanged,
  });

  final List<InventoryLocation> locations;
  final String? sourceLocationId;
  final String? destinationLocationId;
  final ValueChanged<String?> onSourceChanged;
  final ValueChanged<String?> onDestinationChanged;

  @override
  Widget build(BuildContext context) {
    if (locations.isEmpty) {
      return const Text('No inventory locations available');
    }
    final sourceValue =
        locations.any((location) => location.id == sourceLocationId)
        ? sourceLocationId
        : null;
    final destinationValue =
        locations.any((location) => location.id == destinationLocationId)
        ? destinationLocationId
        : null;
    return Column(
      children: [
        DropdownButtonFormField<String?>(
          key: const Key('source-location'),
          initialValue: sourceValue,
          decoration: const InputDecoration(labelText: 'Source location'),
          items: [
            const DropdownMenuItem(value: null, child: Text('Any location')),
            for (final location in locations)
              DropdownMenuItem(
                value: location.id,
                child: Text(_locationLabel(location)),
              ),
          ],
          onChanged: onSourceChanged,
        ),
        const SizedBox(height: 12),
        DropdownButtonFormField<String?>(
          key: const Key('destination-location'),
          initialValue: destinationValue,
          decoration: const InputDecoration(labelText: 'Destination location'),
          items: [
            const DropdownMenuItem(value: null, child: Text('Not selected')),
            for (final location in locations)
              DropdownMenuItem(
                value: location.id,
                child: Text(_locationLabel(location)),
              ),
          ],
          onChanged: onDestinationChanged,
        ),
      ],
    );
  }
}

class _InventoryItemLabel extends StatelessWidget {
  const _InventoryItemLabel({required this.item});

  final InventoryItem item;

  @override
  Widget build(BuildContext context) {
    return Text(item.displayName, maxLines: 1, overflow: TextOverflow.ellipsis);
  }
}

class _InventoryAvailabilityText extends StatelessWidget {
  const _InventoryAvailabilityText({required this.item, this.fallback});

  final InventoryItem item;
  final String? fallback;

  @override
  Widget build(BuildContext context) {
    if (item.stockByLocation.isNotEmpty) {
      return Text(
        item.stockByLocation
            .take(3)
            .map(
              (stock) => '${stock.displayLocation}: ${stock.availableQuantity}',
            )
            .join(' · '),
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
      );
    }
    if (item.availableQuantity != null) {
      return Text('${item.availableQuantity} available');
    }
    final text = fallback;
    if (text == null || text.isEmpty) return const SizedBox.shrink();
    return Text(text);
  }
}

String _locationLabel(InventoryLocation location) {
  final code = location.code;
  return code == null || code.isEmpty
      ? location.name
      : '${location.name} ($code)';
}

Map<String, dynamic> _materialDraftItemJson(MaterialRequestItemDraft draft) => {
  'item': _inventoryItemJson(draft.item),
  'quantity': draft.quantity,
  'notes': draft.notes,
};

List<MaterialRequestItemDraft> _materialDraftItems(Object? raw) {
  if (raw is! List) return const [];
  return raw.whereType<Map>().map((item) {
    final data = item.cast<String, dynamic>();
    final inventory = (data['item'] as Map?)?.cast<String, dynamic>();
    return MaterialRequestItemDraft(
      item: InventoryItem.fromJson(
        inventory ?? const {'id': '', 'name': 'Item'},
      ),
      quantity: data['quantity'] is int
          ? data['quantity'] as int
          : int.tryParse('${data['quantity']}') ?? 1,
      notes: data['notes'] as String?,
    );
  }).toList();
}

Map<String, dynamic> _inventoryItemJson(InventoryItem item) => {
  'id': item.id,
  'name': item.name,
  'sku': item.sku,
  'unit': item.unit,
  'unit_price': item.unitPrice,
  'currency': item.currency,
  'available_quantity': item.availableQuantity,
};

String _statusLabel(String status) => status.replaceAll('_', ' ');

Color _materialStatusColor(BuildContext context, String status) {
  final scheme = Theme.of(context).colorScheme;
  return switch (status) {
    'approved' => Colors.green.shade700,
    'issued' => Colors.blue.shade700,
    'fulfilled' || 'completed' => Colors.teal.shade700,
    'rejected' || 'cancelled' => scheme.error,
    'submitted' || 'pending_approval' => Colors.orange.shade800,
    _ => scheme.outline,
  };
}

List<_StatusStep> _timelineSteps(MaterialRequest request) {
  if (request.status == 'rejected') {
    return [
      _StatusStep(
        label: 'Submitted',
        date: request.submittedAt ?? request.createdAt,
        complete: true,
        active: false,
      ),
      _StatusStep(
        label: 'Rejected',
        date: request.rejectedAt,
        complete: true,
        active: true,
      ),
    ];
  }

  final currentIndex = _statusOrder.indexOf(request.status);
  final activeIndex = currentIndex < 0 ? 0 : currentIndex;
  return [
    _StatusStep(
      label: 'Draft',
      date: request.createdAt,
      complete: activeIndex >= 0,
      active: activeIndex == 0,
    ),
    _StatusStep(
      label: 'Submitted',
      date: request.submittedAt,
      complete: activeIndex >= 1,
      active: activeIndex == 1,
    ),
    _StatusStep(
      label: 'Approved',
      date: request.approvedAt,
      complete: activeIndex >= 2,
      active: activeIndex == 2,
    ),
    _StatusStep(
      label: 'Issued',
      date: request.issuedAt,
      complete: activeIndex >= 3,
      active: activeIndex == 3,
    ),
    _StatusStep(
      label: 'Fulfilled',
      date: request.fulfilledAt,
      complete: activeIndex >= 4,
      active: activeIndex == 4,
    ),
  ];
}

class _StatusStep {
  const _StatusStep({
    required this.label,
    required this.complete,
    required this.active,
    this.date,
  });

  final String label;
  final DateTime? date;
  final bool complete;
  final bool active;
}

class _InventorySuggestions extends StatelessWidget {
  const _InventorySuggestions({
    required this.items,
    required this.selectedItem,
    required this.onSelected,
  });

  final List<InventoryItem> items;
  final InventoryItem? selectedItem;
  final ValueChanged<InventoryItem> onSelected;

  @override
  Widget build(BuildContext context) {
    final selected = selectedItem;
    if (selected != null) {
      return InputDecorator(
        decoration: const InputDecoration(labelText: 'Selected item'),
        child: Row(
          children: [
            Expanded(child: _InventoryItemLabel(item: selected)),
            const SizedBox(width: 8),
            const Icon(Icons.check_circle_outline, size: 18),
          ],
        ),
      );
    }
    if (items.isEmpty) return const SizedBox.shrink();
    return ConstrainedBox(
      constraints: const BoxConstraints(maxHeight: 220),
      child: ListView.separated(
        shrinkWrap: true,
        itemCount: items.length.clamp(0, 6),
        separatorBuilder: (_, _) => const Divider(height: 1),
        itemBuilder: (context, index) {
          final item = items[index];
          return ListTile(
            dense: true,
            contentPadding: const EdgeInsets.symmetric(horizontal: 12),
            leading: const Icon(Icons.inventory_2_outlined, size: 20),
            title: _InventoryItemLabel(item: item),
            subtitle: _InventoryAvailabilityText(item: item),
            onTap: () => onSelected(item),
          );
        },
      ),
    );
  }
}
