import 'package:dotmac_portal/src/models/quote.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('QuotesPage', () {
    test('parses native action availability', () {
      final page = QuotesPage.fromJson({
        'quotes': const [],
        'total': 0,
        'open': 0,
        'source_state': 'native',
        'actions_available': true,
        'actions_unavailable_message': null,
      });

      expect(page.sourceState, QuoteSourceState.native);
      expect(page.actionsAvailable, isTrue);
    });

    test(
        'parses the retired owner message and fails closed if eligibility is missing',
        () {
      final retired = QuotesPage.fromJson({
        'quotes': const [],
        'total': 0,
        'open': 0,
        'source_state': 'retired',
        'actions_available': false,
        'actions_unavailable_message': 'Please contact support.',
      });
      final incomplete = QuotesPage.fromJson({'quotes': const []});

      expect(retired.sourceState, QuoteSourceState.retired);
      expect(retired.actionsAvailable, isFalse);
      expect(retired.actionsUnavailableMessage, 'Please contact support.');
      expect(incomplete.actionsAvailable, isFalse);
      expect(incomplete.sourceState, QuoteSourceState.unknown);
    });
  });

  group('Quote', () {
    test('parses a covered quote with estimate + deposit', () {
      final q = Quote.fromJson({
        'id': 'q1',
        'status': 'draft',
        'currency': 'NGN',
        'total': '75000.00',
        'deposit_amount': '37500.00',
        'deposit_percent': 50,
        'deposit_paid': false,
        'estimate_provisional': false,
        'feasibility': {
          'coverage': 'covered',
          'feasible': true,
          'distance_meters': 800.0,
          'nearest_fap_name': 'FAP Wuse',
        },
        'address': '12 Test St',
        'latitude': 9.07,
        'longitude': 7.49,
        'line_items': [
          {
            'description': 'Fiber installation (base)',
            'unit_price': '50000.00',
            'amount': '50000.00',
          },
        ],
        'created_at': '2026-06-29T10:00:00Z',
      });

      expect(q.id, 'q1');
      expect(q.feasibility.isCovered, isTrue);
      expect(q.canPayDeposit, isTrue);
      expect(q.lineItems.single.description, 'Fiber installation (base)');
      expect(naira(q.depositAmount), '₦37,500');
    });

    test('accepted quote cannot pay deposit again', () {
      final q = Quote.fromJson({
        'id': 'q2',
        'status': 'accepted',
        'deposit_amount': '37500.00',
        'deposit_paid': true,
        'project_id': 'p1',
        'feasibility': {'coverage': 'covered'},
      });
      expect(q.isAccepted, isTrue);
      expect(q.canPayDeposit, isFalse);
      expect(q.statusLabel, contains('Accepted'));
    });

    test('out-of-area provisional estimate', () {
      final q = Quote.fromJson({
        'id': 'q3',
        'status': 'draft',
        'deposit_amount': '0',
        'estimate_provisional': true,
        'feasibility': {'coverage': 'out_of_area', 'feasible': false},
      });
      expect(q.feasibility.outOfArea, isTrue);
      expect(q.estimateProvisional, isTrue);
      expect(q.canPayDeposit, isFalse); // no deposit due
    });

    test('deposit initiation + result parse', () {
      final init = QuoteDepositInitiation.fromJson({
        'invoice_id': 'inv1',
        'quote_id': 'q1',
        'amount': '37500.00',
        'currency': 'NGN',
        'provider_type': 'paystack',
        'payment_reference': 'ref_1',
        'charged': false,
      });
      expect(init.invoiceId, 'inv1');
      expect(init.paymentReference, 'ref_1');

      final result = QuoteDepositResult.fromJson({
        'paid': true,
        'reference': 'ref_1',
        'quote': {
          'id': 'q1',
          'status': 'accepted',
          'deposit_paid': true,
          'feasibility': {},
        },
      });
      expect(result.paid, isTrue);
      expect(result.quote?.isAccepted, isTrue);
    });
  });
}
