# Funded prepaid renewal invoicing

Status: implementation ready for review; becomes active only after deployment

Owner: `financial.prepaid_service_renewals`

## Decision

Every new cash-funded prepaid service period has one paid invoice. The renewal
owner coordinates the transaction, while `financial.invoices` owns document
construction and issuance and the existing payment/opening-funding participants
own settlement evidence.

This is not recurring debt billing. If the full prepaid charge is unavailable,
the owner creates no invoice, consumes no partial credit, creates no entitlement,
and does not advance `Subscription.next_billing_at`.

## Workflow

For one eligible due period, the owner:

1. locks the customer account and rechecks the fingerprinted funding preview;
2. resolves the exact frozen subscription price, tax, currency, cadence, and
   service period;
3. creates one draft invoice and one base-subscription line through the invoice
   participant, using a deterministic period line key;
4. requires the existing prepaid draft reconciler to issue and fully settle that
   exact invoice from settlement-backed payment credit and, where approved,
   reviewed opening funding;
5. derives one active entitlement from the paid invoice line;
6. projects the subscription paid-through anchor from that entitlement while
   preserving any later boundary supported by other exact coverage; and
7. stages the version-2 `prepaid_service.renewed` outcome with the invoice and
   entitlement identities.

All effects commit or roll back together under one renewal owner command. A
settlement, entitlement, anchor, or posting failure cannot leave an unpaid or
partially constructed renewal invoice.

## Invariants

- One subscription period has one deterministic active renewal invoice line.
- A new renewal never writes the retired invoice-less `AccountAdjustment` debit.
- An invoice is considered funded only with exact payment allocation or reviewed
  opening-funding application evidence.
- Invoice period, line period, and entitlement period are identical. The next
  paid-through boundary reaches at least that end and may remain later only when
  other exact entitlement or grant evidence supports it.
- A paid invoice is the customer-position service-consumption debit; no parallel
  adjustment is counted.
- Replay returns the same invoice and entitlement and creates no duplicate money,
  document, access, or event effect.
- Historical direct-renewal adjustments remain readable for replay, reversal, and
  reconciliation. They are not a writer fallback for new periods.

## Timing boundary

This change does not alter the scheduled runner's two-day stale-anchor cutoff.
Current due periods and payment-triggered lapsed renewals use their existing
period-selection policy. Reviewed missed-period execution uses the operator-
approved fingerprint and now produces the same paid-invoice evidence as a normal
funded renewal.

## Verification and rollout

Behavior tests prove fully funded creation and settlement, insufficient-funding
non-creation, tax and period identity, payment-backed and reviewed-opening paths,
idempotent replay, transaction rollback, balance correctness, and the renewed
event contract. Architecture tests prohibit the direct adjustment writer from
returning to the renewal confirmation path.

Deployment mutates no historical customer data. Existing gaps are reconciled
after deployment through the fingerprint-reviewed missed-renewal command, one
customer period at a time.
