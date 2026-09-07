import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/quote.dart';

/// Wraps the self-scoped self-serve quote endpoints (app/api/me.py, /me/quotes*):
/// request a map-pinned installation quote, list quotes, and pay the deposit via
/// the existing billing/pay flow.
class QuotesRepository {
  QuotesRepository(this.dio);

  final Dio dio;

  /// GET /me/quotes
  Future<QuotesPage> quotes() async {
    final data = await guard(() => dio.get('/me/quotes'));
    return QuotesPage.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/quote-request — drop a pin; the active quote owner returns
  /// feasibility and an estimate.
  Future<Quote> requestQuote({
    required double latitude,
    required double longitude,
    String? address,
    String? region,
    String? note,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/quote-request',
        data: {
          'latitude': latitude,
          'longitude': longitude,
          if (address != null && address.isNotEmpty) 'address': address,
          if (region != null && region.isNotEmpty) 'region': region,
          if (note != null && note.isNotEmpty) 'note': note,
        },
      ),
    );
    return Quote.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/quotes/{id}/deposit/initiate — start the deposit checkout.
  Future<QuoteDepositInitiation> initiateDeposit(
    String quoteId, {
    String? provider,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/quotes/$quoteId/deposit/initiate',
        data: {if (provider != null) 'provider': provider},
      ),
    );
    return QuoteDepositInitiation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/quotes/{id}/deposit/verify — confirm payment; on settlement the
  /// quote is accepted in the CRM (sales order + install project).
  Future<QuoteDepositResult> verifyDeposit(
    String quoteId, {
    required String reference,
    String? provider,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/quotes/$quoteId/deposit/verify',
        data: {
          'reference': reference,
          if (provider != null) 'provider': provider,
        },
      ),
    );
    return QuoteDepositResult.fromJson(data as Map<String, dynamic>);
  }
}
