import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';

import '../../app/theme.dart';
import '../../app/widgets/status_pill.dart';
import '../expenses/expense_models.dart';
import 'manager_providers.dart';

class ManagerDashboardScreen extends ConsumerWidget {
  const ManagerDashboardScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final profile = ref.watch(managerProfileProvider);
    final summary = ref.watch(managerSummaryProvider);
    return Scaffold(
      appBar: AppBar(
        title: Text(
          profile.valueOrNull == null
              ? 'Field manager'
              : 'Hi, ${_firstName(profile.valueOrNull!.name)}',
        ),
        actions: [
          IconButton(
            tooltip: 'Refresh',
            onPressed: () {
              ref
                ..invalidate(managerProfileProvider)
                ..invalidate(managerSummaryProvider)
                ..invalidate(managerTechniciansProvider)
                ..invalidate(managerJobsProvider)
                ..invalidate(managerExpensesProvider);
            },
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref
            ..invalidate(managerSummaryProvider)
            ..invalidate(managerTechniciansProvider)
            ..invalidate(managerJobsProvider)
            ..invalidate(managerExpensesProvider);
        },
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          children: [
            Text(
              'Operations dashboard',
              style: Theme.of(
                context,
              ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w800),
            ),
            const SizedBox(height: 6),
            Text(
              'Dispatch load, active technicians, and approvals.',
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: AppColors.subdued(context),
              ),
            ),
            const SizedBox(height: 18),
            summary.when(
              data: (data) => Column(
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: _MetricCard(
                          icon: Icons.engineering_outlined,
                          label: 'Live techs',
                          value: '${data.techniciansLive}',
                          detail: '${data.techniciansSharing} sharing',
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: _MetricCard(
                          icon: Icons.assignment_outlined,
                          label: 'Open jobs',
                          value: '${data.openJobs}',
                          detail: '${data.unassignedJobs} unassigned',
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 10),
                  Row(
                    children: [
                      Expanded(
                        child: _MetricCard(
                          icon: Icons.receipt_long_outlined,
                          label: 'Approvals',
                          value: '${data.pendingExpenses}',
                          detail: 'pending expenses',
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: _MetricCard(
                          icon: Icons.groups_outlined,
                          label: 'Team',
                          value: '${data.techniciansTotal}',
                          detail: 'active profiles',
                        ),
                      ),
                    ],
                  ),
                ],
              ),
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (_, _) =>
                  const _InlineError(message: 'Could not load manager summary'),
            ),
            const SizedBox(height: 18),
            const _QuickActions(),
          ],
        ),
      ),
    );
  }
}

class ManagerTeamMapScreen extends ConsumerWidget {
  const ManagerTeamMapScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final technicians = ref.watch(managerTechniciansProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Team location')),
      body: RefreshIndicator(
        onRefresh: () async => ref.invalidate(managerTechniciansProvider),
        child: technicians.when(
          data: (items) => ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.all(16),
            children: [
              _TeamMapPanel(technicians: items),
              const SizedBox(height: 16),
              Text(
                'Technicians',
                style: Theme.of(
                  context,
                ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w800),
              ),
              const SizedBox(height: 8),
              if (items.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 48),
                  child: Center(child: Text('No active technician profiles')),
                )
              else
                for (final tech in items) _TechnicianTile(technician: tech),
            ],
          ),
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (_, _) =>
              const Center(child: Text('Could not load technicians')),
        ),
      ),
    );
  }
}

class ManagerDispatchScreen extends ConsumerWidget {
  const ManagerDispatchScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final jobs = ref.watch(managerJobsProvider);
    final technicians = ref.watch(managerTechniciansProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Dispatch')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref
            ..invalidate(managerJobsProvider)
            ..invalidate(managerTechniciansProvider);
        },
        child: jobs.when(
          data: (items) => ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.all(16),
            children: [
              Text(
                'Open work orders',
                style: Theme.of(
                  context,
                ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w800),
              ),
              const SizedBox(height: 8),
              if (items.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 48),
                  child: Center(child: Text('No open jobs')),
                )
              else
                for (final job in items)
                  _DispatchJobCard(
                    job: job,
                    technicians: technicians.valueOrNull ?? const [],
                  ),
            ],
          ),
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (_, _) => const Center(child: Text('Could not load jobs')),
        ),
      ),
    );
  }
}

class ManagerExpenseReviewScreen extends ConsumerStatefulWidget {
  const ManagerExpenseReviewScreen({super.key});

  @override
  ConsumerState<ManagerExpenseReviewScreen> createState() =>
      _ManagerExpenseReviewScreenState();
}

class _ManagerExpenseReviewScreenState
    extends ConsumerState<ManagerExpenseReviewScreen> {
  List<ExpenseRequest>? _lastLoadedExpenses;
  final _resolvedExpenseIds = <String>{};
  late final ProviderSubscription<AsyncValue<List<ExpenseRequest>>>
  _expensesSubscription;

  @override
  void initState() {
    super.initState();
    _expensesSubscription = ref.listenManual(managerExpensesProvider, (
      _,
      next,
    ) {
      final items = next.valueOrNull;
      if (items == null || !mounted) return;
      setState(() => _lastLoadedExpenses = items);
    });
  }

  @override
  void dispose() {
    _expensesSubscription.close();
    super.dispose();
  }

  void _markResolved(String id) {
    if (!mounted) return;
    setState(() => _resolvedExpenseIds.add(id));
  }

  @override
  Widget build(BuildContext context) {
    final expenses = ref.watch(managerExpensesProvider);
    final latestItems = expenses.valueOrNull ?? _lastLoadedExpenses;
    final visibleItems = latestItems
        ?.where((item) => !_resolvedExpenseIds.contains(item.id))
        .toList();

    return Scaffold(
      appBar: AppBar(title: const Text('Approvals')),
      body: RefreshIndicator(
        onRefresh: () async => ref.invalidate(managerExpensesProvider),
        child: switch (visibleItems) {
          final items? => ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.all(16),
            children: [
              if (expenses.hasError) ...[
                _ApprovalRefreshError(
                  onRetry: () => ref.invalidate(managerExpensesProvider),
                ),
                const SizedBox(height: 12),
              ],
              Text(
                'Pending expenses',
                style: Theme.of(
                  context,
                ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w800),
              ),
              const SizedBox(height: 8),
              if (items.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 48),
                  child: Center(child: Text('No expense approvals pending')),
                )
              else
                for (final request in items)
                  _ExpenseApprovalCard(
                    request: request,
                    onResolved: _markResolved,
                  ),
            ],
          ),
          null when expenses.isLoading => const Center(
            child: CircularProgressIndicator(),
          ),
          null => ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.all(16),
            children: [
              _ApprovalRefreshError(
                initialLoad: true,
                onRetry: () => ref.invalidate(managerExpensesProvider),
              ),
            ],
          ),
        },
      ),
    );
  }
}

class _ApprovalRefreshError extends StatelessWidget {
  const _ApprovalRefreshError({
    required this.onRetry,
    this.initialLoad = false,
  });

  final VoidCallback onRetry;
  final bool initialLoad;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Row(
          children: [
            const Icon(Icons.cloud_off_outlined),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                initialLoad
                    ? 'Could not load expense approvals.'
                    : 'Could not refresh approvals. Showing the last loaded results.',
              ),
            ),
            TextButton(onPressed: onRetry, child: const Text('Retry')),
          ],
        ),
      ),
    );
  }
}

class _QuickActions extends StatelessWidget {
  const _QuickActions();

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _ActionTile(
          icon: Icons.map_outlined,
          title: 'Team location',
          subtitle: 'Live sharing status and current work context',
          onTap: () => context.go('/map'),
        ),
        const SizedBox(height: 10),
        _ActionTile(
          icon: Icons.assignment_ind_outlined,
          title: 'Dispatch queue',
          subtitle: 'Assign open jobs to available technicians',
          onTap: () => context.go('/schedule'),
        ),
      ],
    );
  }
}

class _MetricCard extends StatelessWidget {
  const _MetricCard({
    required this.icon,
    required this.label,
    required this.value,
    required this.detail,
  });

  final IconData icon;
  final String label;
  final String value;
  final String detail;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(icon, color: AppColors.primary),
            const SizedBox(height: 14),
            Text(
              value,
              style: Theme.of(
                context,
              ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w900),
            ),
            const SizedBox(height: 4),
            Text(label, style: const TextStyle(fontWeight: FontWeight.w700)),
            const SizedBox(height: 2),
            Text(
              detail,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: AppColors.subdued(context),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ActionTile extends StatelessWidget {
  const _ActionTile({
    required this.icon,
    required this.title,
    required this.subtitle,
    this.onTap,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ListTile(
        leading: Icon(icon, color: AppColors.primary),
        title: Text(title),
        subtitle: Text(subtitle),
        trailing: const Icon(Icons.chevron_right),
        onTap: onTap,
      ),
    );
  }
}

class _TeamMapPanel extends StatelessWidget {
  const _TeamMapPanel({required this.technicians});

  final List<ManagerTechnician> technicians;

  @override
  Widget build(BuildContext context) {
    final live = technicians
        .where(
          (tech) =>
              tech.isLive && tech.latitude != null && tech.longitude != null,
        )
        .toList();
    return Card(
      clipBehavior: Clip.antiAlias,
      child: SizedBox(
        height: 260,
        child: Stack(
          children: [
            Positioned.fill(
              child: CustomPaint(
                painter: _MapGridPainter(
                  color: AppColors.border(context),
                  fill: AppColors.softTeal(context),
                ),
              ),
            ),
            if (live.isEmpty)
              const Center(
                child: Text('No live locations in the current window'),
              )
            else
              for (final tech in live)
                _MapDot(technician: tech, offset: _relativeOffset(tech, live)),
            Positioned(
              left: 12,
              top: 12,
              child: DecoratedBox(
                decoration: BoxDecoration(
                  color: AppColors.surface(context).withValues(alpha: 0.92),
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: AppColors.border(context)),
                ),
                child: Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 8,
                  ),
                  child: Text(
                    '${live.length} live',
                    style: const TextStyle(fontWeight: FontWeight.w800),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _MapGridPainter extends CustomPainter {
  const _MapGridPainter({required this.color, required this.fill});

  final Color color;
  final Color fill;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = fill
      ..style = PaintingStyle.fill;
    canvas.drawRect(Offset.zero & size, paint);
    final linePaint = Paint()
      ..color = color.withValues(alpha: 0.65)
      ..strokeWidth = 1;
    for (var x = 0.0; x <= size.width; x += 42) {
      canvas.drawLine(Offset(x, 0), Offset(x, size.height), linePaint);
    }
    for (var y = 0.0; y <= size.height; y += 42) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), linePaint);
    }
  }

  @override
  bool shouldRepaint(covariant _MapGridPainter oldDelegate) =>
      oldDelegate.color != color || oldDelegate.fill != fill;
}

class _MapDot extends StatelessWidget {
  const _MapDot({required this.technician, required this.offset});

  final ManagerTechnician technician;
  final Offset offset;

  @override
  Widget build(BuildContext context) {
    return Positioned(
      left: offset.dx,
      top: offset.dy,
      child: Tooltip(
        message: technician.name,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 22,
              height: 22,
              decoration: BoxDecoration(
                color: AppColors.semanticPositive,
                shape: BoxShape.circle,
                border: Border.all(color: AppColors.panel, width: 3),
                boxShadow: const [
                  BoxShadow(
                    color: Color(0x33000000),
                    blurRadius: 8,
                    offset: Offset(0, 3),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 4),
            DecoratedBox(
              decoration: BoxDecoration(
                color: AppColors.surface(context).withValues(alpha: 0.9),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3),
                child: Text(
                  _firstName(technician.name),
                  style: Theme.of(context).textTheme.labelSmall,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _TechnicianTile extends StatelessWidget {
  const _TechnicianTile({required this.technician});

  final ManagerTechnician technician;

  @override
  Widget build(BuildContext context) {
    final color = technician.isLive
        ? AppColors.semanticPositive
        : technician.locationSharingEnabled
        ? AppColors.accent
        : AppColors.subdued(context);
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: Icon(Icons.person_pin_circle_outlined, color: color),
        title: Text(technician.name),
        subtitle: Text(
          [
            technician.title,
            technician.region,
            technician.status.replaceAll('_', ' '),
            if (technician.activeWorkOrderTitle != null)
              technician.activeWorkOrderTitle,
          ].whereType<String>().where((value) => value.isNotEmpty).join(' · '),
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
        ),
        trailing: _StatusPill(
          label: technician.isLive ? 'Live' : 'Idle',
          color: color,
        ),
      ),
    );
  }
}

class _DispatchJobCard extends ConsumerWidget {
  const _DispatchJobCard({required this.job, required this.technicians});

  final ManagerJob job;
  final List<ManagerTechnician> technicians;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final canUnassign = job.assignmentQueueId != null;
    final time = job.scheduledStart == null
        ? 'Unscheduled'
        : DateFormat('d MMM, HH:mm').format(job.scheduledStart!.toLocal());
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                StatusPill(job.statusPresentation),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    time,
                    textAlign: TextAlign.right,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 10),
            Text(
              job.title,
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w800),
            ),
            const SizedBox(height: 6),
            Text(
              [job.workType, job.priority, job.subscriberLabel, job.addressText]
                  .whereType<String>()
                  .where((value) => value.isNotEmpty)
                  .join(' · '),
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: AppColors.subdued(context),
              ),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: Text(
                    job.assignedToLabel == null
                        ? 'Unassigned'
                        : 'Assigned to ${job.assignedToLabel}',
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                ),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  onPressed: canUnassign
                      ? () => _unassign(context, ref, job)
                      : technicians.isEmpty
                      ? null
                      : () => _assign(context, ref, job, technicians),
                  icon: Icon(
                    canUnassign
                        ? Icons.person_remove_outlined
                        : Icons.assignment_ind_outlined,
                  ),
                  label: Text(canUnassign ? 'Unassign' : 'Assign'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _ExpenseApprovalCard extends ConsumerStatefulWidget {
  const _ExpenseApprovalCard({required this.request, required this.onResolved});

  final ExpenseRequest request;
  final ValueChanged<String> onResolved;

  @override
  ConsumerState<_ExpenseApprovalCard> createState() =>
      _ExpenseApprovalCardState();
}

class _ExpenseApprovalCardState extends ConsumerState<_ExpenseApprovalCard> {
  bool _busy = false;

  Future<void> _approve() async {
    await _run(
      () async {
        final result = await ref
            .read(managerRepositoryProvider)
            .approveExpense(widget.request.id);
        return expenseApprovalMessage(result);
      },
      failureMessage:
          'Expense was not approved; ERP sync is unavailable. Please retry.',
    );
  }

  Future<void> _reject() async {
    final reason = await _rejectReason(context);
    if (reason == null || reason.trim().isEmpty) return;
    await _run(() async {
      await ref
          .read(managerRepositoryProvider)
          .rejectExpense(widget.request.id, reason);
      return 'Expense rejected';
    }, failureMessage: 'Could not reject expense');
  }

  Future<void> _run(
    Future<String> Function() action, {
    required String failureMessage,
  }) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final message = await action();
      widget.onResolved(widget.request.id);
      ref
        ..invalidate(managerExpensesProvider)
        ..invalidate(managerSummaryProvider);
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(message)));
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(failureMessage)));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final request = widget.request;
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.receipt_long_outlined, color: AppColors.primary),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    request.purpose ?? request.displayNumber,
                    style: Theme.of(context).textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ),
                Text(
                  _money(request.currency, request.totalAmount),
                  style: const TextStyle(fontWeight: FontWeight.w800),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              [
                request.displayNumber,
                request.workOrderId == null
                    ? null
                    : 'WO ${request.workOrderId}',
                '${request.items.length} item${request.items.length == 1 ? '' : 's'}',
              ].whereType<String>().join(' · '),
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: AppColors.subdued(context),
              ),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton.icon(
                    onPressed: _busy ? null : _reject,
                    icon: const Icon(Icons.close),
                    label: const Text('Reject'),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: FilledButton.icon(
                    onPressed: _busy ? null : _approve,
                    icon: const Icon(Icons.check),
                    label: const Text('Approve'),
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

String expenseApprovalMessage(ExpenseApprovalResult result) {
  return switch (result.erpSyncStatus) {
    'pending' || 'sent' => 'Expense approved; waiting for ERP',
    'accepted' => 'Expense approved and synced to ERP',
    'rejected' ||
    'dead' ||
    'not_configured' ||
    'not_queued' => 'Expense approved; ERP sync needs attention',
    _ => 'Expense approved; waiting for ERP',
  };
}

class _StatusPill extends StatelessWidget {
  const _StatusPill({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: color.withValues(alpha: 0.35)),
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        child: Text(
          label,
          style: Theme.of(context).textTheme.labelSmall?.copyWith(
            color: color,
            fontWeight: FontWeight.w800,
          ),
        ),
      ),
    );
  }
}

class _InlineError extends StatelessWidget {
  const _InlineError({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 32),
      child: Center(child: Text(message)),
    );
  }
}

Future<void> _assign(
  BuildContext context,
  WidgetRef ref,
  ManagerJob job,
  List<ManagerTechnician> technicians,
) async {
  final selected = await showModalBottomSheet<ManagerTechnician>(
    context: context,
    showDragHandle: true,
    builder: (context) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        children: [
          Text(
            'Assign technician',
            style: Theme.of(
              context,
            ).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w800),
          ),
          const SizedBox(height: 8),
          for (final tech in technicians)
            ListTile(
              leading: Icon(
                tech.isLive ? Icons.radio_button_checked : Icons.person_outline,
                color: tech.isLive ? AppColors.semanticPositive : null,
              ),
              title: Text(tech.name),
              subtitle: Text(
                [
                      tech.region,
                      tech.status.replaceAll('_', ' '),
                      tech.activeWorkOrderTitle,
                    ]
                    .whereType<String>()
                    .where((value) => value.isNotEmpty)
                    .join(' · '),
              ),
              onTap: () => Navigator.of(context).pop(tech),
            ),
        ],
      ),
    ),
  );
  if (selected == null) return;
  try {
    await ref
        .read(managerRepositoryProvider)
        .assignJob(jobId: job.id, personId: selected.personId);
    ref
      ..invalidate(managerJobsProvider)
      ..invalidate(managerSummaryProvider)
      ..invalidate(managerTechniciansProvider);
    if (context.mounted) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('Assigned to ${selected.name}')));
    }
  } catch (_) {
    if (context.mounted) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Could not assign job')));
    }
  }
}

Future<void> _unassign(
  BuildContext context,
  WidgetRef ref,
  ManagerJob job,
) async {
  final assignmentQueueId = job.assignmentQueueId;
  if (assignmentQueueId == null) return;
  final confirmed = await showDialog<bool>(
    context: context,
    builder: (context) => AlertDialog(
      title: const Text('Unassign technician?'),
      content: Text(
        'Remove ${job.assignedToLabel ?? 'the assigned technician'} from '
        '${job.title}?',
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text('Unassign'),
        ),
      ],
    ),
  );
  if (confirmed != true || !context.mounted) return;
  try {
    await ref
        .read(managerRepositoryProvider)
        .unassignJob(
          assignmentQueueId: assignmentQueueId,
          reason: 'Manager unassigned technician from mobile dispatch',
        );
    ref
      ..invalidate(managerJobsProvider)
      ..invalidate(managerSummaryProvider)
      ..invalidate(managerTechniciansProvider);
    if (context.mounted) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Technician unassigned')));
    }
  } catch (_) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Could not unassign technician')),
      );
    }
  }
}

Future<String?> _rejectReason(BuildContext context) async {
  return showDialog<String>(
    context: context,
    builder: (_) => const _RejectExpenseDialog(),
  );
}

class _RejectExpenseDialog extends StatefulWidget {
  const _RejectExpenseDialog();

  @override
  State<_RejectExpenseDialog> createState() => _RejectExpenseDialogState();
}

class _RejectExpenseDialogState extends State<_RejectExpenseDialog> {
  final _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _submit() {
    FocusManager.instance.primaryFocus?.unfocus();
    Navigator.of(context).pop(_controller.text);
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Reject expense'),
      content: TextField(
        key: const Key('expense-rejection-reason'),
        controller: _controller,
        autofocus: true,
        maxLines: 3,
        textInputAction: TextInputAction.done,
        onSubmitted: (_) => _submit(),
        decoration: const InputDecoration(labelText: 'Reason'),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(onPressed: _submit, child: const Text('Reject')),
      ],
    );
  }
}

Offset _relativeOffset(ManagerTechnician tech, List<ManagerTechnician> live) {
  final lats = live.map((item) => item.latitude!).toList();
  final lngs = live.map((item) => item.longitude!).toList();
  final minLat = lats.reduce(math.min);
  final maxLat = lats.reduce(math.max);
  final minLng = lngs.reduce(math.min);
  final maxLng = lngs.reduce(math.max);
  final latRange = (maxLat - minLat).abs() < 0.00001 ? 1 : maxLat - minLat;
  final lngRange = (maxLng - minLng).abs() < 0.00001 ? 1 : maxLng - minLng;
  final x = ((tech.longitude! - minLng) / lngRange).clamp(0.0, 1.0);
  final y = (1 - ((tech.latitude! - minLat) / latRange)).clamp(0.0, 1.0);
  return Offset(26 + x * 250, 54 + y * 150);
}

String _firstName(String name) {
  final trimmed = name.trim();
  if (trimmed.isEmpty) return 'Manager';
  return trimmed.split(RegExp(r'\s+')).first;
}

String _money(String? currency, double amount) {
  final code = (currency == null || currency.isEmpty) ? 'NGN' : currency;
  final symbol = '$code ';
  return '$symbol${NumberFormat.decimalPattern().format(amount)}';
}
