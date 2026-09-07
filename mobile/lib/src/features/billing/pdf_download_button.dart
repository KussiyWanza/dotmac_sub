import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_exception.dart';
import '../../models/billing_document.dart';
import '../../providers/data_providers.dart';

class PdfDownloadButton extends ConsumerStatefulWidget {
  const PdfDownloadButton({
    super.key,
    required this.label,
    required this.download,
    this.enabled = true,
  });

  final String label;
  final Future<BillingDocument> Function() download;
  final bool enabled;

  @override
  ConsumerState<PdfDownloadButton> createState() => _PdfDownloadButtonState();
}

class _PdfDownloadButtonState extends ConsumerState<PdfDownloadButton> {
  bool _downloading = false;

  Future<void> _download() async {
    setState(() => _downloading = true);
    try {
      final document = await widget.download();
      await ref.read(billingDocumentPresenterProvider).present(document);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('PDF ready to save or share.')),
      );
    } catch (error) {
      if (!mounted) return;
      final message = error is ApiException
          ? error.message
          : 'The PDF could not be saved. Please try again.';
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(message)),
      );
    } finally {
      if (mounted) setState(() => _downloading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      child: OutlinedButton.icon(
        onPressed: widget.enabled && !_downloading ? _download : null,
        icon: _downloading
            ? const SizedBox.square(
                dimension: 18,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : const Icon(Icons.picture_as_pdf_outlined),
        label: Text(_downloading ? 'Preparing PDF…' : widget.label),
      ),
    );
  }
}
