import 'dart:math';

import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/addon.dart';
import '../models/account_health.dart';
import '../models/device_command.dart';
import '../models/page.dart';
import '../models/plan_change.dart';
import '../models/subscription.dart';

/// Wraps the catalog subscription endpoints (app/api/catalog.py).
class CatalogRepository {
  CatalogRepository(this.dio);

  final Dio dio;

  /// GET /me/subscriptions — the signed-in subscriber's own services.
  Future<Page<Subscription>> subscriptions({
    String? status,
    int limit = 50,
    int offset = 0,
  }) async {
    final data = await guard(
      () => dio.get(
        '/me/subscriptions',
        queryParameters: {
          if (status != null) 'status': status,
          'limit': limit,
          'offset': offset,
        },
      ),
    );
    return Page.fromJson(data as Map<String, dynamic>, Subscription.fromJson);
  }

  /// GET /me/account-health — the canonical account, financial, access,
  /// session, connection, outage, freshness, and next-action projection.
  Future<AccountHealth> accountHealth() async {
    final data = await guard(() => dio.get('/me/account-health'));
    return AccountHealth.fromJson((data as Map).cast<String, dynamic>());
  }

  /// Reboot the exact device currently assigned to this signed-in service.
  Future<DeviceCommandOutcome> rebootDevice(String subscriptionId) async {
    final data = await guard(
      () => dio.post('/me/subscriptions/$subscriptionId/device/reboot'),
    );
    return DeviceCommandOutcome.fromJson((data as Map).cast<String, dynamic>());
  }

  /// Save and queue Wi-Fi settings for the exact assigned device.
  Future<DeviceCommandOutcome> updateWifi(
    String subscriptionId, {
    required String ssid,
    String? password,
  }) async {
    final key = 'wifi-${DateTime.now().microsecondsSinceEpoch}-'
        '${Random().nextInt(1 << 32)}';
    final data = await guard(
      () => dio.post(
        '/me/subscriptions/$subscriptionId/device/wifi',
        data: {
          'ssid': ssid,
          if (password != null && password.isNotEmpty) 'password': password,
          'idempotency_key': key,
        },
      ),
    );
    return DeviceCommandOutcome.fromJson((data as Map).cast<String, dynamic>());
  }

  /// Read the latest background Wi-Fi delivery status.
  Future<DeviceCommandOutcome> wifiUpdateStatus(String subscriptionId) async {
    final data = await guard(
      () => dio.get('/me/subscriptions/$subscriptionId/device/wifi'),
    );
    return DeviceCommandOutcome.fromJson((data as Map).cast<String, dynamic>());
  }

  /// GET /subscriptions/{id}
  Future<Subscription> subscription(String id) async {
    final data = await guard(() => dio.get('/subscriptions/$id'));
    return Subscription.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/subscriptions/{id}/service-change — plans and addresses available.
  Future<PlanChangeOptions> planChangeOptions(String subscriptionId) async {
    final data = await guard(
      () => dio.get('/me/subscriptions/$subscriptionId/service-change'),
    );
    return PlanChangeOptions.fromJson(data as Map<String, dynamic>);
  }

  /// GET …/service-change/quote — exact plan and delivery quote.
  Future<PlanChangeQuote> planChangeQuote(
    String subscriptionId,
    String offerId, {
    String? targetServiceAddressId,
  }) async {
    final data = await guard(
      () => dio.get(
        '/me/subscriptions/$subscriptionId/service-change/quote',
        queryParameters: {
          'offer_id': offerId,
          if (targetServiceAddressId != null)
            'target_service_address_id': targetServiceAddressId,
        },
      ),
    );
    return PlanChangeQuote.fromJson((data as Map).cast<String, dynamic>());
  }

  /// GET /me/subscriptions/{id}/add-ons — available and active add-ons.
  Future<AddonsAvailable> addons(String subscriptionId) async {
    final data = await guard(
      () => dio.get('/me/subscriptions/$subscriptionId/add-ons'),
    );
    return AddonsAvailable.fromJson(data as Map<String, dynamic>);
  }

  /// GET …/add-ons/quote — server-owned purchase and exact-debit preview.
  Future<AddonQuote> addonQuote(
    String subscriptionId,
    String addOnId,
    int quantity,
  ) async {
    final data = await guard(
      () => dio.get(
        '/me/subscriptions/$subscriptionId/add-ons/quote',
        queryParameters: {'add_on_id': addOnId, 'quantity': quantity},
      ),
    );
    return AddonQuote.fromJson(data as Map<String, dynamic>);
  }

  /// POST …/add-ons — confirm the exact previewed add-on debit. A fresh
  /// idempotency key is built into the request so a transport-level retry
  /// (e.g. the 401-refresh replay) cannot post the prepaid debit twice.
  Future<AddonPurchaseResult> purchaseAddon(
    String subscriptionId,
    String addOnId,
    int quantity,
    String previewFingerprint,
  ) async {
    final key = 'addon-${DateTime.now().microsecondsSinceEpoch}-'
        '${Random().nextInt(1 << 32)}';
    final data = await guard(
      () => dio.post(
        '/me/subscriptions/$subscriptionId/add-ons',
        data: {
          'add_on_id': addOnId,
          'quantity': quantity,
          'preview_fingerprint': previewFingerprint,
          'idempotency_key': key,
        },
      ),
    );
    return AddonPurchaseResult.fromJson(data as Map<String, dynamic>);
  }

  /// DELETE …/add-ons/{id} — cancel an add-on (stops billing next cycle).
  Future<void> cancelAddon(String subscriptionId, String subAddOnId) async {
    await guard(
      () => dio.delete('/me/subscriptions/$subscriptionId/add-ons/$subAddOnId'),
    );
  }

  /// POST …/service-change — confirm the reviewed service change.
  Future<PlanChangeResult> submitPlanChange(
    String subscriptionId, {
    required String offerId,
    required String previewFingerprint,
    required DateTime previewEffectiveAt,
    String? targetServiceAddressId,
    String? fieldQuoteFingerprint,
    String? notes,
  }) async {
    final key = 'plan-${DateTime.now().microsecondsSinceEpoch}-'
        '${Random().nextInt(1 << 32)}';
    final data = await guard(
      () => dio.post(
        '/me/subscriptions/$subscriptionId/service-change',
        data: {
          'offer_id': offerId,
          'preview_fingerprint': previewFingerprint,
          'preview_effective_at': previewEffectiveAt.toUtc().toIso8601String(),
          if (targetServiceAddressId != null)
            'target_service_address_id': targetServiceAddressId,
          if (fieldQuoteFingerprint != null)
            'field_quote_fingerprint': fieldQuoteFingerprint,
          'idempotency_key': key,
          if (notes != null) 'notes': notes,
        },
      ),
    );
    return PlanChangeResult.fromJson((data as Map).cast<String, dynamic>());
  }
}
