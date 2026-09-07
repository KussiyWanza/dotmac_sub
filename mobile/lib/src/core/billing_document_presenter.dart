import 'dart:io';

import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';

import '../models/billing_document.dart';

abstract interface class BillingDocumentPresenter {
  Future<void> present(BillingDocument document);
}

/// Writes the authenticated PDF to app cache, then opens the platform's native
/// sheet so the customer can save, print, or share it.
class NativeBillingDocumentPresenter implements BillingDocumentPresenter {
  const NativeBillingDocumentPresenter();

  @override
  Future<void> present(BillingDocument document) async {
    final cache = await getTemporaryDirectory();
    final directory = await cache.createTemp('billing-document-');
    final file = File('${directory.path}${Platform.pathSeparator}'
        '${_safeFilename(document.filename)}');
    try {
      await file.writeAsBytes(document.bytes, flush: true);
      await Share.shareXFiles(
        [XFile(file.path, mimeType: BillingDocument.contentType)],
        subject: document.filename,
      );
    } finally {
      if (await directory.exists()) {
        await directory.delete(recursive: true);
      }
    }
  }

  String _safeFilename(String value) {
    final leaf = value.split(RegExp(r'[/\\]')).last;
    final cleaned = leaf.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '-');
    return cleaned.toLowerCase().endsWith('.pdf') ? cleaned : '$cleaned.pdf';
  }
}
