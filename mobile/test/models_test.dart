import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/config/env.dart';
import 'package:dotmac_portal/src/models/auth.dart';
import 'package:dotmac_portal/src/models/addon.dart';
import 'package:dotmac_portal/src/models/account_health.dart';
import 'package:dotmac_portal/src/models/device_command.dart';
import 'package:dotmac_portal/src/models/invoice.dart';
import 'package:dotmac_portal/src/models/ledger.dart';
import 'package:dotmac_portal/src/models/notification.dart';
import 'package:dotmac_portal/src/models/page.dart';
import 'package:dotmac_portal/src/models/payment_method.dart';
import 'package:dotmac_portal/src/models/payment_flow.dart';
import 'package:dotmac_portal/src/models/plan_change.dart';
import 'package:dotmac_portal/src/models/session.dart';
import 'package:dotmac_portal/src/models/subscription.dart';
import 'package:dotmac_portal/src/models/ticket.dart';
import 'package:dotmac_portal/src/models/topup.dart';
import 'package:dotmac_portal/src/models/usage.dart';

void main() {
  test('device command outcome preserves canonical operation evidence', () {
    final outcome = DeviceCommandOutcome.fromJson({
      'command': 'reboot',
      'status': 'succeeded',
      'subscription_id': 'sub-1',
      'device_id': 'ont-1',
      'operation_id': 'op-1',
      'message': 'Restart completed',
    });
    expect(outcome.succeeded, isTrue);
    expect(outcome.operationId, 'op-1');
    expect(outcome.subscriptionId, 'sub-1');
  });

  group('LoginResult', () {
    test('parses a token response', () {
      final r = LoginResult.fromJson({
        'access_token': 'a',
        'refresh_token': 'b',
        'token_type': 'bearer',
      });
      expect(r.isAuthenticated, isTrue);
      expect(r.mfaRequired, isFalse);
    });

    test('parses an MFA challenge', () {
      final r = LoginResult.fromJson({
        'mfa_required': true,
        'mfa_token': 'tok',
      });
      expect(r.isAuthenticated, isFalse);
      expect(r.mfaToken, 'tok');
    });
  });

  group('Me', () {
    test('derives initials and full name', () {
      final me = Me.fromJson({
        'id': '11111111-1111-1111-1111-111111111111',
        'first_name': 'Ada',
        'last_name': 'Obi',
        'email': 'ada@example.com',
      });
      expect(me.initials, 'AO');
      expect(me.fullName, 'Ada Obi');
    });

    test('prefers display_name when present', () {
      final me = Me.fromJson({
        'id': '1',
        'first_name': 'Ada',
        'last_name': 'Obi',
        'email': 'a@b.c',
        'display_name': 'Ada O.',
      });
      expect(me.fullName, 'Ada O.');
    });
  });

  group('Invoice', () {
    test('parses Decimal-as-string amounts and overdue state', () {
      final inv = Invoice.fromJson({
        'id': 'i1',
        'account_id': 'a1',
        'status': 'issued',
        'status_presentation': {
          'value': 'issued',
          'label': 'Awaiting payment',
          'tone': 'info',
          'icon': 'info',
        },
        'currency': 'NGN',
        'subtotal': '1000.00',
        'tax_total': '75.00',
        'total': '1075.00',
        'balance_due': '1075.00',
        'due_at': '2000-01-01T00:00:00Z', // in the past
      });
      expect(inv.total, 1075.0);
      expect(inv.isPaid, isFalse);
      expect(inv.isOverdue, isTrue);
      expect(inv.statusPresentation.label, 'Awaiting payment');
      expect(inv.statusPresentation.tone.name, 'info');
    });

    test('treats zero balance as paid', () {
      final inv = Invoice.fromJson({
        'id': 'i2',
        'account_id': 'a1',
        'status': 'issued',
        'balance_due': '0.00',
        'total': '500.00',
      });
      expect(inv.isPaid, isTrue);
      expect(inv.statusPresentation.tone.name, 'neutral');
    });
  });

  group('Payment', () {
    test('parses the server-owned status presentation', () {
      final payment = Payment.fromJson({
        'id': 'p1',
        'amount': '2500.00',
        'currency': 'NGN',
        'status': 'partially_refunded',
        'created_at': '2026-09-01T10:00:00Z',
        'refunded_amount': '500.00',
        'provider_fee': '25.00',
        'allocations': [
          {'id': 'a1', 'invoice_id': 'i1', 'amount': '2000.00'},
        ],
        'status_presentation': {
          'value': 'partially_refunded',
          'label': 'Partially refunded',
          'tone': 'warning',
          'icon': 'clock',
        },
      });

      expect(payment.amount, 2500.0);
      expect(payment.statusPresentation.label, 'Partially refunded');
      expect(payment.statusPresentation.tone.name, 'warning');
      expect(payment.statusPresentation.icon, 'clock');
      expect(payment.createdAt, isNotNull);
      expect(payment.refundedAmount, 500);
      expect(payment.netAmount, 2000);
      expect(payment.providerFee, 25);
      expect(payment.allocations.single.invoiceId, 'i1');
      expect(payment.hasReceipt, isFalse);
    });

    test('uses a neutral compatibility fallback for older servers', () {
      final payment = Payment.fromJson({
        'id': 'p2',
        'amount': '100.00',
        'status': 'succeeded',
      });

      expect(payment.statusPresentation.label, 'Succeeded');
      expect(payment.statusPresentation.tone.name, 'neutral');
      expect(payment.hasReceipt, isTrue);
    });
  });

  group('QuotaBucket', () {
    test('computes fraction and remaining for a capped plan', () {
      final b = QuotaBucket.fromJson({
        'id': 'b1',
        'subscription_id': 's1',
        'period_start': '2026-06-01T00:00:00Z',
        'period_end': '2026-07-01T00:00:00Z',
        'included_gb': '100',
        'used_gb': '25',
        'rollover_gb': '0',
        'overage_gb': '0',
      });
      expect(b.isUnlimited, isFalse);
      expect(b.allowanceGb, 100);
      expect(b.remainingGb, 75);
      expect(b.usedFraction, closeTo(0.25, 1e-9));
    });

    test('unlimited when included_gb is null', () {
      final b = QuotaBucket.fromJson({
        'id': 'b2',
        'subscription_id': 's1',
        'period_start': '2026-06-01T00:00:00Z',
        'period_end': '2026-07-01T00:00:00Z',
        'used_gb': '40',
      });
      expect(b.isUnlimited, isTrue);
      expect(b.usedFraction, isNull);
    });
  });

  group('Page', () {
    test('parses envelope and computes hasMore', () {
      final page = Page.fromJson({
        'items': [
          {'id': 'i1', 'account_id': 'a', 'balance_due': '0', 'total': '0'},
        ],
        'count': 5,
        'limit': 1,
        'offset': 0,
      }, Invoice.fromJson);
      expect(page.items, hasLength(1));
      expect(page.hasMore, isTrue);
    });
  });

  group('Env.resolveUrl', () {
    test('prefixes relative paths with the base url', () {
      expect(
        Env.resolveUrl('/static/avatars/x.png'),
        '${Env.apiBaseUrl}/static/avatars/x.png',
      );
    });
    test('leaves absolute urls unchanged', () {
      expect(
        Env.resolveUrl('https://cdn.example.com/a.png'),
        'https://cdn.example.com/a.png',
      );
    });
  });

  group('Subscription', () {
    test('parses plan type, IP and prepaid flag', () {
      final s = Subscription.fromJson({
        'id': 's1',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'status_presentation': {
          'value': 'active',
          'label': 'Service active',
          'tone': 'positive',
          'icon': 'check',
        },
        'billing_mode': 'prepaid',
        'ipv4_address': '10.11.128.186',
        'offer': {
          'name': 'unlimited 3',
          'service_type': 'business',
          'access_type': 'fiber',
        },
      });
      expect(s.ipv4Address, '10.11.128.186');
      expect(s.isPrepaid, isTrue);
      expect(s.planType, 'business · fiber');
      expect(s.displayName, 'unlimited 3');
      expect(s.statusPresentation.label, 'Service active');
      expect(s.statusPresentation.tone.name, 'positive');
    });

    test('computes negative days for an expired service', () {
      final s = Subscription.fromJson({
        'id': 's2',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'suspended',
        'billing_mode': 'prepaid',
        'next_billing_at': '2021-03-16T00:00:00Z',
      });
      expect(s.expiresAt, isNotNull);
      expect(s.daysUntilExpiry, isNotNull);
      expect(s.daysUntilExpiry! < 0, isTrue);
    });

    test('postpaid never expires on next_billing_at (no false expiry)', () {
      final s = Subscription.fromJson({
        'id': 's3',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'postpaid',
        'next_billing_at': '2020-01-01T00:00:00Z',
      });
      expect(s.hasExpiry, isFalse);
      expect(s.expiresAt, isNull);
      expect(s.daysUntilExpiry, isNull);
      expect(s.isExpired, isFalse);
      expect(s.expiresSoon, isFalse);
    });

    test('active service is never expired despite a stale billing date', () {
      final s = Subscription.fromJson({
        'id': 's4',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'prepaid',
        'next_billing_at': '2020-01-01T00:00:00Z',
      });
      expect(s.daysUntilExpiry! < 0, isTrue);
      expect(s.isExpired, isFalse, reason: 'active = running, not expired');
    });

    test('non-active prepaid with a past validity date is expired', () {
      final s = Subscription.fromJson({
        'id': 's5',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'suspended',
        'billing_mode': 'prepaid',
        'next_billing_at': '2020-01-01T00:00:00Z',
      });
      expect(s.isExpired, isTrue);
    });

    test('postpaid honours an explicit past contract end_at as expiry', () {
      final s = Subscription.fromJson({
        'id': 's6',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'stopped',
        'billing_mode': 'postpaid',
        'end_at': '2020-01-01T00:00:00Z',
      });
      expect(s.hasExpiry, isTrue);
      expect(s.isExpired, isTrue);
    });

    test(
      'prefers server is_expired/expires_at when the backend provides them',
      () {
        // Server says: active, no date expiry (prepaid lapses on balance, not
        // next_billing_at). Client must trust it over local date math.
        final s = Subscription.fromJson({
          'id': 's9',
          'account_id': 'a1',
          'offer_id': 'o1',
          'status': 'active',
          'billing_mode': 'prepaid',
          'next_billing_at': '2020-01-01T00:00:00Z',
          'expires_at': null,
          'is_expired': false,
        });
        expect(s.hasServerExpiry, isTrue);
        expect(s.expiresAt, isNull);
        expect(s.isExpired, isFalse);
      },
    );

    test('falls back to local logic when server fields are absent', () {
      final s = Subscription.fromJson({
        'id': 's10',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'prepaid',
        'next_billing_at': '2020-01-01T00:00:00Z',
      });
      expect(s.hasServerExpiry, isFalse);
      expect(
        s.expiresAt,
        isNotNull,
      ); // local fallback for older/offline backend
      expect(s.isExpired, isFalse); // active is never expired
    });

    test('expiresSoon only inside the 3-day window for date-based expiry', () {
      final soon = Subscription.fromJson({
        'id': 's7',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'prepaid',
        'next_billing_at':
            DateTime.now().add(const Duration(days: 2)).toIso8601String(),
      });
      expect(soon.expiresSoon, isTrue);
      final postpaid = Subscription.fromJson({
        'id': 's8',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'postpaid',
        'next_billing_at':
            DateTime.now().add(const Duration(days: 2)).toIso8601String(),
      });
      expect(postpaid.expiresSoon, isFalse);
    });

    Subscription withStatus(String status) => Subscription(
          id: 's',
          accountId: 'a',
          offerId: 'o',
          status: status,
          billingMode: 'prepaid',
        );

    test('isCurrent excludes terminal/historical statuses', () {
      for (final status in [
        'pending',
        'active',
        'blocked',
        'suspended',
        'stopped',
      ]) {
        expect(withStatus(status).isCurrent, isTrue, reason: status);
      }
      for (final status in [
        'disabled',
        'canceled',
        'expired',
        'hidden',
        'archived',
      ]) {
        expect(withStatus(status).isCurrent, isFalse, reason: status);
      }
    });
  });

  group('Ticket', () {
    test('parses the server-owned status presentation', () {
      final ticket = Ticket.fromJson({
        'id': 'ticket-1',
        'title': 'Slow browsing',
        'status': 'waiting_on_customer',
        'priority': 'normal',
        'status_presentation': {
          'value': 'waiting_on_customer',
          'label': 'Waiting on customer',
          'tone': 'warning',
          'icon': 'clock',
        },
      });

      expect(ticket.statusPresentation.label, 'Waiting on customer');
      expect(ticket.statusPresentation.tone.name, 'warning');
      expect(ticket.statusPresentation.icon, 'clock');
    });

    test('uses a neutral fallback for an old server payload', () {
      final ticket = Ticket.fromJson({
        'id': 'ticket-2',
        'title': 'Legacy ticket',
        'status': 'future_state',
        'priority': 'normal',
      });

      expect(ticket.statusPresentation.label, 'Future State');
      expect(ticket.statusPresentation.tone.name, 'neutral');
      expect(ticket.statusPresentation.icon, 'info');
    });
  });

  group('PlanChangeQuote', () {
    test('parses a prepaid proration quote', () {
      final q = PlanChangeQuote.fromJson({
        'charge_amount': 5000.0,
        'net_amount': 2900.0,
        'prepaid_funding_before': 5000.0,
        'prepaid_funding_after': 2100.0,
        'postpaid_receivables': 750.0,
        'collection_blocking_balance': 125.0,
        'shortfall': 0.0,
        'days_remaining': 12,
        'can_apply_immediately': true,
        'is_upgrade': true,
        'is_downgrade': false,
        'preview_fingerprint': List.filled(64, 'p').join(),
        'has_financial_effect': true,
        'ledger_entry_type': 'debit',
        'ledger_source': 'adjustment',
        'ledger_amount': 2900.0,
        'access_consequence': 'none_plan_change_only',
      });
      expect(q.hasProration, isTrue);
      expect(q.isUpgrade, isTrue);
      expect(q.needsTopUp, isFalse);
      expect(q.netAmount, 2900.0);
      expect(q.prepaidFundingAfter, 2100.0);
      expect(q.postpaidReceivables, 750.0);
      expect(q.collectionBlockingBalance, 125.0);
      expect(q.previewFingerprint, hasLength(64));
      expect(q.ledgerEntryType, 'debit');
      expect(q.ledgerSource, 'adjustment');
      expect(q.ledgerAmount, 2900.0);
      expect(q.accessConsequence, 'none_plan_change_only');
    });

    test('empty quote => no proration (postpaid)', () {
      final q = PlanChangeQuote.fromJson(const {});
      expect(q.hasProration, isFalse);
    });

    test(
      'postpaid quote still carries a confirmable no-ledger fingerprint',
      () {
        final q = PlanChangeQuote.fromJson({
          'preview_fingerprint': List.filled(64, 'n').join(),
          'preview_effective_at': '2026-07-22T10:00:00Z',
          'has_financial_effect': false,
          'postpaid_receivables': 750.0,
          'collection_blocking_balance': 0.0,
          'access_consequence': 'none_plan_change_only',
          'delivery_mode': 'remote_reprovision',
        });
        expect(q.hasProration, isFalse);
        expect(q.hasFinancialEffect, isFalse);
        expect(q.previewFingerprint, hasLength(64));
        expect(q.previewEffectiveAt, isNotNull);
        expect(q.postpaidReceivables, 750.0);
        expect(q.deliveryMode, 'remote_reprovision');
        expect(q.appliesImmediately, isFalse);
        expect(q.requiresSiteVisit, isFalse);
      },
    );

    test('field delivery requires a site visit', () {
      final q = PlanChangeQuote.fromJson({
        'delivery_mode': 'field_migration',
        'field_delivery_quote': {
          'target_service_address_id': 'address-2',
          'target_address_label': 'New site, Abuja',
          'qualification_status': 'eligible',
          'eligible': true,
          'preview_fingerprint': List.filled(64, 'f').join(),
          'fee_amount': '25000.00',
          'currency': 'NGN',
        },
      });
      expect(q.deliveryLabel, 'Field migration');
      expect(q.requiresSiteVisit, isTrue);
      expect(q.fieldDeliveryQuote?.targetServiceAddressId, 'address-2');
      expect(q.fieldDeliveryQuote?.qualificationStatus, 'eligible');
      expect(q.fieldDeliveryQuote?.feeAmount, 25000.0);
      expect(q.fieldDeliveryQuote?.eligible, isTrue);
    });

    test('flags a shortfall as needing top-up', () {
      final q = PlanChangeQuote.fromJson({
        'net_amount': 5000.0,
        'shortfall': 3000.0,
      });
      expect(q.needsTopUp, isTrue);
    });
  });

  group('LedgerTxn', () {
    test('credit entry: sign/colour flag + payment title', () {
      final t = LedgerTxn.fromJson({
        'id': 'l1',
        'entry_type': 'credit',
        'source': 'payment',
        'amount': '52000.00',
        'currency': 'NGN',
        'memo': 'Zenith 461 Bank',
        'created_at': '2026-03-15T10:00:00Z',
        'effective_date': '2026-03-14T09:00:00Z',
        'payment_id': 'p1',
        'invoice_id': 'i1',
      });
      expect(t.isCredit, isTrue);
      expect(t.amount, 52000.0);
      expect(t.title, 'Zenith 461 Bank');
      expect(t.occurredAt, t.effectiveAt);
      expect(t.paymentId, 'p1');
      expect(t.invoiceId, 'i1');
    });

    test('debit falls back to source label when memo is empty', () {
      final t = LedgerTxn.fromJson({
        'id': 'l2',
        'entry_type': 'debit',
        'source': 'invoice',
        'amount': 56437.5,
        'currency': 'NGN',
      });
      expect(t.isCredit, isFalse);
      expect(t.title, 'Charge');
    });
  });

  group('AccountBalance', () {
    test('positive credit / negative owes', () {
      expect(
        AccountBalance.fromJson({'credit_balance': '2071.49'}).inCredit,
        isTrue,
      );
      expect(AccountBalance.fromJson({'credit_balance': -500}).owes, isTrue);
      final zero = AccountBalance.fromJson({'credit_balance': '0.00'});
      expect(zero.inCredit, isFalse);
      expect(zero.owes, isFalse);
    });
  });

  group('Topup', () {
    test(
      'TopupPage keeps payable-invoice visibility while deposit stays allowed',
      () {
        final page = TopupPage.fromJson({
          'provider_type': 'paystack',
          'currency': 'NGN',
          'min_amount': 1000,
          'max_amount': 500000,
          'deposit_allowed': true,
          'eligible_unpaid_total': '18000.00',
          'eligible_unpaid_invoices': [
            {'invoice_id': 'inv-1', 'invoice_number': 'INV-1'}
          ],
        });
        expect(page.depositAllowed, isTrue);
        expect(page.eligibleUnpaidTotal, 18000.0);
        expect(page.eligibleUnpaidInvoices.single['invoice_number'], 'INV-1');
      },
    );

    test(
      'TopupPreview parses invoice application breakdown and fingerprint',
      () {
        final preview = TopupPreview.fromJson({
          'account_id': 'acct-1',
          'currency': 'NGN',
          'current_account_credit': '0.00',
          'requested_deposit': '10000.00',
          'eligible_invoice_count': 1,
          'invoice_applications': [
            {
              'invoice_id': 'inv-1',
              'invoice_number': 'INV-1',
              'currency': 'NGN',
              'amount_applied': '10000.00',
              'outstanding_after_application': '8000.00',
            }
          ],
          'total_applied_to_invoices': '10000.00',
          'total_outstanding_after_application': '8000.00',
          'remaining_account_credit': '0.00',
          'projected_available_credit': '0.00',
          'allocation_policy': 'credit_only',
          'credit_application_policy': 'pay_eligible_invoices',
          'policy_version': 1,
          'preview_fingerprint': List.filled(64, 'd').join(),
        });
        expect(preview.eligibleInvoiceCount, 1);
        expect(preview.invoiceApplications.single.amountApplied, 10000.0);
        expect(preview.previewFingerprint, hasLength(64));
      },
    );
  });

  group('Add-ons', () {
    test('AddonsAvailable parses options without deriving a balance', () {
      final d = AddonsAvailable.fromJson({
        'available': [
          {
            'add_on_id': 'a1',
            'name': 'Static IP',
            'addon_type': 'static_ip',
            'amount': 2000.0,
            'currency': 'NGN',
            'min_quantity': 1,
            'max_quantity': 3,
          },
        ],
        'active': [
          {'id': 's1', 'add_on_id': 'a1', 'name': 'Static IP', 'quantity': 2},
        ],
      });
      expect(d.available.single.maxQuantity, 3);
      expect(d.active.single.quantity, 2);
    });

    test('AddonQuote keeps funding and receivables visibly distinct', () {
      final q = AddonQuote.fromJson({
        'charge': '2000.00',
        'currency': 'NGN',
        'prepaid_funding_before': '5000.00',
        'prepaid_funding_after': '3000.00',
        'postpaid_receivables': '750.00',
        'shortfall': '0.00',
        'can_afford': true,
        'allowed': true,
        'preview_fingerprint': List.filled(64, 'a').join(),
      });
      expect(q.prepaidFundingBefore, 5000.0);
      expect(q.prepaidFundingAfter, 3000.0);
      expect(q.postpaidReceivables, 750.0);
      expect(q.previewFingerprint, hasLength(64));
    });

    test('AddonPurchaseResult flags insufficient prepaid funding', () {
      final r = AddonPurchaseResult.fromJson({
        'success': false,
        'reason': 'insufficient_prepaid_funding',
        'charge': '4000.00',
        'shortfall': '2000.00',
        'currency': 'NGN',
      });
      expect(r.success, isFalse);
      expect(r.insufficient, isTrue);
      expect(r.shortfall, 2000.0);
    });
  });

  group('SavedCard', () {
    test('derives title and expiry, defaults', () {
      final c = SavedCard.fromJson({
        'id': 'p1',
        'method_type': 'card',
        'label': 'Visa •••• 4081',
        'last4': '4081',
        'brand': 'visa',
        'expires_month': 8,
        'expires_year': 2030,
        'is_default': true,
      });
      expect(c.title, 'Visa •••• 4081');
      expect(c.expiry, '08/30');
      expect(c.isDefault, isTrue);
    });

    test('falls back to brand + last4 when no label', () {
      final c = SavedCard.fromJson({
        'id': 'p2',
        'brand': 'mastercard',
        'last4': '1234',
      });
      expect(c.title, 'mastercard •••• 1234');
      expect(c.expiry, isNull);
    });
  });

  group('AppNotification', () {
    test('uses the subject as the title', () {
      final n = AppNotification.fromJson({
        'id': 'n1',
        'channel': 'email',
        'status': 'queued',
        'is_read': true,
        'subject': 'Service suspended — action required',
        'event_type': 'service_suspended',
      });
      expect(n.title, 'Service suspended — action required');
      expect(n.isRead, isTrue);
    });

    test('falls back to a humanised event_type when no subject', () {
      final n = AppNotification.fromJson({
        'id': 'n2',
        'channel': 'sms',
        'status': 'delivered',
        'event_type': 'payment_received',
      });
      expect(n.title, 'payment received');
      expect(n.isRead, isFalse);
    });
  });

  group('AuthSessionInfo', () {
    test('labels a native-app session and flags current', () {
      final s = AuthSessionInfo.fromJson({
        'id': 's1',
        'status': 'active',
        'is_current': true,
        'ip_address': '102.89.1.2',
        'user_agent': 'Dart/3.12 (dart:io)',
      });
      expect(s.isCurrent, isTrue);
      expect(s.deviceLabel, 'Mobile app');
    });

    test('derives os/browser from a desktop user agent', () {
      final s = AuthSessionInfo.fromJson({
        'id': 's2',
        'status': 'active',
        'user_agent':
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120 Safari/537.36',
      });
      expect(s.isCurrent, isFalse);
      expect(s.deviceLabel, contains('Macintosh'));
      expect(s.deviceLabel, contains('Chrome'));
    });
  });

  group('Payment flow', () {
    test('parses initiation', () {
      final init = PaymentInitiation.fromJson({
        'invoice_id': 'i1',
        'amount': '2500.00',
        'currency': 'NGN',
        'provider_type': 'paystack',
        'provider_public_key': 'pk',
        'payment_reference': 'ref',
        'customer_email': 'c@e.com',
      });
      expect(init.amount, 2500.0);
      expect(init.providerType, 'paystack');
      expect(init.paymentReference, 'ref');
    });

    test('parses verification success', () {
      final v = PaymentVerification.fromJson({
        'reference': 'ref',
        'payment_id': 'p1',
        'amount': '2500.00',
        'currency': 'NGN',
        'status': 'succeeded',
        'already_recorded': false,
      });
      expect(v.succeeded, isTrue);
      expect(v.paymentId, 'p1');
    });
  });

  group('AccountingSession framed IP + LiveBandwidth', () {
    test('parses framed_ip_address from the session payload', () {
      final s = AccountingSession.fromJson({
        'id': '1',
        'subscription_id': 'sub',
        'session_id': 'sess',
        'status_type': 'start',
        'framed_ip_address': '100.64.3.7',
      });
      expect(s.framedIpAddress, '100.64.3.7');
    });

    test('LiveBandwidth binds subscriber-perspective fields only', () {
      final b = LiveBandwidth.fromJson({
        'current_rx_bps': 1, // NAS perspective — intentionally ignored
        'download_bps': 12000000.0,
        'upload_bps': 3000000.0,
      });
      expect(b.downloadBps, 12000000.0);
      expect(b.uploadBps, 3000000.0);
      expect(b.hasSignal, isTrue);
      expect(LiveBandwidth.fromJson(const {}).hasSignal, isFalse);
    });
  });

  group('AccountHealth', () {
    test('parses financial lanes, service evidence, and canonical action', () {
      final health = AccountHealth.fromJson({
        'account_id': 'a1',
        'display_name': 'Amaka Okafor',
        'lifecycle': {
          'value': 'active',
          'label': 'Active',
          'tone': 'positive',
          'icon': 'check',
        },
        'financial': {
          'billing_mode': {'kind': 'present', 'value': 'Prepaid'},
          'receivables': {
            'kind': 'present',
            'value': [
              {
                'currency': 'NGN',
                'outstanding': '1250.00',
                'overdue': '250.00',
                'overdue_count': 1,
              },
            ],
          },
          'prepaid_funding': {
            'kind': 'present',
            'value': {'amount': '50.00', 'currency': 'NGN'},
          },
        },
        'primary_action': {
          'kind': 'top_up',
          'label': 'Top up',
          'message': 'Balance low — top up NGN 50.00 to keep your service.',
          'amount': '50.00',
          'currency': 'NGN',
          'restores_service': false,
        },
        'services': [
          {
            'subscription_id': 's1',
            'offer_name': 'Fibre 100',
            'lifecycle': {
              'value': 'active',
              'label': 'Active',
              'tone': 'positive',
              'icon': 'check',
            },
            'billing_mode': 'prepaid',
            'access_state': 'available',
            'access': {
              'value': 'available',
              'label': 'Available',
              'tone': 'positive',
              'icon': 'check',
            },
            'access_reason': 'No access hold is active.',
            'session': {
              'state': 'connected',
              'binding': 'exact_subscription',
              'observed_at': '2026-07-22T10:00:00Z',
              'framed_ip_address': '100.64.1.2',
              'nas_device_id': null,
            },
            'session_presentation': {
              'value': 'connected',
              'label': 'Connected',
              'tone': 'positive',
              'icon': 'check',
            },
            'connection': {
              'kind': 'present',
              'value': {
                'state': 'connected',
                'status_presentation': {
                  'value': 'connected',
                  'label': 'Connected',
                  'tone': 'positive',
                  'icon': 'check',
                },
                'headline': "You're connected",
                'message': 'Connection healthy.',
                'advice': null,
                'medium': 'fiber',
                'area_outage': false,
                'checked_at': '2026-07-22T10:00:00Z',
              },
            },
            'next_action': {
              'kind': 'top_up',
              'label': 'Top up',
              'message': 'Balance low — top up NGN 50.00 to keep your service.',
              'amount': '50.00',
              'currency': 'NGN',
              'restores_service': false,
            },
            'pending_change': {
              'request_id': 'change-1',
              'status': 'pending',
              'target_offer_name': 'Wireless 100',
              'effective_date': '2026-07-25',
              'delivery_mode': 'remote_reprovision',
            },
          },
        ],
        'as_of': '2026-07-22T10:00:00Z',
      });
      expect(health.financial.prepaidFunding.value?.amount, 50.0);
      expect(health.financial.receivables.value?.single.outstanding, 1250.0);
      expect(health.services.single.session.isOnline, isTrue);
      expect(health.services.single.connection.value?.isConnected, isTrue);
      expect(health.services.single.nextAction?.isFinancial, isTrue);
      expect(health.services.single.pendingChange?.requestId, 'change-1');
      expect(
        health.services.single.pendingChange?.deliveryMode,
        'remote_reprovision',
      );
      expect(health.primaryAction?.kind, 'top_up');
    });

    test('keeps unavailable values distinct from authoritative zero', () {
      final value = AvailableValue<MoneyAmount>.fromJson({
        'kind': 'unavailable',
        'value': null,
      }, (raw) => MoneyAmount.fromJson((raw as Map).cast<String, dynamic>()));
      expect(value.isPresent, isFalse);
      expect(value.value, isNull);
    });
  });
}
