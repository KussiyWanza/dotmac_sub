import 'dart:typed_data';

import 'package:dotmac_portal/src/core/billing_document_presenter.dart';
import 'package:dotmac_portal/src/features/billing/pdf_download_button.dart';
import 'package:dotmac_portal/src/models/billing_document.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _RecordingPresenter implements BillingDocumentPresenter {
  BillingDocument? presented;

  @override
  Future<void> present(BillingDocument document) async {
    presented = document;
  }
}

void main() {
  testWidgets('PDF button downloads once and hands off to the native presenter',
      (tester) async {
    final presenter = _RecordingPresenter();
    var downloads = 0;
    final document = BillingDocument(
      bytes: Uint8List.fromList('%PDF-test'.codeUnits),
      filename: 'invoice-1.pdf',
    );

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          billingDocumentPresenterProvider.overrideWithValue(presenter),
        ],
        child: MaterialApp(
          home: Scaffold(
            body: PdfDownloadButton(
              label: 'Download invoice PDF',
              download: () async {
                downloads++;
                return document;
              },
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Download invoice PDF'));
    await tester.pumpAndSettle();

    expect(downloads, 1);
    expect(presenter.presented, same(document));
    expect(find.text('PDF ready to save or share.'), findsOneWidget);
  });
}
