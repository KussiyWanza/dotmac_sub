import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';
import 'package:intl/intl.dart';
import 'package:uuid/uuid.dart';

import '../../app/theme.dart';
import '../../app/status_presentation.dart';
import '../../app/widgets/primary_action_button.dart';
import '../../core/offline/draft_store.dart';
import '../jobs/job_models.dart';
import '../jobs/jobs_providers.dart';
import '../manager/manager_providers.dart';
import 'expense_models.dart';
import 'expenses_providers.dart';

const _statusOrder = ['submitted', 'approved', 'paid'];

class ExpensesScreen extends ConsumerWidget {
  const ExpensesScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final requests = ref.watch(expenseRequestsProvider);
    final drafts = ref.watch(expenseRequestDraftsProvider);

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
            Text(
              'Expense requests',
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 8),
            drafts.when(
              data: (items) => Column(
                children: [
                  for (final draft in items)
                    Card(
                      child: ListTile(
                        leading: const Icon(Icons.edit_note_outlined),
                        title: Text(
                          draft.payload['purpose']?.toString().isNotEmpty ==
                                  true
                              ? draft.payload['purpose'].toString()
                              : 'Saved expense draft',
                        ),
                        subtitle: const Text('Tap to continue editing'),
                        trailing: const Icon(Icons.chevron_right),
                        onTap: () => context.push('/expenses/new'),
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
    final isLocal =
        request.status == 'queued' || request.status == 'sync failed';
    final date = request.createdAt == null
        ? null
        : DateFormat('d MMM, HH:mm').format(request.createdAt!.toLocal());
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: isLocal ? null : () => context.push('/expenses/${request.id}'),
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
                        if (request.erpClaimStatus != null)
                          Text('ERP ${request.erpClaimStatus}'),
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
            if (data.erpClaimNumber != null || data.erpSyncStatus != null) ...[
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
        if (request.selectedApproverName != null)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.approval_outlined),
            title: const Text('Expense approver'),
            subtitle: Text(request.selectedApproverName!),
          ),
        if (request.recipientBankName != null)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.account_balance_wallet_outlined),
            title: const Text('Payment destination'),
            subtitle: Text(
              [
                if (request.verifiedBeneficiaryName != null)
                  request.verifiedBeneficiaryName!,
                request.recipientBankName!,
                if (request.maskedAccountNumber != null)
                  request.maskedAccountNumber!,
              ].join(' · '),
            ),
          ),
        if (request.erpSyncStatus != null)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.sync_outlined),
            title: const Text('ERP sync'),
            subtitle: Text(_expenseErpSyncLabel(request.erpSyncStatus!)),
          ),
        if (request.paymentStatus != null)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.payments_outlined),
            title: const Text('Payment'),
            subtitle: Text(_expensePaymentLabel(request.paymentStatus!)),
          ),
        if (request.paymentError != null)
          Text(
            request.paymentError!,
            style: TextStyle(color: Theme.of(context).colorScheme.error),
          ),
        if ({
              'dead',
              'rejected',
              'not_configured',
              'not_queued',
            }.contains(request.erpSyncStatus) &&
            request.erpSyncError != null)
          Text(
            'Sync failed: ${request.erpSyncError!}',
            style: TextStyle(color: Theme.of(context).colorScheme.error),
          ),
      ],
    );
  }
}

String _expenseErpSyncLabel(String status) => switch (status) {
  'pending' => 'Waiting to send',
  'sent' => 'Sent; waiting for ERP confirmation',
  'accepted' => 'Synced',
  'rejected' => 'Rejected by ERP',
  'dead' => 'Failed; needs attention',
  'not_configured' => 'ERP delivery is not configured',
  'not_queued' => 'Not queued; needs attention',
  _ => status.replaceAll('_', ' '),
};

String _expensePaymentLabel(String status) => switch (status) {
  'queued' => 'Queued securely for ERP processing',
  'pending' => 'Prepared; awaiting transfer initiation',
  'processing' => 'Transfer is processing',
  'completed' => 'Transfer completed',
  'failed' => 'Transfer failed; a manager may retry',
  'indeterminate' => 'Transfer outcome is unknown; do not retry',
  'delivery_failed' => 'Payment command could not reach ERP',
  _ => status.replaceAll('_', ' '),
};

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
      isExpanded: true,
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
    this.initialProjectId,
    this.initialTicketId,
  });

  final String? initialWorkOrderId;
  final String? initialWorkOrderLabel;
  final String? initialProjectId;
  final String? initialTicketId;

  @override
  ConsumerState<NewExpenseRequestScreen> createState() =>
      _NewExpenseRequestScreenState();
}

class _NewExpenseRequestScreenState
    extends ConsumerState<NewExpenseRequestScreen> {
  final _purpose = TextEditingController();
  final _notes = TextEditingController();
  final _projectId = TextEditingController();
  final _ticketId = TextEditingController();
  final _description = TextEditingController();
  final _amount = TextEditingController();
  final _vendor = TextEditingController();
  final _receiptUrl = TextEditingController();
  final _accountNumber = TextEditingController();
  final _beneficiaryName = TextEditingController();
  final _approverFieldKey = GlobalKey();
  final String _clientRef = const Uuid().v4();
  String _workOrderId = '';
  DateTime _expenseDate = DateTime.now();
  ExpenseCategory? _selectedCategory;
  ExpenseApprover? _selectedApprover;
  ExpenseBank? _selectedBank;
  ExpensePaymentMode _destinationMode = ExpensePaymentMode.erpProfile;
  final _items = <ExpenseItemDraft>[];
  bool _saving = false;
  bool _receiptUploading = false;
  String _receiptFileName = '';
  String? _receiptAttachmentId;
  String _submitError = '';
  String _approverError = '';
  String _lineError = '';

  @override
  void initState() {
    super.initState();
    _workOrderId = widget.initialWorkOrderId ?? '';
    _projectId.text = widget.initialProjectId ?? '';
    _ticketId.text = widget.initialTicketId ?? '';
    Future.microtask(_loadDraft);
    Future.microtask(_syncDestinationMode);
  }

  @override
  void dispose() {
    _purpose.dispose();
    _notes.dispose();
    _projectId.dispose();
    _ticketId.dispose();
    _description.dispose();
    _amount.dispose();
    _vendor.dispose();
    _receiptUrl.dispose();
    _accountNumber.dispose();
    _beneficiaryName.dispose();
    super.dispose();
  }

  void _addLine() {
    final category = _selectedCategory;
    final description = _description.text.trim();
    final amount = double.tryParse(_amount.text.trim()) ?? 0;
    if (category == null) {
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
    final maxAmount = category.maxAmountPerClaim;
    if (maxAmount != null && amount > maxAmount) {
      setState(
        () => _lineError =
            'Amount is above the ${category.displayName} '
            'limit of ${maxAmount.toStringAsFixed(2)}.',
      );
      return;
    }
    if (category.requiresReceipt &&
        _receiptUrl.text.trim().isEmpty &&
        (_receiptAttachmentId == null || _receiptAttachmentId!.isEmpty)) {
      setState(
        () => _lineError = '${category.displayName} requires a receipt.',
      );
      return;
    }
    setState(() {
      _items.add(
        ExpenseItemDraft(
          categoryCode: category.categoryCode,
          categoryName: category.categoryName,
          description: description,
          amount: amount,
          vendorName: _vendor.text,
          receiptUrl: _receiptUrl.text,
          receiptAttachmentId: _receiptAttachmentId,
        ),
      );
      _selectedCategory = null;
      _description.clear();
      _amount.clear();
      _vendor.clear();
      _receiptUrl.clear();
      _receiptFileName = '';
      _receiptAttachmentId = null;
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
      _workOrderId =
          widget.initialWorkOrderId ?? draft['work_order_id'] as String? ?? '';
      _projectId.text =
          widget.initialProjectId ?? draft['project_id'] as String? ?? '';
      _ticketId.text =
          widget.initialTicketId ?? draft['ticket_id'] as String? ?? '';
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
            'work_order_id': _workOrderId,
            'project_id': _projectId.text,
            'ticket_id': _ticketId.text,
            'items': _items.map((item) => item.toDraftJson()).toList(),
          },
        );
    ref.invalidate(expenseRequestDraftsProvider);
    if (!mounted) return;
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(const SnackBar(content: Text('Draft saved')));
  }

  Future<void> _syncDestinationMode() async {
    try {
      final data = await ref.read(expenseFormContextProvider.future);
      if (!mounted) return;
      setState(() {
        _destinationMode = data.profileDestination.available
            ? ExpensePaymentMode.erpProfile
            : ExpensePaymentMode.expenseOverride;
      });
    } catch (_) {
      // The visible form-context error owns retry guidance.
    }
  }

  void _retryFormContext() {
    setState(() {
      _selectedApprover = null;
      _selectedBank = null;
      _approverError = '';
      _submitError = '';
    });
    ref.invalidate(expenseFormContextProvider);
    unawaited(_syncDestinationMode());
  }

  void _retryCategories() {
    setState(() {
      _selectedCategory = null;
      _lineError = '';
      _submitError = '';
    });
    ref.invalidate(expenseCategoriesProvider);
  }

  void _showApproverError(String message) {
    setState(() {
      _approverError = message;
      _submitError = message;
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final fieldContext = _approverFieldKey.currentContext;
      if (fieldContext == null) return;
      unawaited(
        Scrollable.ensureVisible(
          fieldContext,
          alignment: 0.2,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOutCubic,
        ),
      );
    });
  }

  Widget _workOrderSelector(AsyncValue<JobList> workOrders) {
    return workOrders.when(
      data: (list) {
        final seen = <String>{};
        final jobs = [
          for (final job in list.jobs)
            if (job.id.trim().isNotEmpty && seen.add(job.id)) job,
        ];
        JobSummary? selected;
        for (final job in jobs) {
          if (job.id == _workOrderId) {
            selected = job;
            break;
          }
        }
        final savedSelectionIsUnavailable =
            _workOrderId.isNotEmpty && selected == null;
        if (jobs.isEmpty) {
          return _WorkOrderAvailability(
            message: 'No assigned work orders are available.',
            onRetry: () => ref.invalidate(allAssignedJobsProvider),
          );
        }
        return Semantics(
          button: true,
          child: InkWell(
            key: const Key('expense-work-order'),
            borderRadius: BorderRadius.circular(12),
            onTap: _saving
                ? null
                : () async {
                    final picked = await showModalBottomSheet<JobSummary>(
                      context: context,
                      isScrollControlled: true,
                      useSafeArea: true,
                      builder: (context) => _WorkOrderPickerSheet(
                        jobs: jobs,
                        selectedId: selected?.id,
                      ),
                    );
                    if (!mounted || picked == null) return;
                    setState(() {
                      _workOrderId = picked.id;
                      _submitError = '';
                    });
                  },
            child: InputDecorator(
              isEmpty: selected == null,
              decoration: InputDecoration(
                labelText: 'Work order',
                helperText: selected == null
                    ? list.fromCache
                          ? 'Choose from your saved assigned work orders.'
                          : 'Choose from your assigned work orders.'
                    : '${selected.id} · ${selected.statusPresentation.label}',
                errorText: savedSelectionIsUnavailable
                    ? 'This saved work order is no longer assigned. Choose another.'
                    : null,
                suffixIcon: const Icon(Icons.unfold_more_rounded),
              ),
              child: Text(
                selected?.title ?? 'Select a work order',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: selected == null
                    ? TextStyle(color: Theme.of(context).hintColor)
                    : null,
              ),
            ),
          ),
        );
      },
      loading: () => const _WorkOrderAvailability(
        message: 'Loading assigned work orders…',
        loading: true,
      ),
      error: (_, _) => _WorkOrderAvailability(
        message: 'Could not load assigned work orders.',
        onRetry: () => ref.invalidate(allAssignedJobsProvider),
      ),
    );
  }

  Future<void> _pickReceipt(ImageSource source) async {
    final workOrderId = _workOrderId.trim();
    if (workOrderId.isEmpty) {
      setState(
        () => _lineError = 'Select a work order before uploading a receipt.',
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
      final upload = await ref
          .read(expensesRepositoryProvider)
          .uploadReceipt(
            workOrderId: workOrderId,
            filePath: picked.path,
            fileName: picked.name,
            clientRef: const Uuid().v4(),
          );
      if (!mounted) return;
      setState(() {
        _receiptUrl.clear();
        _receiptAttachmentId = upload.attachmentId;
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
    final workOrderId = _workOrderId.trim();
    if (workOrderId.isEmpty) {
      setState(() => _submitError = 'Select a work order.');
      return;
    }
    if (widget.initialWorkOrderId == null) {
      final assigned = ref.read(allAssignedJobsProvider).asData?.value.jobs;
      if (assigned == null) {
        setState(
          () => _submitError = 'Wait for your assigned work orders to load.',
        );
        return;
      }
      if (!assigned.any((job) => job.id == workOrderId)) {
        setState(
          () => _submitError = 'Select one of your assigned work orders.',
        );
        return;
      }
    }
    final formContext = ref.read(expenseFormContextProvider).asData?.value;
    if (formContext == null) {
      setState(
        () => _submitError =
            'Expense approvers and payment details are unavailable. Retry above.',
      );
      return;
    }
    if (formContext.approvers.isEmpty) {
      setState(
        () => _submitError =
            'No eligible expense approvers are available for your account.',
      );
      return;
    }
    final selectedApprover = _selectedApprover;
    if (selectedApprover == null) {
      _showApproverError('Select an expense approver.');
      return;
    }
    if (!formContext.approvers.any(
      (approver) =>
          approver.erpEmployeeId == selectedApprover.erpEmployeeId &&
          approver.systemUserId == selectedApprover.systemUserId,
    )) {
      _showApproverError('This expense approver is no longer available.');
      return;
    }
    final categories = ref.read(expenseCategoriesProvider).asData?.value;
    if (categories == null || categories.isEmpty) {
      setState(
        () => _submitError =
            'Expense categories are unavailable. Retry the category list above.',
      );
      return;
    }
    final categoryCodes = {
      for (final category in categories) category.categoryCode,
    };
    if (_items.any((item) => !categoryCodes.contains(item.categoryCode))) {
      setState(
        () => _submitError =
            'A saved expense uses a category that is no longer available. Remove it and add it again.',
      );
      return;
    }
    if (_destinationMode == ExpensePaymentMode.erpProfile &&
        !formContext.profileDestination.available) {
      setState(
        () => _submitError =
            'Your ERP payment profile is unavailable. Use different payment details.',
      );
      return;
    }
    if (_destinationMode == ExpensePaymentMode.expenseOverride &&
        formContext.banks.isEmpty) {
      setState(
        () => _submitError =
            'No banks are available for different payment details. Retry above.',
      );
      return;
    }
    if (_destinationMode == ExpensePaymentMode.expenseOverride) {
      final accountNumber = _accountNumber.text.trim();
      final accountName = _beneficiaryName.text.trim();
      if (_selectedBank == null) {
        setState(() => _submitError = 'Select a bank.');
        return;
      }
      if (accountNumber.length < 6) {
        setState(
          () =>
              _submitError = 'Enter an account number with at least 6 digits.',
        );
        return;
      }
      if (accountName.length < 2) {
        setState(() => _submitError = 'Enter the account name.');
        return;
      }
    }
    setState(() => _saving = true);
    try {
      final verified = await ref
          .read(expensesRepositoryProvider)
          .verifyDestination(
            sourceClaimId: _clientRef,
            mode: _destinationMode,
            bankCode: _selectedBank?.bankCode,
            accountNumber: _accountNumber.text.trim(),
            beneficiaryName: _beneficiaryName.text.trim(),
          );
      final request = await ref
          .read(expensesRepositoryProvider)
          .submitRequest(
            purpose: _purpose.text,
            clientRef: _clientRef,
            expenseDate: DateFormat('yyyy-MM-dd').format(_expenseDate),
            notes: _notes.text,
            workOrderId: workOrderId,
            projectId: _projectId.text,
            ticketId: _ticketId.text,
            items: _items,
            selectedApprover: selectedApprover,
            paymentDestination: verified,
          );
      _invalidateExpenseProjections(ref);
      try {
        await ref.read(expenseRequestsProvider.future);
      } catch (_) {
        // The request was created; the list can still be refreshed manually.
      }
      await ref.read(draftStoreProvider).delete(expenseRequestDraftId);
      ref.invalidate(expenseRequestDraftsProvider);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('${request.displayNumber} submitted')),
        );
        context.go('/expenses');
      }
    } on DioException catch (error) {
      if (!mounted) return;
      if (error.response == null) {
        const message =
            'Connect to the internet to verify payment details and submit. You can save the non-sensitive draft.';
        setState(() => _submitError = message);
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text(message)));
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
    final formContext = ref.watch(expenseFormContextProvider);
    final assignedWorkOrders = widget.initialWorkOrderId == null
        ? ref.watch(allAssignedJobsProvider)
        : null;
    final total = _items.fold<double>(0, (sum, item) => sum + item.amount);
    final formContextData = formContext.asData?.value;
    final categoriesData = categories.asData?.value;
    final categoriesReady = categoriesData != null && categoriesData.isNotEmpty;
    final currentCategoryCodes = {
      for (final category in categoriesData ?? const <ExpenseCategory>[])
        category.categoryCode,
    };
    final itemCategoriesValid =
        categoriesReady &&
        _items.every(
          (item) => currentCategoryCodes.contains(item.categoryCode),
        );
    final paymentContextReady =
        formContextData != null &&
        (_destinationMode == ExpensePaymentMode.erpProfile
            ? formContextData.profileDestination.available
            : formContextData.banks.isNotEmpty);
    final requiredServerDataReady =
        formContextData != null &&
        formContextData.approvers.isNotEmpty &&
        paymentContextReady &&
        categoriesReady;
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
            _workOrderSelector(assignedWorkOrders!),
          ],
          const SizedBox(height: 12),
          if (widget.initialProjectId != null)
            TextFormField(
              initialValue: widget.initialProjectId,
              readOnly: true,
              decoration: const InputDecoration(labelText: 'Linked project'),
            ),
          if (widget.initialProjectId != null) const SizedBox(height: 12),
          if (widget.initialTicketId != null)
            TextFormField(
              initialValue: widget.initialTicketId,
              readOnly: true,
              decoration: const InputDecoration(labelText: 'Linked ticket'),
            ),
          if (widget.initialTicketId != null) const SizedBox(height: 12),
          TextField(
            controller: _notes,
            decoration: const InputDecoration(labelText: 'Notes'),
            maxLines: 3,
          ),
          const SizedBox(height: 24),
          Card(
            key: const Key('expense-approver-payment-card'),
            margin: EdgeInsets.zero,
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Row(
                    children: [
                      Icon(
                        Icons.account_balance_wallet_outlined,
                        color: Theme.of(context).colorScheme.primary,
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(
                          'Approver & payment',
                          style: Theme.of(context).textTheme.titleMedium
                              ?.copyWith(fontWeight: FontWeight.w700),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 16),
                  formContext.when(
                    loading: () => const _ExpenseDataAvailability(
                      label: 'Approval and payment',
                      message: 'Loading expense approvers and payment details…',
                      loading: true,
                    ),
                    error: (_, _) => _ExpenseDataAvailability(
                      label: 'Approval and payment',
                      message:
                          'Could not load expense approvers and payment details.',
                      onRetry: _retryFormContext,
                      retryKey: const Key('expense-form-context-retry'),
                    ),
                    data: (data) => Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        if (data.approvers.isEmpty)
                          _ExpenseDataAvailability(
                            label: 'Expense approver',
                            message:
                                'No eligible expense approvers are available. Ask an administrator to check the ERP approver and active user accounts.',
                            onRetry: _retryFormContext,
                            retryKey: const Key('expense-approver-retry'),
                          )
                        else
                          Container(
                            key: _approverFieldKey,
                            child: DropdownButtonFormField<ExpenseApprover>(
                              key: const Key('expense-approver'),
                              initialValue: _selectedApprover,
                              isExpanded: true,
                              decoration: InputDecoration(
                                labelText: 'Approver',
                                helperText:
                                    'Choose who will review this request.',
                                errorText: _approverError.isEmpty
                                    ? null
                                    : _approverError,
                              ),
                              items: [
                                for (final approver in data.approvers)
                                  DropdownMenuItem(
                                    value: approver,
                                    child: Text(
                                      approver.displayName,
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                    ),
                                  ),
                              ],
                              onChanged: (value) => setState(() {
                                _selectedApprover = value;
                                _approverError = '';
                                _submitError = '';
                              }),
                            ),
                          ),
                        if (!data.profileDestination.available &&
                            data.banks.isEmpty)
                          _ExpenseDataAvailability(
                            label: 'Payment destination',
                            message:
                                'No payment destination is available. Ask an administrator to check your ERP bank profile and bank list.',
                            onRetry: _retryFormContext,
                            retryKey: const Key('expense-payment-retry'),
                          )
                        else ...[
                          RadioListTile<ExpensePaymentMode>(
                            key: const Key('expense-payment-erp'),
                            value: ExpensePaymentMode.erpProfile,
                            contentPadding: EdgeInsets.zero,
                            // ignore: deprecated_member_use
                            groupValue: _destinationMode,
                            // ignore: deprecated_member_use
                            onChanged: data.profileDestination.available
                                ? (value) => setState(() {
                                    _destinationMode = value!;
                                    _submitError = '';
                                  })
                                : null,
                            title: const Text('Use ERP payment details'),
                            subtitle: Text(
                              data.profileDestination.available
                                  ? '${data.profileDestination.beneficiaryName} · ${data.profileDestination.bankName} · ${data.profileDestination.maskedAccountNumber}'
                                  : 'ERP bank profile is incomplete.',
                            ),
                          ),
                          if (data.banks.isNotEmpty)
                            RadioListTile<ExpensePaymentMode>(
                              key: const Key('expense-payment-custom'),
                              value: ExpensePaymentMode.expenseOverride,
                              contentPadding: EdgeInsets.zero,
                              // ignore: deprecated_member_use
                              groupValue: _destinationMode,
                              // ignore: deprecated_member_use
                              onChanged: (value) => setState(() {
                                _destinationMode = value!;
                                _submitError = '';
                              }),
                              title: const Text('Input custom payment details'),
                              subtitle: const Text(
                                'These details apply only to this expense.',
                              ),
                            ),
                        ],
                        if (_destinationMode ==
                                ExpensePaymentMode.expenseOverride &&
                            data.banks.isNotEmpty) ...[
                          DropdownButtonFormField<ExpenseBank>(
                            key: const Key('expense-bank'),
                            initialValue: _selectedBank,
                            isExpanded: true,
                            decoration: const InputDecoration(
                              labelText: 'Bank name',
                            ),
                            items: [
                              for (final bank in data.banks)
                                DropdownMenuItem(
                                  value: bank,
                                  child: Text(
                                    bank.bankName,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                ),
                            ],
                            onChanged: (value) => setState(() {
                              _selectedBank = value;
                              _submitError = '';
                            }),
                          ),
                          const SizedBox(height: 12),
                          TextField(
                            key: const Key('expense-account-number'),
                            controller: _accountNumber,
                            keyboardType: TextInputType.number,
                            inputFormatters: [
                              FilteringTextInputFormatter.digitsOnly,
                              LengthLimitingTextInputFormatter(30),
                            ],
                            autofillHints: const [],
                            onChanged: (_) => setState(() => _submitError = ''),
                            decoration: const InputDecoration(
                              labelText: 'Account number',
                            ),
                          ),
                          const SizedBox(height: 12),
                          TextField(
                            key: const Key('expense-beneficiary-name'),
                            controller: _beneficiaryName,
                            inputFormatters: [
                              LengthLimitingTextInputFormatter(150),
                            ],
                            onChanged: (_) => setState(() => _submitError = ''),
                            decoration: const InputDecoration(
                              labelText: 'Account name',
                              helperText:
                                  'ERP will verify these details when you submit.',
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 24),
          Text('Add expenses', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          categories.when(
            data: (items) => items.isEmpty
                ? _ExpenseDataAvailability(
                    label: 'Expense category',
                    message:
                        'No expense categories are available. Ask an administrator to check the ERP category list.',
                    onRetry: _retryCategories,
                    retryKey: const Key('expense-category-retry'),
                  )
                : DropdownButtonFormField<ExpenseCategory>(
                    key: const Key('expense-category'),
                    initialValue: _selectedCategory,
                    isExpanded: true,
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
                    onChanged: (value) => setState(() {
                      _selectedCategory = value;
                      _lineError = '';
                    }),
                  ),
            loading: () => const _ExpenseDataAvailability(
              label: 'Expense category',
              message: 'Loading expense categories…',
              loading: true,
            ),
            error: (_, _) => _ExpenseDataAvailability(
              label: 'Expense category',
              message: 'Could not load expense categories.',
              onRetry: _retryCategories,
              retryKey: const Key('expense-category-retry'),
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
                  onChanged: (_) {
                    if (_receiptAttachmentId == null) return;
                    setState(() {
                      _receiptAttachmentId = null;
                      _receiptFileName = '';
                    });
                  },
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
            onPressed: categoriesReady ? _addLine : null,
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
                            if ((item.receiptUrl != null &&
                                    item.receiptUrl!.trim().isNotEmpty) ||
                                (item.receiptAttachmentId != null &&
                                    item.receiptAttachmentId!
                                        .trim()
                                        .isNotEmpty))
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
          if (_items.isNotEmpty && categoriesReady && !itemCategoriesValid) ...[
            const SizedBox(height: 12),
            Text(
              'A saved expense uses a category that is no longer available. Remove it and add it again.',
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
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
                key: const Key('submit-expense-request'),
                onPressed:
                    _items.isEmpty ||
                        _saving ||
                        !requiredServerDataReady ||
                        !itemCategoriesValid
                    ? null
                    : _submit,
                icon: Icons.check_rounded,
                label: _saving ? 'Submitting…' : 'Submit request',
              ),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                key: const Key('save-expense-draft'),
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

class _ExpenseDataAvailability extends StatelessWidget {
  const _ExpenseDataAvailability({
    required this.label,
    required this.message,
    this.loading = false,
    this.onRetry,
    this.retryKey,
  });

  final String label;
  final String message;
  final bool loading;
  final VoidCallback? onRetry;
  final Key? retryKey;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      container: true,
      liveRegion: !loading,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          InputDecorator(
            isEmpty: true,
            decoration: InputDecoration(labelText: label),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    message,
                    style: TextStyle(color: Theme.of(context).hintColor),
                  ),
                ),
                if (loading) ...[
                  const SizedBox(width: 12),
                  const SizedBox.square(
                    dimension: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                ],
              ],
            ),
          ),
          if (onRetry != null)
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                key: retryKey,
                onPressed: onRetry,
                icon: const Icon(Icons.refresh_rounded),
                label: const Text('Retry'),
              ),
            ),
        ],
      ),
    );
  }
}

class _WorkOrderAvailability extends StatelessWidget {
  const _WorkOrderAvailability({
    required this.message,
    this.loading = false,
    this.onRetry,
  });

  final String message;
  final bool loading;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        InputDecorator(
          isEmpty: true,
          decoration: const InputDecoration(labelText: 'Work order'),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  message,
                  style: TextStyle(color: Theme.of(context).hintColor),
                ),
              ),
              if (loading) ...[
                const SizedBox(width: 12),
                const SizedBox.square(
                  dimension: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              ],
            ],
          ),
        ),
        if (onRetry != null)
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh_rounded),
              label: const Text('Retry'),
            ),
          ),
      ],
    );
  }
}

class _WorkOrderPickerSheet extends StatefulWidget {
  const _WorkOrderPickerSheet({required this.jobs, this.selectedId});

  final List<JobSummary> jobs;
  final String? selectedId;

  @override
  State<_WorkOrderPickerSheet> createState() => _WorkOrderPickerSheetState();
}

class _WorkOrderPickerSheetState extends State<_WorkOrderPickerSheet> {
  final _search = TextEditingController();
  String _query = '';

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final query = _query.trim().toLowerCase();
    final matches = query.isEmpty
        ? widget.jobs
        : widget.jobs
              .where(
                (job) =>
                    job.title.toLowerCase().contains(query) ||
                    job.id.toLowerCase().contains(query) ||
                    job.statusPresentation.label.toLowerCase().contains(query),
              )
              .toList();
    final bottomInset = MediaQuery.viewInsetsOf(context).bottom;
    return AnimatedPadding(
      duration: const Duration(milliseconds: 180),
      padding: EdgeInsets.only(bottom: bottomInset),
      child: FractionallySizedBox(
        heightFactor: 0.82,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      'Select work order',
                      style: Theme.of(context).textTheme.titleLarge?.copyWith(
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                  IconButton(
                    tooltip: 'Close',
                    onPressed: () => Navigator.of(context).pop(),
                    icon: const Icon(Icons.close_rounded),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              TextField(
                key: const Key('expense-work-order-search'),
                controller: _search,
                autofocus: false,
                textInputAction: TextInputAction.search,
                decoration: const InputDecoration(
                  labelText: 'Search assigned work orders',
                  prefixIcon: Icon(Icons.search_rounded),
                ),
                onChanged: (value) => setState(() => _query = value),
              ),
              const SizedBox(height: 12),
              Expanded(
                child: matches.isEmpty
                    ? const Center(
                        child: Text('No matching work orders found.'),
                      )
                    : ListView.separated(
                        itemCount: matches.length,
                        separatorBuilder: (_, _) => const Divider(height: 1),
                        itemBuilder: (context, index) {
                          final job = matches[index];
                          final selected = job.id == widget.selectedId;
                          return ListTile(
                            key: Key('expense-work-order-option-${job.id}'),
                            selected: selected,
                            contentPadding: const EdgeInsets.symmetric(
                              horizontal: 8,
                              vertical: 4,
                            ),
                            title: Text(
                              job.title,
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                            ),
                            subtitle: Text(
                              '${job.id} · ${job.statusPresentation.label}',
                            ),
                            trailing: selected
                                ? const Icon(Icons.check_rounded)
                                : null,
                            onTap: () => Navigator.of(context).pop(job),
                          );
                        },
                      ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

void _invalidateExpenseProjections(WidgetRef ref) {
  ref
    ..invalidate(expenseRequestsProvider)
    ..invalidate(managerExpensesProvider)
    ..invalidate(managerSummaryProvider);
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
    if (detail is Map) {
      final message = detail['message'];
      if (message is String && message.trim().isNotEmpty) {
        return message.trim();
      }
    }
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

List<ExpenseItemDraft> _expenseDraftItems(Object? raw) {
  if (raw is! List) return const [];
  return raw.whereType<Map>().map((item) {
    return ExpenseItemDraft.fromDraftJson(item.cast<String, dynamic>());
  }).toList();
}
