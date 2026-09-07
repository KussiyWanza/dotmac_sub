import 'dart:typed_data';

import 'package:dio/dio.dart';

import '../core/api_exception.dart';
import '../core/http.dart';
import '../models/billing_document.dart';
import '../models/invoice.dart';
import '../models/ledger.dart';
import '../models/page.dart';
import '../models/payment_flow.dart';
import '../models/payment_proof.dart';
import '../models/payment_method.dart';
import '../models/topup.dart';

/// Wraps the billing endpoints (app/api/billing.py, mounted at /api/v1).
class BillingRepository {
  BillingRepository(this.dio);

  final Dio dio;

  /// POST /payment-proofs/me — upload a bank-transfer receipt (multipart).
  Future<PaymentProofItem> submitPaymentProof({
    required String amount,
    String? bankName,
    String? reference,
    DateTime? paidAt,
    required String filePath,
    required String fileName,
  }) async {
    final form = FormData.fromMap({
      'amount': amount,
      if (bankName != null && bankName.isNotEmpty) 'bank_name': bankName,
      if (reference != null && reference.isNotEmpty) 'reference': reference,
      if (paidAt != null) 'paid_at': paidAt.toIso8601String(),
      'file': await MultipartFile.fromFile(filePath, filename: fileName),
    });
    final data = await guard(() => dio.post('/payment-proofs/me', data: form));
    return PaymentProofItem.fromJson(data as Map<String, dynamic>);
  }

  /// GET /payment-proofs/me — my submitted transfer proofs.
  Future<List<PaymentProofItem>> myPaymentProofs() async {
    final data = await guard(() => dio.get('/payment-proofs/me'));
    final items = (data as Map<String, dynamic>)['items'] as List? ?? const [];
    return items
        .cast<Map<String, dynamic>>()
        .map(PaymentProofItem.fromJson)
        .toList();
  }

  /// GET /me/invoices — the signed-in subscriber's own invoices (self-scoped).
  Future<Page<Invoice>> invoices({
    String? status,
    int limit = 50,
    int offset = 0,
  }) async {
    final data = await guard(() => dio.get('/me/invoices', queryParameters: {
          if (status != null) 'status': status,
          'limit': limit,
          'offset': offset,
        }));
    return Page.fromJson(data as Map<String, dynamic>, Invoice.fromJson);
  }

  /// GET /me/invoices/{id} — detail for one of the subscriber's own invoices.
  Future<Invoice> invoice(String id) async {
    final data = await guard(() => dio.get('/me/invoices/$id'));
    return Invoice.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/invoices/{id}/pdf — canonical, authenticated invoice document.
  Future<BillingDocument> invoicePdf(String id) => _downloadPdf(
        '/me/invoices/$id/pdf',
        fallbackFilename: 'invoice-$id.pdf',
      );

  /// GET /me/payments — the subscriber's own payment history (self-scoped).
  Future<Page<Payment>> payments({int limit = 50, int offset = 0}) async {
    final data = await guard(() => dio.get('/me/payments', queryParameters: {
          'limit': limit,
          'offset': offset,
        }));
    return Page.fromJson(data as Map<String, dynamic>, Payment.fromJson);
  }

  /// GET /me/payments/{id} — one self-scoped payment for a detail screen.
  Future<Payment> payment(String id) async {
    final data = await guard(() => dio.get('/me/payments/$id'));
    return Payment.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/payments/{id}/receipt/pdf — successful-payment receipt.
  Future<BillingDocument> paymentReceiptPdf(String id) => _downloadPdf(
        '/me/payments/$id/receipt/pdf',
        fallbackFilename: 'payment-receipt-$id.pdf',
      );

  /// GET /me/payment-methods — the subscriber's saved cards.
  Future<List<SavedCard>> paymentMethods() async {
    final data = await guard(() => dio.get('/me/payment-methods'));
    return (data as List)
        .cast<Map<String, dynamic>>()
        .map(SavedCard.fromJson)
        .toList();
  }

  /// PATCH /me/payment-methods/{id}/default — make a card the default.
  Future<void> setDefaultCard(String id) async {
    await guard(() => dio.patch('/me/payment-methods/$id/default'));
  }

  /// GET /me/autopay — current autopay status.
  Future<AutopayStatus> autopayStatus() async {
    final data = await guard(() => dio.get('/me/autopay'));
    return AutopayStatus.fromJson((data as Map).cast<String, dynamic>());
  }

  /// POST /me/autopay — enable autopay (default card unless one is given).
  Future<AutopayStatus> enableAutopay({String? paymentMethodId}) async {
    final data = await guard(() => dio.post('/me/autopay', data: {
          if (paymentMethodId != null) 'payment_method_id': paymentMethodId,
        }));
    return AutopayStatus.fromJson((data as Map).cast<String, dynamic>());
  }

  /// DELETE /me/autopay — disable autopay.
  Future<AutopayStatus> disableAutopay() async {
    final data = await guard(() => dio.delete('/me/autopay'));
    return AutopayStatus.fromJson((data as Map).cast<String, dynamic>());
  }

  /// DELETE /me/payment-methods/{id} — remove a saved card.
  Future<void> removeCard(String id) async {
    await guard(() => dio.delete('/me/payment-methods/$id'));
  }

  /// GET /me/balance — the subscriber's wallet/credit balance.
  Future<AccountBalance> balance() async {
    final data = await guard(() => dio.get('/me/balance'));
    return AccountBalance.fromJson((data as Map).cast<String, dynamic>());
  }

  /// GET /me/ledger — the subscriber's account ledger (transaction history).
  Future<Page<LedgerTxn>> ledger({int limit = 50, int offset = 0}) async {
    final data = await guard(() => dio.get('/me/ledger', queryParameters: {
          'limit': limit,
          'offset': offset,
        }));
    return Page.fromJson(data as Map<String, dynamic>, LedgerTxn.fromJson);
  }

  /// GET /me/ledger/{id} — one immutable, self-scoped activity entry.
  Future<LedgerTxn> ledgerEntry(String id) async {
    final data = await guard(() => dio.get('/me/ledger/$id'));
    return LedgerTxn.fromJson(data as Map<String, dynamic>);
  }

  /// GET /dashboard — free-form stats map for the current consumer.
  Future<Map<String, dynamic>> dashboard() async {
    final data = await guard(() => dio.get('/dashboard'));
    return (data as Map).cast<String, dynamic>();
  }

  /// POST /payments/initiate — pay one invoice via the chosen method.
  /// [provider] picks the gateway for a new card; [paymentMethodId] charges a
  /// saved card server-side (returns charged=true); [idempotencyKey] makes that
  /// charge safe against a double-submit.
  Future<PaymentInitiation> initiatePayment(
    String invoiceId, {
    String? provider,
    String? paymentMethodId,
    String? idempotencyKey,
  }) async {
    final data = await guard(() => dio.post(
          '/payments/initiate',
          data: {
            'invoice_id': invoiceId,
            if (provider != null) 'provider': provider,
            if (paymentMethodId != null) 'payment_method_id': paymentMethodId,
            if (idempotencyKey != null) 'idempotency_key': idempotencyKey,
          },
        ));
    return PaymentInitiation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /payments/verify — confirm + record a completed provider transaction.
  Future<PaymentVerification> verifyPayment(
    String reference, {
    String? provider,
  }) async {
    final data = await guard(() => dio.post(
          '/payments/verify',
          data: {
            'reference': reference,
            if (provider != null) 'provider': provider,
          },
        ));
    return PaymentVerification.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/topup — balance, amount limits, presets, provider.
  Future<TopupPage> topupPage() async {
    final data = await guard(() => dio.get('/me/topup'));
    return TopupPage.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/topup/preview — server-owned allocation preview for a deposit.
  Future<TopupPreview> previewTopup(num amount) async {
    final data = await guard(() => dio.post('/me/topup/preview', data: {
          'amount': amount,
        }));
    return TopupPreview.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/topup/initiate — start a prepaid top-up checkout.
  ///
  /// When [paymentMethodId] is given the server charges that saved card
  /// directly (one-tap) and returns `charged: true`; [idempotencyKey] makes
  /// that charge safe against a double-submit.
  /// [provider] picks the online gateway ('paystack'/'flutterwave') for a
  /// new-card checkout; ignored for saved-card charges.
  Future<TopupInitiation> initiateTopup(
    num amount, {
    required String previewFingerprint,
    String? provider,
    String? paymentMethodId,
    String? idempotencyKey,
  }) async {
    final data = await guard(() => dio.post('/me/topup/initiate', data: {
          'amount': amount,
          'preview_fingerprint': previewFingerprint,
          if (provider != null) 'provider': provider,
          if (paymentMethodId != null) 'payment_method_id': paymentMethodId,
          if (idempotencyKey != null) 'idempotency_key': idempotencyKey,
        }));
    return TopupInitiation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/topup/verify — confirm + credit the account.
  Future<TopupResult> verifyTopup(String reference,
      {bool saveCard = false}) async {
    final data = await guard(() => dio.post('/me/topup/verify', data: {
          'reference': reference,
          if (saveCard) 'save_card': true,
        }));
    return TopupResult.fromJson(data as Map<String, dynamic>);
  }

  Future<BillingDocument> _downloadPdf(
    String path, {
    required String fallbackFilename,
  }) async {
    late final Response<List<int>> response;
    try {
      response = await dio.get<List<int>>(
        path,
        options: Options(responseType: ResponseType.bytes),
      );
    } on DioException catch (error) {
      throw ApiException.fromDio(error);
    }

    final status = response.statusCode ?? 0;
    if (status >= 400) {
      throw ApiException('The PDF could not be downloaded.',
          statusCode: status);
    }
    final contentType = response.headers.value(Headers.contentTypeHeader) ?? '';
    final data = response.data;
    if (!contentType.toLowerCase().startsWith(BillingDocument.contentType) ||
        data == null ||
        !_hasPdfSignature(data)) {
      throw ApiException('The server returned an invalid PDF document.');
    }

    return BillingDocument(
      bytes: Uint8List.fromList(data),
      filename: _downloadFilename(response.headers, fallbackFilename),
    );
  }

  bool _hasPdfSignature(List<int> bytes) =>
      bytes.length >= 5 &&
      bytes[0] == 0x25 &&
      bytes[1] == 0x50 &&
      bytes[2] == 0x44 &&
      bytes[3] == 0x46 &&
      bytes[4] == 0x2d;

  String _downloadFilename(Headers headers, String fallback) {
    final disposition = headers.value('content-disposition') ?? '';
    final extended = RegExp(
      r"filename\*=UTF-8''([^;]+)",
      caseSensitive: false,
    ).firstMatch(disposition)?.group(1);
    final regular = RegExp(
      r'filename="?([^";]+)"?',
      caseSensitive: false,
    ).firstMatch(disposition)?.group(1);
    var candidate = extended ?? regular ?? fallback;
    try {
      candidate = Uri.decodeComponent(candidate);
    } on FormatException {
      candidate = fallback;
    }
    candidate = candidate.split(RegExp(r'[/\\]')).last.trim();
    if (candidate.isEmpty) candidate = fallback;
    return candidate.toLowerCase().endsWith('.pdf')
        ? candidate
        : '$candidate.pdf';
  }
}
