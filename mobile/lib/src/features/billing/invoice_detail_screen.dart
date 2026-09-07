import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/formatters.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';
import 'invoice_pay_button.dart';
import 'pdf_download_button.dart';

class InvoiceDetailScreen extends ConsumerWidget {
  const InvoiceDetailScreen({super.key, required this.invoiceId});

  final String invoiceId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final invoice = ref.watch(invoiceProvider(invoiceId));

    return Scaffold(
      appBar: AppBar(title: const Text('Invoice')),
      body: AsyncValueView(
        value: invoice,
        onRetry: () => ref.invalidate(invoiceProvider(invoiceId)),
        skeleton: const ListSkeleton(rows: 4),
        data: (inv) => ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Expanded(
                  child: Text(
                    inv.invoiceNumber ?? 'Invoice ${inv.id.substring(0, 8)}',
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                ),
                StatusChip.fromPresentation(inv.statusPresentation),
              ],
            ),
            const SizedBox(height: 16),
            Card(
              child: Column(
                children: [
                  _Row('Issued', Fmt.date(inv.issuedAt)),
                  const Divider(height: 1),
                  _Row('Due', Fmt.date(inv.dueAt)),
                  if (inv.paidAt != null) ...[
                    const Divider(height: 1),
                    _Row('Paid', Fmt.date(inv.paidAt)),
                  ],
                ],
              ),
            ),
            const SizedBox(height: 12),
            Card(
              child: Column(
                children: [
                  _Row('Subtotal', Fmt.money(inv.subtotal, inv.currency)),
                  const Divider(height: 1),
                  _Row('Tax', Fmt.money(inv.taxTotal, inv.currency)),
                  const Divider(height: 1),
                  _Row('Total', Fmt.money(inv.total, inv.currency), bold: true),
                  const Divider(height: 1),
                  _Row('Balance due', Fmt.money(inv.balanceDue, inv.currency),
                      bold: true,
                      color: inv.balanceDue > 0
                          ? Theme.of(context).colorScheme.error
                          : null),
                ],
              ),
            ),
            if (inv.memo != null && inv.memo!.isNotEmpty) ...[
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text(inv.memo!),
                ),
              ),
            ],
            if (!inv.isPaid) ...[
              const SizedBox(height: 24),
              InvoicePayButton(invoice: inv),
            ],
            const SizedBox(height: 12),
            PdfDownloadButton(
              key: const ValueKey('download-invoice-pdf'),
              label: 'Download invoice PDF',
              download: () =>
                  ref.read(billingRepositoryProvider).invoicePdf(inv.id),
            ),
          ],
        ),
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row(this.label, this.value, {this.bold = false, this.color});
  final String label;
  final String value;
  final bool bold;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final style = TextStyle(
      fontWeight: bold ? FontWeight.bold : FontWeight.normal,
      color: color,
    );
    return ListTile(
      title: Text(label),
      trailing: Text(value, style: style),
    );
  }
}
