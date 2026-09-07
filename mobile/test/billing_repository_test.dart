import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/repositories/billing_repository.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeAdapter implements HttpClientAdapter {
  _FakeAdapter(this.onFetch);

  final ResponseBody Function(RequestOptions options) onFetch;
  final List<RequestOptions> calls = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    calls.add(options);
    return onFetch(options);
  }

  @override
  void close({bool force = false}) {}
}

Dio _dio(_FakeAdapter adapter) {
  final dio = Dio(BaseOptions(baseUrl: 'https://test.local/api/v1'));
  dio.httpClientAdapter = adapter;
  return dio;
}

ResponseBody _pdf({String filename = 'invoice-INV-42.pdf'}) =>
    ResponseBody.fromBytes(
      Uint8List.fromList('%PDF-test'.codeUnits),
      200,
      headers: {
        Headers.contentTypeHeader: ['application/pdf'],
        'content-disposition': ['attachment; filename="$filename"'],
      },
    );

void main() {
  test('invoice PDF uses authenticated API path and server filename', () async {
    final adapter = _FakeAdapter((_) => _pdf());

    final document = await BillingRepository(_dio(adapter)).invoicePdf('i-1');

    expect(adapter.calls.single.path, '/me/invoices/i-1/pdf');
    expect(adapter.calls.single.responseType, ResponseType.bytes);
    expect(document.filename, 'invoice-INV-42.pdf');
    expect(document.bytes.sublist(0, 5), '%PDF-'.codeUnits);
  });

  test('payment receipt PDF uses the self-scoped receipt path', () async {
    final adapter = _FakeAdapter((_) => _pdf(filename: 'receipt-RCP-2.pdf'));

    final document =
        await BillingRepository(_dio(adapter)).paymentReceiptPdf('p-2');

    expect(adapter.calls.single.path, '/me/payments/p-2/receipt/pdf');
    expect(document.filename, 'receipt-RCP-2.pdf');
  });

  test('PDF download rejects a non-PDF response', () async {
    final adapter = _FakeAdapter(
      (_) => ResponseBody.fromString(
        '<html>login</html>',
        200,
        headers: {
          Headers.contentTypeHeader: ['text/html'],
        },
      ),
    );

    expect(
      BillingRepository(_dio(adapter)).invoicePdf('i-1'),
      throwsA(isA<ApiException>()),
    );
  });
}
