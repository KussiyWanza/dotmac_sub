// A single account ledger entry (app/api/me.py GET /me/ledger): a charge,
// payment, credit or adjustment on the customer's account.
import '../core/parsers.dart';

class LedgerTxn {
  LedgerTxn({
    required this.id,
    required this.entryType,
    required this.amount,
    required this.currency,
    required this.createdAt,
    this.effectiveAt,
    this.source,
    this.memo,
    this.invoiceId,
    this.paymentId,
  });

  final String id;
  final String entryType; // 'debit' | 'credit'
  final double amount;
  final String currency;
  final DateTime? createdAt;
  final DateTime? effectiveAt;
  final String? source; // invoice | payment | adjustment | refund | credit_note
  final String? memo;
  final String? invoiceId;
  final String? paymentId;

  /// Credits (payments, refunds in) increase the customer's standing; debits
  /// (charges) reduce it. Used for sign + colour in the UI.
  bool get isCredit => entryType == 'credit';
  DateTime? get occurredAt => effectiveAt ?? createdAt;

  /// A human label for what this entry is, preferring the memo.
  String get title {
    final m = memo?.trim();
    if (m != null && m.isNotEmpty) return m;
    return switch (source) {
      'invoice' => 'Charge',
      'payment' => 'Payment',
      'adjustment' => 'Adjustment',
      'refund' => 'Refund',
      'credit_note' => 'Credit note',
      _ => isCredit ? 'Credit' : 'Charge',
    };
  }

  factory LedgerTxn.fromJson(Map<String, dynamic> json) => LedgerTxn(
        id: json['id'].toString(),
        entryType: json['entry_type'] as String? ?? 'debit',
        amount: asDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        createdAt:
            DateTime.tryParse(json['created_at']?.toString() ?? '')?.toLocal(),
        effectiveAt: DateTime.tryParse(json['effective_date']?.toString() ?? '')
            ?.toLocal(),
        source: json['source'] as String?,
        memo: json['memo'] as String?,
        invoiceId: json['invoice_id']?.toString(),
        paymentId: json['payment_id']?.toString(),
      );
}

/// The customer's wallet/credit balance (GET /me/balance). Positive means
/// credit on file; negative means an outstanding amount.
class AccountBalance {
  AccountBalance({required this.creditBalance, required this.currency});

  final double creditBalance;
  final String currency;

  bool get inCredit => creditBalance > 0;
  bool get owes => creditBalance < 0;

  factory AccountBalance.fromJson(Map<String, dynamic> json) => AccountBalance(
        creditBalance: asDouble(json['credit_balance']),
        currency: json['currency'] as String? ?? 'NGN',
      );
}
