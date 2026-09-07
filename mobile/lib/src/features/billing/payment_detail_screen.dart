import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';
import 'pdf_download_button.dart';

class PaymentDetailScreen extends ConsumerWidget {
  const PaymentDetailScreen({super.key, required this.paymentId});

  final String paymentId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final payment = ref.watch(paymentProvider(paymentId));
    return Scaffold(
      appBar: AppBar(title: const Text('Payment')),
      body: AsyncValueView(
        value: payment,
        onRetry: () => ref.invalidate(paymentProvider(paymentId)),
        skeleton: const ListSkeleton(rows: 4),
        data: (item) => ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    Fmt.money(item.amount, item.currency),
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                ),
                StatusChip.fromPresentation(item.statusPresentation),
              ],
            ),
            const SizedBox(height: 16),
            Card(
              child: Column(
                children: [
                  _DetailRow(
                    'Payment date',
                    Fmt.dateTime(item.paidAt ?? item.createdAt),
                  ),
                  const Divider(height: 1),
                  _DetailRow(
                    'Reference',
                    item.externalId?.trim().isNotEmpty == true
                        ? item.externalId!
                        : 'Not available',
                    selectable: item.externalId?.trim().isNotEmpty == true,
                  ),
                  if (item.providerFee > 0) ...[
                    const Divider(height: 1),
                    _DetailRow(
                      'Provider fee',
                      Fmt.money(item.providerFee, item.currency),
                    ),
                  ],
                  if (item.refundedAmount > 0) ...[
                    const Divider(height: 1),
                    _DetailRow(
                      'Refunded',
                      Fmt.money(item.refundedAmount, item.currency),
                    ),
                    const Divider(height: 1),
                    _DetailRow(
                      'Net payment',
                      Fmt.money(item.netAmount, item.currency),
                      bold: true,
                    ),
                  ],
                ],
              ),
            ),
            if (item.memo?.trim().isNotEmpty == true) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text(item.memo!),
                ),
              ),
            ],
            if (item.allocations.isNotEmpty) ...[
              const SizedBox(height: 20),
              Text('Applied to invoices',
                  style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              Card(
                child: Column(
                  children: [
                    for (var index = 0;
                        index < item.allocations.length;
                        index++) ...[
                      if (index > 0) const Divider(height: 1),
                      ListTile(
                        title: Text(
                          'Invoice ${item.allocations[index].invoiceId.substring(0, 8)}',
                        ),
                        subtitle: Text(Fmt.money(
                          item.allocations[index].amount,
                          item.currency,
                        )),
                        trailing: const Icon(Icons.chevron_right),
                        onTap: () => context.push(
                          '/billing/invoices/${item.allocations[index].invoiceId}',
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            ],
            const SizedBox(height: 24),
            PdfDownloadButton(
              key: const ValueKey('download-payment-pdf'),
              label: item.hasReceipt
                  ? 'Download payment receipt PDF'
                  : 'Payment receipt unavailable',
              enabled: item.hasReceipt,
              download: () => ref
                  .read(billingRepositoryProvider)
                  .paymentReceiptPdf(item.id),
            ),
            if (!item.hasReceipt) ...[
              const SizedBox(height: 8),
              Text(
                'A PDF receipt becomes available after the payment succeeds.',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _DetailRow extends StatelessWidget {
  const _DetailRow(
    this.label,
    this.value, {
    this.bold = false,
    this.selectable = false,
  });

  final String label;
  final String value;
  final bool bold;
  final bool selectable;

  @override
  Widget build(BuildContext context) {
    final valueStyle = TextStyle(fontWeight: bold ? FontWeight.bold : null);
    return ListTile(
      title: Text(label),
      subtitle: selectable
          ? SelectableText(value, style: valueStyle)
          : Text(value, style: valueStyle),
    );
  }
}
