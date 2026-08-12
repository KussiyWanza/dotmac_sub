import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';
import 'package:intl/intl.dart';
import 'package:uuid/uuid.dart';

import '../../app/theme.dart';
import '../../app/status_presentation.dart';
import '../../app/widgets/primary_action_button.dart';
import '../../core/offline/draft_store.dart';
import '../execution/execution_controller.dart';
import 'expense_models.dart';
import 'expenses_providers.dart';

const _statusOrder = ['submitted', 'approved', 'paid'];

class ExpensesScreen extends ConsumerWidget {
  const ExpensesScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final requests = ref.watch(expenseRequestsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Expenses'),
        actions: [
          IconButton(
            tooltip: 'New expense request',
            onPressed: () => context.push('/expenses/new'),
            icon: const Icon(Icons.add),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async => ref.invalidate(expenseRequestsProvider),
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    'Expense requests',
                    style: Theme.of(context).textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                FilledButton.icon(
                  onPressed: () => context.push('/expenses/new'),
                  icon: const Icon(Icons.add),
                  label: const Text('Request'),
                ),
              ],
            ),
            const SizedBox(height: 8),
            requests.when(
              data: (items) {
                if (items.isEmpty) {
                  return const Padding(
                    padding: EdgeInsets.symmetric(vertical: 48),
                    child: Center(child: Text('No expense requests yet')),
                  );
                }
                return Column(
                  children: [
                    for (final request in items)
                      _ExpenseRequestTile(request: request),
                  ],
                );
              },
              loading: () => const Padding(
                padding: EdgeInsets.only(top: 48),
                child: Center(child: CircularProgressIndicator()),
              ),
              error: (_, _) => const Padding(
                padding: EdgeInsets.only(top: 48),
                child: Center(child: Text('Could not load expense requests')),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ExpenseRequestTile extends StatelessWidget {
  const _ExpenseRequestTile({required this.request});

  final ExpenseRequest request;

  @override
  Widget build(BuildContext context) {
    final date = request.createdAt == null
        ? null
        : DateFormat('d MMM, HH:mm').format(request.createdAt!.toLocal());
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => context.push('/expenses/${request.id}'),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(
                Icons.receipt_long_outlined,
                color: _expenseStatusColor(context, request.status),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      request.purpose ?? request.displayNumber,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 6,
                      runSpacing: 4,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        _ExpenseStatusChip(status: request.status),
                        Text(request.displayNumber),
                        if (date != null) Text(date),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 112),
                child: Text(
                  _money(request.currency, request.totalAmount),
                  textAlign: TextAlign.right,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class ExpenseRequestDetailScreen extends ConsumerStatefulWidget {
  const ExpenseRequestDetailScreen({super.key, required this.id});

  final String id;

  @override
  ConsumerState<ExpenseRequestDetailScreen> createState() =>
      _ExpenseRequestDetailScreenState();
}

class _ExpenseRequestDetailScreenState
    extends ConsumerState<ExpenseRequestDetailScreen> {
  bool _canceling = false;

  Future<void> _cancel() async {
    if (_canceling) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Cancel expense request?'),
        content: const Text(
          'This withdraws the request before it is processed by finance.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Keep request'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Cancel request'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _canceling = true);
    try {
      await ref.read(expensesRepositoryProvider).cancelRequest(widget.id);
      ref
        ..invalidate(expenseRequestProvider(widget.id))
        ..invalidate(expenseRequestsProvider);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Expense request canceled')),
        );
      }
    } on DioException catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            _expenseErrorMessage(error, 'Could not cancel this request'),
          ),
        ),
      );
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Could not cancel this request')),
      );
    } finally {
      if (mounted) setState(() => _canceling = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final request = ref.watch(expenseRequestProvider(widget.id));
    return Scaffold(
      appBar: AppBar(title: const Text('Expense request')),
      body: request.when(
        data: (data) => ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text(
              data.purpose ?? data.displayNumber,
              style: Theme.of(
                context,
              ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 4),
            Text(
              data.displayNumber,
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                _ExpenseStatusChip(status: data.status),
                if (data.expenseDate != null)
                  Chip(label: Text(data.expenseDate!)),
              ],
            ),
            const SizedBox(height: 16),
            _ExpenseStatusTimeline(request: data),
            if (data.erpClaimNumber != null ||
                (data.erpSyncStatus == 'failed' &&
                    data.erpSyncError != null)) ...[
              const SizedBox(height: 16),
              _ExpenseErpSummary(request: data),
            ],
            if (data.status == 'rejected' && data.rejectionReason != null) ...[
              const SizedBox(height: 16),
              InputDecorator(
                decoration: const InputDecoration(labelText: 'Rejection'),
                child: Text(data.rejectionReason!),
              ),
            ],
            if (data.notes != null && data.notes!.isNotEmpty) ...[
              const SizedBox(height: 16),
              Text(data.notes!),
            ],
            const SizedBox(height: 24),
            Text('Items', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            if (data.items.isEmpty)
              const Text('No items on this request')
            else ...[
              for (final item in data.items)
                _ExpenseRequestItemTile(item: item, currency: data.currency),
              const Divider(height: 24),
              Row(
                children: [
                  const Expanded(
                    child: Text(
                      'Total',
                      style: TextStyle(fontWeight: FontWeight.w700),
                    ),
                  ),
                  Text(
                    _money(data.currency, data.totalAmount),
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                ],
              ),
            ],
            if (data.status == 'submitted') ...[
              const SizedBox(height: 24),
              OutlinedButton.icon(
                key: const Key('cancel-expense-request'),
                onPressed: _canceling ? null : _cancel,
                icon: const Icon(Icons.cancel_outlined),
                label: Text(_canceling ? 'Canceling...' : 'Cancel request'),
              ),
            ],
          ],
        ),
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) =>
            const Center(child: Text('Could not load this request')),
      ),
    );
  }
}

class _ExpenseStatusChip extends StatelessWidget {
  const _ExpenseStatusChip({required this.status});

  final String status;

  @override
  Widget build(BuildContext context) {
    final color = _expenseStatusColor(context, status);
    return Chip(
      visualDensity: VisualDensity.compact,
      label: Text(status.replaceAll('_', ' ')),
      backgroundColor: color.withValues(alpha: 0.16),
      side: BorderSide(color: color.withValues(alpha: 0.4)),
    );
  }
}

class _ExpenseStatusTimeline extends StatelessWidget {
  const _ExpenseStatusTimeline({required this.request});

  final ExpenseRequest request;

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
            error: step.error,
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
    this.error = false,
    this.date,
  });

  final String label;
  final DateTime? date;
  final bool active;
  final bool complete;
  final bool error;

  @override
  Widget build(BuildContext context) {
    final color = error
        ? Theme.of(context).colorScheme.error
        : active || complete
        ? Theme.of(context).colorScheme.primary
        : Theme.of(context).disabledColor;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(
        children: [
          Icon(
            error
                ? Icons.cancel_outlined
                : complete
                ? Icons.check_circle
                : Icons.radio_button_unchecked,
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

class _ExpenseErpSummary extends StatelessWidget {
  const _ExpenseErpSummary({required this.request});

  final ExpenseRequest request;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Finance', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        if (request.erpClaimNumber != null)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.account_balance_outlined),
            title: const Text('ERP claim'),
            subtitle: Text(
              [
                request.erpClaimNumber!,
                if (request.erpClaimStatus != null)
                  request.erpClaimStatus!.replaceAll('_', ' '),
              ].join(' · '),
            ),
          ),
        if (request.erpSyncStatus == 'failed' && request.erpSyncError != null)
          Text(
            'Sync failed: ${request.erpSyncError!}',
            style: TextStyle(color: Theme.of(context).colorScheme.error),
          ),
      ],
    );
  }
}

class _ExpenseRequestItemTile extends StatelessWidget {
  const _ExpenseRequestItemTile({required this.item, this.currency});

  final ExpenseRequestItem item;
  final String? currency;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  item.description ?? item.categoryLabel,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 2),
                Text(
                  [
                    item.categoryLabel,
                    if (item.vendorName != null && item.vendorName!.isNotEmpty)
                      item.vendorName!,
                    if (item.notes != null && item.notes!.isNotEmpty)
                      item.notes!,
                  ].join(' · '),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ],
            ),
          ),
          const SizedBox(width: 12),
          ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 112),
            child: Text(
              _money(currency, item.amount),
              textAlign: TextAlign.right,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
          ),
        ],
      ),
    );
  }
}

class _VendorField extends StatelessWidget {
  const _VendorField({required this.controller, required this.vendors});

  final TextEditingController controller;
  final AsyncValue<List<String>> vendors;

  @override
  Widget build(BuildContext context) {
    final values = vendors.valueOrNull ?? const <String>[];
    if (values.isEmpty) {
      return TextField(
        key: const Key('expense-vendor'),
        controller: controller,
        decoration: const InputDecoration(labelText: 'Vendor'),
      );
    }

    final selected = values.contains(controller.text) ? controller.text : null;
    return DropdownButtonFormField<String>(
      key: const Key('expense-vendor'),
      initialValue: selected,
      decoration: const InputDecoration(labelText: 'Vendor'),
      items: [
        for (final vendor in values)
          DropdownMenuItem(
            value: vendor,
            child: Text(vendor, maxLines: 1, overflow: TextOverflow.ellipsis),
          ),
      ],
      onChanged: (value) => controller.text = value ?? '',
    );
  }
}

class NewExpenseRequestScreen extends ConsumerStatefulWidget {
  const NewExpenseRequestScreen({
    super.key,
    this.initialWorkOrderId,
    this.initialWorkOrderLabel,
  });

  final String? initialWorkOrderId;
  final String? initialWorkOrderLabel;

  @override
  ConsumerState<NewExpenseRequestScreen> createState() =>
      _NewExpenseRequestScreenState();
}

class _NewExpenseRequestScreenState
    extends ConsumerState<NewExpenseRequestScreen> {
  final _purpose = TextEditingController();
  final _notes = TextEditingController();
  final _workOrderId = TextEditingController();
  final _categoryCode = TextEditingController();
  final _description = TextEditingController();
  final _amount = TextEditingController();
  final _vendor = TextEditingController();
  final _receiptUrl = TextEditingController();
  DateTime _expenseDate = DateTime.now();
  ExpenseCategory? _selectedCategory;
  final _items = <ExpenseItemDraft>[];
  bool _saving = false;
  bool _receiptUploading = false;
  String _receiptFileName = '';
  String _submitError = '';
  String _lineError = '';

  @override
  void initState() {
    super.initState();
    _workOrderId.text = widget.initialWorkOrderId ?? '';
    Future.microtask(_loadDraft);
  }

  @override
  void dispose() {
    _purpose.dispose();
    _notes.dispose();
    _workOrderId.dispose();
    _categoryCode.dispose();
    _description.dispose();
    _amount.dispose();
    _vendor.dispose();
    _receiptUrl.dispose();
    super.dispose();
  }

  void _addLine() {
    final categoryCode =
        _selectedCategory?.categoryCode ?? _categoryCode.text.trim();
    final description = _description.text.trim();
    final amount = double.tryParse(_amount.text.trim()) ?? 0;
    if (categoryCode.isEmpty) {
      setState(() => _lineError = 'Pick an expense category.');
      return;
    }
    if (description.isEmpty) {
      setState(() => _lineError = 'Describe what the expense was for.');
      return;
    }
    if (amount <= 0) {
      setState(() => _lineError = 'Enter an amount greater than zero.');
      return;
    }
    final maxAmount = _selectedCategory?.maxAmountPerClaim;
    if (maxAmount != null && amount > maxAmount) {
      setState(
        () => _lineError =
            'Amount is above the ${_selectedCategory!.displayName} '
            'limit of ${maxAmount.toStringAsFixed(2)}.',
      );
      return;
    }
    if (_selectedCategory?.requiresReceipt == true &&
        _receiptUrl.text.trim().isEmpty) {
      setState(
        () => _lineError =
            '${_selectedCategory!.displayName} requires a receipt.',
      );
      return;
    }
    setState(() {
      _items.add(
        ExpenseItemDraft(
          categoryCode: categoryCode,
          categoryName: _selectedCategory?.categoryName,
          description: description,
          amount: amount,
          vendorName: _vendor.text,
          receiptUrl: _receiptUrl.text,
        ),
      );
      _selectedCategory = null;
      _categoryCode.clear();
      _description.clear();
      _amount.clear();
      _vendor.clear();
      _receiptUrl.clear();
      _receiptFileName = '';
      _lineError = '';
    });
  }

  void _removeLine(int index) {
    setState(() => _items.removeAt(index));
  }

  Future<void> _pickDate() async {
    final picked = await showDatePicker(
      context: context,
      initialDate: _expenseDate,
      firstDate: DateTime.now().subtract(const Duration(days: 365)),
      lastDate: DateTime.now().add(const Duration(days: 1)),
    );
    if (picked != null) setState(() => _expenseDate = picked);
  }

  Future<void> _loadDraft() async {
    final draft = await ref
        .read(draftStoreProvider)
        .load(expenseRequestDraftId);
    if (!mounted || draft == null) return;
    setState(() {
      _purpose.text = draft['purpose'] as String? ?? '';
      _notes.text = draft['notes'] as String? ?? '';
      _workOrderId.text =
          widget.initialWorkOrderId ?? draft['work_order_id'] as String? ?? '';
      final date = draft['expense_date'];
      if (date is String) {
        _expenseDate = DateTime.tryParse(date) ?? _expenseDate;
      }
      _items
        ..clear()
        ..addAll(_expenseDraftItems(draft['items']));
    });
  }

  Future<void> _saveDraft() async {
    await ref
        .read(draftStoreProvider)
        .save(
          id: expenseRequestDraftId,
          type: 'expense_request',
          payload: {
            'purpose': _purpose.text,
            'expense_date': DateFormat('yyyy-MM-dd').format(_expenseDate),
            'notes': _notes.text,
            'work_order_id': _workOrderId.text,
            'items': _items.map(_expenseDraftItemJson).toList(),
          },
        );
    if (!mounted) return;
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(const SnackBar(content: Text('Draft saved')));
  }

  Future<void> _pickReceipt(ImageSource source) async {
    final workOrderId = _workOrderId.text.trim();
    if (workOrderId.isEmpty) {
      setState(
        () => _lineError = 'Enter a work order ID before uploading a receipt.',
      );
      return;
    }
    final picked = await ImagePicker().pickImage(
      source: source,
      imageQuality: 85,
    );
    if (picked == null) return;
    setState(() {
      _receiptUploading = true;
      _lineError = '';
    });
    try {
      final url = await ref
          .read(expensesRepositoryProvider)
          .uploadReceipt(
            workOrderId: workOrderId,
            filePath: picked.path,
            fileName: picked.name,
            clientRef: const Uuid().v4(),
          );
      if (!mounted) return;
      setState(() {
        _receiptUrl.text = url;
        _receiptFileName = picked.name;
      });
    } on DioException catch (error) {
      if (!mounted) return;
      setState(
        () => _lineError = _expenseErrorMessage(
          error,
          'Could not upload receipt.',
        ),
      );
    } catch (_) {
      if (!mounted) return;
      setState(() => _lineError = 'Could not upload receipt.');
    } finally {
      if (mounted) setState(() => _receiptUploading = false);
    }
  }

  Future<void> _chooseReceiptSource() async {
    final source = await showModalBottomSheet<ImageSource>(
      context: context,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              key: const Key('expense-receipt-camera'),
              leading: const Icon(Icons.photo_camera_outlined),
              title: const Text('Take photo'),
              onTap: () => Navigator.of(context).pop(ImageSource.camera),
            ),
            ListTile(
              key: const Key('expense-receipt-gallery'),
              leading: const Icon(Icons.photo_library_outlined),
              title: const Text('Choose from device'),
              onTap: () => Navigator.of(context).pop(ImageSource.gallery),
            ),
          ],
        ),
      ),
    );
    if (source != null) await _pickReceipt(source);
  }

  Future<void> _submit() async {
    if (_items.isEmpty || _saving) return;
    if (_purpose.text.trim().isEmpty) {
      setState(() => _submitError = 'Purpose is required.');
      return;
    }
    final clientRef = const Uuid().v4();
    final payload = buildExpenseRequestPayload(
      purpose: _purpose.text,
      clientRef: clientRef,
      expenseDate: DateFormat('yyyy-MM-dd').format(_expenseDate),
      notes: _notes.text,
      workOrderId: _workOrderId.text,
      items: _items,
    );
    setState(() => _saving = true);
    try {
      final request = await ref
          .read(expensesRepositoryProvider)
          .createRequest(
            purpose: _purpose.text,
            clientRef: clientRef,
            expenseDate: DateFormat('yyyy-MM-dd').format(_expenseDate),
            notes: _notes.text,
            workOrderId: _workOrderId.text,
            items: _items,
          );
      ref.invalidate(expenseRequestsProvider);
      try {
        await ref.read(expenseRequestsProvider.future);
      } catch (_) {
        // The request was created; the list can still be refreshed manually.
      }
      await ref.read(draftStoreProvider).delete(expenseRequestDraftId);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('${request.displayNumber} submitted')),
        );
        context.go('/expenses');
      }
    } on DioException catch (error) {
      if (!mounted) return;
      if (error.response == null) {
        await ref
            .read(syncServiceProvider)
            .enqueue(
              kind: 'expense_request',
              clientRef: clientRef,
              payload: payload,
            );
        await ref.read(draftStoreProvider).delete(expenseRequestDraftId);
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Expense request queued for sync')),
        );
        context.go('/expenses');
        return;
      }
      final message = _expenseErrorMessage(
        error,
        'Could not submit expense request',
      );
      setState(() => _submitError = message);
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(message)));
    } catch (_) {
      if (!mounted) return;
      const message = 'Could not submit expense request';
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
    final categories = ref.watch(expenseCategoriesProvider);
    final vendors = ref.watch(expenseVendorsProvider);
    final total = _items.fold<double>(0, (sum, item) => sum + item.amount);
    return Scaffold(
      appBar: AppBar(title: const Text('New expense request')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(
            key: const Key('expense-purpose'),
            controller: _purpose,
            decoration: const InputDecoration(
              labelText: 'Purpose',
              helperText: 'What was this expense for?',
            ),
          ),
          const SizedBox(height: 12),
          InkWell(
            onTap: _pickDate,
            child: InputDecorator(
              decoration: const InputDecoration(
                labelText: 'Expense date',
                suffixIcon: Icon(Icons.calendar_today_outlined, size: 18),
              ),
              child: Text(DateFormat('d MMM yyyy').format(_expenseDate)),
            ),
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
            controller: _notes,
            decoration: const InputDecoration(labelText: 'Notes'),
            maxLines: 3,
          ),
          const SizedBox(height: 24),
          Text('Add expenses', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          categories.when(
            data: (items) => items.isEmpty
                ? TextField(
                    key: const Key('expense-category-code'),
                    controller: _categoryCode,
                    decoration: const InputDecoration(
                      labelText: 'Category code',
                    ),
                  )
                : DropdownButtonFormField<ExpenseCategory>(
                    key: const Key('expense-category'),
                    initialValue: _selectedCategory,
                    decoration: const InputDecoration(labelText: 'Category'),
                    items: [
                      for (final category in items)
                        DropdownMenuItem(
                          value: category,
                          child: Text(
                            category.displayName,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: (value) =>
                        setState(() => _selectedCategory = value),
                  ),
            loading: () => const LinearProgressIndicator(),
            error: (_, _) => TextField(
              key: const Key('expense-category-code'),
              controller: _categoryCode,
              decoration: const InputDecoration(labelText: 'Category code'),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            key: const Key('expense-description'),
            controller: _description,
            decoration: const InputDecoration(labelText: 'Description'),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: TextField(
                  key: const Key('expense-amount'),
                  controller: _amount,
                  keyboardType: const TextInputType.numberWithOptions(
                    decimal: true,
                  ),
                  style: const TextStyle(
                    fontFamily: 'Outfit',
                    fontWeight: FontWeight.w700,
                    fontSize: 18,
                    fontFeatures: [FontFeature.tabularFigures()],
                  ),
                  decoration: InputDecoration(
                    labelText: 'Amount',
                    prefixText: '₦ ',
                    prefixStyle: TextStyle(
                      fontFamily: 'Outfit',
                      fontWeight: FontWeight.w700,
                      fontSize: 18,
                      color: AppColors.primary,
                    ),
                    filled: true,
                    fillColor: AppColors.primary.withValues(alpha: 0.06),
                  ),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _VendorField(controller: _vendor, vendors: vendors),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: TextField(
                  key: const Key('expense-receipt-url'),
                  controller: _receiptUrl,
                  decoration: InputDecoration(
                    labelText: _selectedCategory?.requiresReceipt == true
                        ? 'Receipt URL required'
                        : 'Receipt URL',
                    helperText: _receiptFileName.isEmpty
                        ? null
                        : 'Uploaded $_receiptFileName',
                  ),
                ),
              ),
              const SizedBox(width: 8),
              IconButton.filledTonal(
                key: const Key('expense-receipt-upload'),
                onPressed: _receiptUploading ? null : _chooseReceiptSource,
                icon: _receiptUploading
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.upload_file_outlined),
                tooltip: 'Upload receipt',
              ),
            ],
          ),
          if (_lineError.isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(
              _lineError,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
          const SizedBox(height: 8),
          OutlinedButton.icon(
            key: const Key('add-expense-line'),
            onPressed: _addLine,
            icon: const Icon(Icons.add),
            label: const Text('Add expense'),
          ),
          const SizedBox(height: 16),
          for (final (index, item) in _items.indexed)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 6),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Icon(Icons.receipt_long_outlined),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          item.description,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                        ),
                        const SizedBox(height: 2),
                        Text(
                          [
                            item.categoryName ?? item.categoryCode,
                            if (item.vendorName != null &&
                                item.vendorName!.trim().isNotEmpty)
                              item.vendorName!.trim(),
                            if (item.receiptUrl != null &&
                                item.receiptUrl!.trim().isNotEmpty)
                              'receipt attached',
                          ].join(' · '),
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 8),
                  ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 104),
                    child: Text(
                      _money('NGN', item.amount),
                      textAlign: TextAlign.right,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                  ),
                  IconButton(
                    tooltip: 'Remove expense',
                    onPressed: () => _removeLine(index),
                    icon: const Icon(Icons.delete_outline),
                  ),
                ],
              ),
            ),
          if (_items.isNotEmpty)
            Align(
              alignment: Alignment.centerRight,
              child: Text(
                'Total ${_money('NGN', total)}',
                style: Theme.of(
                  context,
                ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
              ),
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
              PrimaryActionButton(
                onPressed: _items.isEmpty || _saving ? null : _submit,
                icon: Icons.check_rounded,
                label: _saving ? 'Submitting…' : 'Submit request',
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

String _money(String? currency, double value) =>
    '${currency ?? 'NGN'} ${value.toStringAsFixed(2)}';

Color _expenseStatusColor(BuildContext context, String status) {
  final scheme = Theme.of(context).colorScheme;
  return switch (status) {
    'submitted' => AppColors.statusTone(context, StatusTone.info),
    'approved' || 'paid' => AppColors.statusTone(context, StatusTone.positive),
    'rejected' => scheme.error,
    'canceled' || 'cancelled' || 'draft' => scheme.outline,
    _ => scheme.outline,
  };
}

String _expenseErrorMessage(DioException error, String fallback) {
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
  return fallback;
}

List<_StatusStep> _timelineSteps(ExpenseRequest request) {
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
        error: true,
      ),
    ];
  }
  if (request.status == 'canceled' || request.status == 'cancelled') {
    return [
      _StatusStep(
        label: 'Submitted',
        date: request.submittedAt ?? request.createdAt,
        complete: true,
        active: false,
      ),
      _StatusStep(
        label: 'Canceled',
        date: request.updatedAt,
        complete: true,
        active: true,
      ),
    ];
  }

  final activeIndex = _statusOrder.indexOf(request.status);
  return [
    _StatusStep(
      label: 'Submitted',
      date: request.submittedAt ?? request.createdAt,
      complete: activeIndex >= 0,
      active: activeIndex == 0,
    ),
    _StatusStep(
      label: 'Approved',
      date: request.approvedAt,
      complete: activeIndex >= 1,
      active: activeIndex == 1,
    ),
    _StatusStep(
      label: 'Paid',
      date: request.paidAt,
      complete: activeIndex >= 2,
      active: activeIndex == 2,
    ),
  ];
}

class _StatusStep {
  const _StatusStep({
    required this.label,
    required this.complete,
    required this.active,
    this.error = false,
    this.date,
  });

  final String label;
  final DateTime? date;
  final bool complete;
  final bool active;
  final bool error;
}

Map<String, dynamic> _expenseDraftItemJson(ExpenseItemDraft item) => {
  'category_code': item.categoryCode,
  'category_name': item.categoryName,
  'description': item.description,
  'amount': item.amount,
  'expense_date': item.expenseDate,
  'vendor_name': item.vendorName,
  'receipt_url': item.receiptUrl,
  'notes': item.notes,
};

List<ExpenseItemDraft> _expenseDraftItems(Object? raw) {
  if (raw is! List) return const [];
  return raw.whereType<Map>().map((item) {
    final data = item.cast<String, dynamic>();
    return ExpenseItemDraft(
      categoryCode: data['category_code'] as String? ?? '',
      categoryName: data['category_name'] as String?,
      description: data['description'] as String? ?? '',
      amount: switch (data['amount']) {
        num value => value.toDouble(),
        String value => double.tryParse(value) ?? 0,
        _ => 0,
      },
      expenseDate: data['expense_date'] as String?,
      vendorName: data['vendor_name'] as String?,
      receiptUrl: data['receipt_url'] as String?,
      notes: data['notes'] as String?,
    );
  }).toList();
}
