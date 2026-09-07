import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';

class ActivityDetailScreen extends ConsumerWidget {
  const ActivityDetailScreen({super.key, required this.entryId});

  final String entryId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final entry = ref.watch(ledgerEntryProvider(entryId));
    return Scaffold(
      appBar: AppBar(title: const Text('Activity')),
      body: AsyncValueView(
        value: entry,
        onRetry: () => ref.invalidate(ledgerEntryProvider(entryId)),
        skeleton: const ListSkeleton(rows: 4),
        data: (item) {
          final color = item.isCredit
              ? context.semantic.success
              : Theme.of(context).colorScheme.error;
          final sign = item.isCredit ? '+' : '−';
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Text(item.title, style: Theme.of(context).textTheme.titleLarge),
              const SizedBox(height: 8),
              Text(
                '$sign${Fmt.money(item.amount, item.currency)}',
                style: Theme.of(context)
                    .textTheme
                    .headlineSmall
                    ?.copyWith(color: color, fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 16),
              Card(
                child: Column(
                  children: [
                    _ActivityRow('Date', Fmt.dateTime(item.occurredAt)),
                    const Divider(height: 1),
                    _ActivityRow(
                      'Type',
                      item.entryType == 'credit' ? 'Credit' : 'Debit',
                    ),
                    const Divider(height: 1),
                    _ActivityRow(
                      'Source',
                      _titleCase(item.source ?? 'Not available'),
                    ),
                    const Divider(height: 1),
                    _ActivityRow('Activity ID', item.id, selectable: true),
                  ],
                ),
              ),
              if (item.paymentId != null || item.invoiceId != null) ...[
                const SizedBox(height: 20),
                Text('Related records',
                    style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 8),
                Card(
                  child: Column(
                    children: [
                      if (item.paymentId != null)
                        ListTile(
                          leading: const Icon(Icons.payments_outlined),
                          title: const Text('View payment'),
                          trailing: const Icon(Icons.chevron_right),
                          onTap: () => context
                              .push('/billing/payments/${item.paymentId}'),
                        ),
                      if (item.paymentId != null && item.invoiceId != null)
                        const Divider(height: 1),
                      if (item.invoiceId != null)
                        ListTile(
                          leading: const Icon(Icons.receipt_long_outlined),
                          title: const Text('View invoice'),
                          trailing: const Icon(Icons.chevron_right),
                          onTap: () => context
                              .push('/billing/invoices/${item.invoiceId}'),
                        ),
                    ],
                  ),
                ),
              ],
            ],
          );
        },
      ),
    );
  }

  String _titleCase(String value) => value
      .replaceAll('_', ' ')
      .split(' ')
      .where((part) => part.isNotEmpty)
      .map((part) => '${part[0].toUpperCase()}${part.substring(1)}')
      .join(' ');
}

class _ActivityRow extends StatelessWidget {
  const _ActivityRow(this.label, this.value, {this.selectable = false});

  final String label;
  final String value;
  final bool selectable;

  @override
  Widget build(BuildContext context) => ListTile(
        title: Text(label),
        subtitle: selectable ? SelectableText(value) : Text(value),
      );
}
