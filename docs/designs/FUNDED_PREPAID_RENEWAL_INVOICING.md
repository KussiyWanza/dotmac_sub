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
Current due periods retain their existing selection policy. A payment-triggered
renewal starts after exact uninterrupted entitlement or applied-extension
coverage containing the payment instant; without exact coverage, a lapsed
renewal starts on the payment's WAT business date. Mutable anchors and canceled
or reversed extensions do not defer the period. Reviewed missed-period execution
uses the operator-approved fingerprint and produces the same paid-invoice
evidence as a normal funded renewal.

## Reviewed legacy tax-invoice correction

Finance may require an invoice for a historical period that already has a
base-only `AccountAdjustment` debit and an adjustment-backed entitlement. That
case is not a missed renewal: creating another normal renewal would duplicate
the service and customer-position debit.

The correction owner accepts an explicit account, subscription, adjustment,
entitlement, expected tax-inclusive invoice total, and expected remaining
credit. Its read-only preview proves that they form one exact active legacy
renewal chain, that the current canonical exclusive-tax treatment produces the
approved total, that no invoice competes for the period, and that reversing the
old debit restores exactly enough payment-backed credit to settle the invoice.
Any mismatch returns `manual_review` and changes nothing.

The fingerprint-bound command then locks the account and selected records and,
in one owner transaction:

1. reverses the exact historical adjustment through its registered participant;
2. marks only the linked legacy entitlement as replaced;
3. creates and issues one canonical tax-inclusive invoice and base line;
4. fully settles that invoice from the restored and retained payment credit;
5. creates the replacement invoice-backed entitlement for the identical period;
6. recomputes the paid-through anchor from exact coverage; and
7. records the operator audit and
   `prepaid_service.renewal_document_corrected` event.

The invoice metadata preserves the adjustment, replaced entitlement, reversal,
preview, actor, and approval-reference evidence. Replay returns the same
invoice. There is no customer-specific branch, raw SQL repair, balance override,
or second service period.

## Verification and rollout

Behavior tests prove fully funded creation and settlement, insufficient-funding
non-creation, tax and period identity, payment-backed and reviewed-opening paths,
idempotent replay, transaction rollback, balance correctness, and the renewed
event contract. Architecture tests prohibit the direct adjustment writer from
returning to the renewal confirmation path.

Deployment mutates no historical customer data. A genuinely missing period is
reconciled through the reviewed missed-renewal command. A period that already
has an invoice-less adjustment-backed renewal uses the separate reviewed legacy
tax-invoice correction runbook, one explicitly approved account period at a
time.
