/// Models for the self-serve installation quote flow (Sales/Quotes).
///
/// Mirror of the sub `/me/quotes` payloads. Money fields stay as the backend's
/// decimal strings (e.g. "75000.00") to avoid float drift; use [naira] to format.
library;

String _str(dynamic v) => v == null ? '' : v.toString();

Map<String, dynamic>? _asMap(dynamic v) =>
    v is Map ? v.cast<String, dynamic>() : null;

double? _toDoubleOrNull(dynamic v) {
  if (v == null) return null;
  return double.tryParse(v.toString());
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}

enum QuoteSourceState { native, retired, unknown }

/// The complete `/me/quotes` read contract, including the command owner's
/// decision about whether the customer may request another quote.
class QuotesPage {
  QuotesPage({
    required this.quotes,
    required this.total,
    required this.open,
    required this.sourceState,
    required this.actionsAvailable,
    this.actionsUnavailableMessage,
  });

  final List<Quote> quotes;
  final int total;
  final int open;
  final QuoteSourceState sourceState;
  final bool actionsAvailable;
  final String? actionsUnavailableMessage;

  factory QuotesPage.fromJson(Map<String, dynamic> json) {
    final sourceState = switch (json['source_state']) {
      'native' => QuoteSourceState.native,
      'retired' => QuoteSourceState.retired,
      _ => QuoteSourceState.unknown,
    };
    final unavailableMessage = json['actions_unavailable_message']?.toString();
    return QuotesPage(
      quotes: [
        for (final item in (json['quotes'] as List? ?? const []))
          if (_asMap(item) case final quote?) Quote.fromJson(quote),
      ],
      total: (json['total'] as num?)?.toInt() ?? 0,
      open: (json['open'] as num?)?.toInt() ?? 0,
      sourceState: sourceState,
      // Fail closed when an older or malformed server omits eligibility.
      actionsAvailable: json['actions_available'] == true,
      actionsUnavailableMessage:
          unavailableMessage == null || unavailableMessage.trim().isEmpty
              ? null
              : unavailableMessage,
    );
  }
}

/// Format a decimal-string amount as Naira with thousands separators.
String naira(String amount) {
  final value = double.tryParse(amount) ?? 0;
  final whole = value.round();
  final digits = whole.toString();
  final buf = StringBuffer();
  for (var i = 0; i < digits.length; i++) {
    if (i > 0 && (digits.length - i) % 3 == 0) buf.write(',');
    buf.write(digits[i]);
  }
  return '₦$buf';
}

class QuoteFeasibility {
  QuoteFeasibility({
    this.coverage,
    this.feasible,
    this.distanceMeters,
    this.nearestFapName,
  });

  final String? coverage; // covered | survey_required | out_of_area
  final bool? feasible;
  final double? distanceMeters;
  final String? nearestFapName;

  bool get isCovered => coverage == 'covered';
  bool get needsSurvey => coverage == 'survey_required';
  bool get outOfArea => coverage == 'out_of_area';

  String get label => switch (coverage) {
        'covered' => 'Covered — fibre is nearby',
        'survey_required' => 'Survey required',
        'out_of_area' => 'Outside current coverage',
        _ => 'Checking coverage…',
      };

  factory QuoteFeasibility.fromJson(Map<String, dynamic>? json) {
    if (json == null) return QuoteFeasibility();
    return QuoteFeasibility(
      coverage: json['coverage'] as String?,
      feasible: json['feasible'] as bool?,
      distanceMeters: _toDoubleOrNull(json['distance_meters']),
      nearestFapName: json['nearest_fap_name'] as String?,
    );
  }
}

class QuoteLineItem {
  QuoteLineItem({
    required this.description,
    this.quantity,
    this.unitPrice,
    this.amount,
  });

  final String description;
  final String? quantity;
  final String? unitPrice;
  final String? amount;

  factory QuoteLineItem.fromJson(Map<String, dynamic> json) => QuoteLineItem(
        description: _str(json['description']),
        quantity: json['quantity'] as String?,
        unitPrice: json['unit_price'] as String?,
        amount: json['amount'] as String?,
      );
}

class Quote {
  Quote({
    required this.id,
    required this.status,
    required this.currency,
    required this.total,
    required this.depositAmount,
    required this.depositPaid,
    required this.estimateProvisional,
    required this.feasibility,
    this.depositPercent,
    this.address,
    this.region,
    this.latitude,
    this.longitude,
    this.lineItems = const [],
    this.salesOrderId,
    this.projectId,
    this.createdAt,
    this.expiresAt,
  });

  final String id;
  final String status; // draft | sent | accepted | rejected | expired
  final String currency;
  final String total;
  final String depositAmount;
  final int? depositPercent;
  final bool depositPaid;
  final bool estimateProvisional;
  final QuoteFeasibility feasibility;
  final String? address;
  final String? region;
  final double? latitude;
  final double? longitude;
  final List<QuoteLineItem> lineItems;
  final String? salesOrderId;
  final String? projectId;
  final DateTime? createdAt;
  final DateTime? expiresAt;

  bool get isAccepted => status == 'accepted';
  bool get canPayDeposit =>
      !isAccepted && !depositPaid && (double.tryParse(depositAmount) ?? 0) > 0;

  String get statusLabel => switch (status) {
        'draft' => depositPaid ? 'Deposit paid' : 'Awaiting deposit',
        'sent' => 'Awaiting deposit',
        'accepted' => 'Accepted — installation scheduled',
        'rejected' => 'Declined',
        'expired' => 'Expired',
        _ => status,
      };

  factory Quote.fromJson(Map<String, dynamic> json) => Quote(
        id: _str(json['id']),
        status: json['status'] as String? ?? 'draft',
        currency: json['currency'] as String? ?? 'NGN',
        total: _str(json['total'] ?? '0'),
        depositAmount: _str(json['deposit_amount'] ?? '0'),
        depositPercent: json['deposit_percent'] as int?,
        depositPaid: json['deposit_paid'] as bool? ?? false,
        estimateProvisional: json['estimate_provisional'] as bool? ?? false,
        feasibility: QuoteFeasibility.fromJson(_asMap(json['feasibility'])),
        address: json['address'] as String?,
        region: json['region'] as String?,
        latitude: _toDoubleOrNull(json['latitude']),
        longitude: _toDoubleOrNull(json['longitude']),
        lineItems: [
          for (final li in (json['line_items'] as List? ?? const []))
            if (_asMap(li) case final m?) QuoteLineItem.fromJson(m),
        ],
        salesOrderId: json['sales_order_id'] as String?,
        projectId: json['project_id'] as String?,
        createdAt: _toDate(json['created_at']),
        expiresAt: _toDate(json['expires_at']),
      );
}

class QuoteDepositInitiation {
  QuoteDepositInitiation({
    required this.invoiceId,
    required this.quoteId,
    required this.amount,
    required this.currency,
    required this.providerType,
    required this.paymentReference,
    this.providerPublicKey,
    this.checkoutUrl,
    this.customerEmail,
    this.charged = false,
  });

  final String invoiceId;
  final String quoteId;
  final String amount;
  final String currency;
  final String providerType;
  final String paymentReference;
  final String? providerPublicKey;
  final String? checkoutUrl;
  final String? customerEmail;
  final bool charged;

  factory QuoteDepositInitiation.fromJson(Map<String, dynamic> json) =>
      QuoteDepositInitiation(
        invoiceId: _str(json['invoice_id']),
        quoteId: _str(json['quote_id']),
        amount: _str(json['amount'] ?? '0'),
        currency: json['currency'] as String? ?? 'NGN',
        providerType: json['provider_type'] as String? ?? 'paystack',
        paymentReference: _str(json['payment_reference']),
        providerPublicKey: json['provider_public_key'] as String?,
        checkoutUrl: json['checkout_url'] as String?,
        customerEmail: json['customer_email'] as String?,
        charged: json['charged'] as bool? ?? false,
      );
}

class QuoteDepositResult {
  QuoteDepositResult({required this.paid, required this.reference, this.quote});

  final bool paid;
  final String reference;
  final Quote? quote;

  factory QuoteDepositResult.fromJson(Map<String, dynamic> json) =>
      QuoteDepositResult(
        paid: json['paid'] as bool? ?? false,
        reference: _str(json['reference']),
        quote: _asMap(json['quote']) != null
            ? Quote.fromJson(_asMap(json['quote'])!)
            : null,
      );
}
