import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../../models/invoice.dart';
import '../../models/ledger.dart';
import '../../providers/data_providers.dart';
import '../../widgets/account_avatar_button.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/offline_banner.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';
import 'invoice_pay_button.dart';

class InvoicesScreen extends ConsumerStatefulWidget {
  const InvoicesScreen({super.key});

  @override
  ConsumerState<InvoicesScreen> createState() => _InvoicesScreenState();
}

class _InvoicesScreenState extends ConsumerState<InvoicesScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tabController;
  int _selectedTab = 0;

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 3, vsync: this)
      ..addListener(_handleTabChanged);
  }

  void _handleTabChanged() {
    if (!_tabController.indexIsChanging &&
        _selectedTab != _tabController.index) {
      setState(() => _selectedTab = _tabController.index);
    }
  }

  @override
  void dispose() {
    _tabController
      ..removeListener(_handleTabChanged)
      ..dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final invoices = ref.watch(invoicesProvider);
    final payments = ref.watch(paymentsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Billing'),
        actions: [
          IconButton(
            tooltip: 'Pay by bank transfer',
            icon: const Icon(Icons.account_balance_outlined),
            onPressed: () => context.go('/billing/transfer-proofs'),
          ),
          const AccountAvatarButton(),
        ],
        bottom: TabBar(
          controller: _tabController,
          tabs: const [
            Tab(text: 'Invoices'),
            Tab(text: 'Payments'),
            Tab(text: 'Activity'),
          ],
        ),
      ),
      floatingActionButton: _selectedTab == 1
          ? FloatingActionButton.extended(
              tooltip: 'Make a payment',
              onPressed: () => context.push('/topup'),
              icon: const Icon(Icons.add_card_outlined),
              label: const Text('Make payment'),
            )
          : null,
      body: Column(
        children: [
          const OfflineBanner(),
          const _BillingPrimaryAction(),
          Expanded(
            child: TabBarView(
              controller: _tabController,
              children: [
                RefreshIndicator(
                  onRefresh: () async {
                    ref.invalidate(invoicesProvider);
                    await ref.read(invoicesProvider.future);
                  },
                  child: AsyncValueView(
                    value: invoices,
                    onRetry: () => ref.invalidate(invoicesProvider),
                    skeleton: const ListSkeleton(),
                    data: (page) {
                      final all = page.items;
                      if (all.isEmpty) {
                        return const _ScrollableEmpty(
                          icon: Icons.receipt_long_outlined,
                          message: 'No invoices yet.',
                        );
                      }
                      final filter = ref.watch(invoiceFilterProvider);
                      final outstanding = all
                          .where((i) => !i.isPaid)
                          .fold<double>(0, (sum, i) => sum + i.balanceDue);
                      final currency = all.first.currency;
                      final items = all.where(filter.test).toList();
                      return ListView(
                        padding: const EdgeInsets.all(12),
                        children: [
                          if (outstanding > 0) ...[
                            _OutstandingHeader(
                                amount: outstanding, currency: currency),
                            const SizedBox(height: 8),
                          ],
                          _InvoiceFilterBar(
                            selected: filter,
                            counts: {
                              for (final f in InvoiceFilter.values)
                                f: all.where(f.test).length,
                            },
                            onChanged: (f) => ref
                                .read(invoiceFilterProvider.notifier)
                                .state = f,
                          ),
                          const SizedBox(height: 12),
                          if (items.isEmpty)
                            Padding(
                              padding: const EdgeInsets.only(top: 64),
                              child: EmptyState(
                                icon: Icons.filter_alt_off_outlined,
                                message:
                                    'No ${filter.label.toLowerCase()} invoices.',
                              ),
                            )
                          else
                            for (final inv in items) ...[
                              _InvoiceTile(invoice: inv),
                              const SizedBox(height: 8),
                            ],
                        ],
                      );
                    },
                  ),
                ),
                RefreshIndicator(
                  onRefresh: () async {
                    ref.invalidate(paymentsProvider);
                    await ref.read(paymentsProvider.future);
                  },
                  child: AsyncValueView(
                    value: payments,
                    onRetry: () => ref.invalidate(paymentsProvider),
                    skeleton: const ListSkeleton(hasLeading: true),
                    data: (page) {
                      if (page.items.isEmpty) {
                        return const _ScrollableEmpty(
                          icon: Icons.payments_outlined,
                          message: 'No payments recorded.',
                        );
                      }
                      return ListView.separated(
                        padding: const EdgeInsets.all(12),
                        itemCount: page.items.length,
                        separatorBuilder: (_, __) => const SizedBox(height: 8),
                        itemBuilder: (_, i) {
                          final p = page.items[i];
                          return Card(
                            margin: EdgeInsets.zero,
                            child: ListTile(
                              key: ValueKey('payment-${p.id}'),
                              leading: const Icon(Icons.payments_outlined),
                              title: Text(Fmt.money(p.amount, p.currency)),
                              subtitle:
                                  Text(Fmt.dateTime(p.paidAt ?? p.createdAt)),
                              trailing: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  StatusChip.fromPresentation(
                                    p.statusPresentation,
                                  ),
                                  const SizedBox(width: 4),
                                  const Icon(Icons.chevron_right),
                                ],
                              ),
                              onTap: () =>
                                  context.push('/billing/payments/${p.id}'),
                            ),
                          );
                        },
                      );
                    },
                  ),
                ),
                RefreshIndicator(
                  onRefresh: () async {
                    ref.invalidate(ledgerProvider);
                    ref.invalidate(balanceProvider);
                    await ref.read(ledgerProvider.future);
                  },
                  child: AsyncValueView(
                    value: ref.watch(ledgerProvider),
                    onRetry: () => ref.invalidate(ledgerProvider),
                    skeleton: const ListSkeleton(hasLeading: true),
                    data: (page) {
                      final balance = ref.watch(balanceProvider);
                      return ListView(
                        padding: const EdgeInsets.all(12),
                        children: [
                          balance.maybeWhen(
                            data: (b) => _BalanceCard(balance: b),
                            orElse: () => const SizedBox.shrink(),
                          ),
                          const SizedBox(height: 8),
                          if (page.items.isEmpty)
                            const Padding(
                              padding: EdgeInsets.only(top: 80),
                              child: EmptyState(
                                icon: Icons.receipt_long_outlined,
                                message: 'No account activity yet.',
                              ),
                            )
                          else
                            for (final t in page.items) ...[
                              _LedgerTile(txn: t),
                              const SizedBox(height: 8),
                            ],
                        ],
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Account-level payment entry point. Invoice rows keep their contextual Pay
/// action, while this remains available across every Billing tab and empty
/// state for customers who want to add credit before an invoice is due.
class _BillingPrimaryAction extends StatelessWidget {
  const _BillingPrimaryAction();

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 12, 12, 0),
      child: SizedBox(
        width: double.infinity,
        child: FilledButton.icon(
          key: const ValueKey('billing-add-funds'),
          onPressed: () => context.push('/topup'),
          icon: const Icon(Icons.account_balance_wallet_outlined),
          label: const Text('Add funds / Pay'),
        ),
      ),
    );
  }
}

/// Total still owed across unpaid invoices — the number a customer opens the
/// Invoices tab to find.
class _OutstandingHeader extends StatelessWidget {
  const _OutstandingHeader({required this.amount, required this.currency});
  final double amount;
  final String currency;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      margin: EdgeInsets.zero,
      color: scheme.errorContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Expanded(
              child: Text('Outstanding balance',
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                        color: scheme.onErrorContainer,
                      )),
            ),
            const SizedBox(width: 12),
            // Scale the figure down rather than letting it overflow — same
            // idiom as the dashboard stat cards (a cut-off amount is worse
            // than a smaller one).
            Flexible(
              child: FittedBox(
                fit: BoxFit.scaleDown,
                child: Text(
                  Fmt.money(amount, currency),
                  maxLines: 1,
                  style: Theme.of(context).textTheme.titleLarge?.copyWith(
                        color: scheme.onErrorContainer,
                        fontWeight: FontWeight.w700,
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

/// Status filter chips above the invoices list.
class _InvoiceFilterBar extends StatelessWidget {
  const _InvoiceFilterBar({
    required this.selected,
    required this.onChanged,
    this.counts = const {},
  });
  final InvoiceFilter selected;
  final ValueChanged<InvoiceFilter> onChanged;
  final Map<InvoiceFilter, int> counts;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 8,
      children: [
        for (final f in InvoiceFilter.values)
          ChoiceChip(
            label:
                Text(counts[f] != null ? '${f.label} (${counts[f]})' : f.label),
            selected: f == selected,
            onSelected: (_) => onChanged(f),
          ),
      ],
    );
  }
}

/// One invoice row: number, due date, amount + status, and an inline Pay action
/// on anything still owing so the customer can pay without opening the detail.
class _InvoiceTile extends StatelessWidget {
  const _InvoiceTile({required this.invoice});
  final Invoice invoice;

  @override
  Widget build(BuildContext context) {
    final inv = invoice;
    return Card(
      margin: EdgeInsets.zero,
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => context.go('/billing/invoices/${inv.id}'),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      inv.invoiceNumber ?? 'Invoice ${inv.id.substring(0, 8)}',
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                    const SizedBox(height: 2),
                    Text('Due ${Fmt.date(inv.dueAt)}',
                        style: Theme.of(context).textTheme.bodySmall),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Text(Fmt.money(inv.total, inv.currency),
                      style: const TextStyle(fontWeight: FontWeight.w600)),
                  const SizedBox(height: 4),
                  StatusChip.fromPresentation(inv.statusPresentation),
                ],
              ),
              if (!inv.isPaid) ...[
                const SizedBox(width: 12),
                InvoicePayButton(invoice: inv, compact: true),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

/// Wallet/credit balance header for the Activity tab.
class _BalanceCard extends StatelessWidget {
  const _BalanceCard({required this.balance});
  final AccountBalance balance;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final (label, color) = balance.owes
        ? ('Balance due', scheme.error)
        : balance.inCredit
            ? ('Account credit', context.semantic.success)
            : ('Balance', scheme.onSurface);
    return Card(
      color: scheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Expanded(
              child:
                  Text(label, style: Theme.of(context).textTheme.titleMedium),
            ),
            const SizedBox(width: 12),
            Flexible(
              child: FittedBox(
                fit: BoxFit.scaleDown,
                child: Text(
                  Fmt.money(balance.creditBalance.abs(), balance.currency),
                  maxLines: 1,
                  style: Theme.of(context)
                      .textTheme
                      .titleLarge
                      ?.copyWith(color: color, fontWeight: FontWeight.w700),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// One account ledger row: credits (payments/refunds) are green +, debits
/// (charges) are red −.
class _LedgerTile extends StatelessWidget {
  const _LedgerTile({required this.txn});
  final LedgerTxn txn;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final credit = txn.isCredit;
    final color = credit ? context.semantic.success : scheme.error;
    final sign = credit ? '+' : '−';
    return Card(
      margin: EdgeInsets.zero,
      child: ListTile(
        key: ValueKey('activity-${txn.id}'),
        leading: Icon(
          credit ? Icons.south_west : Icons.north_east,
          color: color,
        ),
        title: Text(txn.title, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(Fmt.dateTime(txn.occurredAt)),
        trailing: Text(
          '$sign${Fmt.money(txn.amount, txn.currency)}',
          style: TextStyle(color: color, fontWeight: FontWeight.w600),
        ),
        onTap: () => context.push('/billing/activity/${txn.id}'),
      ),
    );
  }
}

/// Empty state that still scrolls so pull-to-refresh works.
class _ScrollableEmpty extends StatelessWidget {
  const _ScrollableEmpty({required this.icon, required this.message});
  final IconData icon;
  final String message;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) => SingleChildScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        child: SizedBox(
          height: constraints.maxHeight,
          child: EmptyState(icon: icon, message: message),
        ),
      ),
    );
  }
}
