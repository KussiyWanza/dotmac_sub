# Prepaid draft invoice reconciliation

Status: cut over for new funding events; historical application remains
dry-run-first and operator reviewed.

Owner: `financial.prepaid_draft_reconciliation`

## Problem

A prepaid billing period could be represented by two competing write paths:

1. monthly billing created a draft invoice and projected a future billing
   anchor; and
2. a later account-credit event skipped drafts, posted a direct renewal debit,
   created entitlement, and advanced the anchor without closing the draft.

The same visible symptom also occurs when a draft is almost funded. For example,
an NGN 18,812.50 invoice with only NGN 18,812.00 of exact payment-backed credit
is short NGN 0.50. That is insufficient funding, not a rounding condition.

A third case exists after the reviewed prepaid opening-position cutover: the
account can have enough authoritative funding even though settlement-backed
Payments alone do not cover the draft. The remaining value belongs to the
signed opening baseline. Treating that value as a Payment would destroy its
provenance; ignoring it leaves a funded customer locked and a draft stranded.

A fourth historical case is an onboarding proforma created before its prepaid
subscription. The document can later have exact native payment-backed credit
while remaining outside financial allocation because it is still a proforma,
has no service period, and its line has no subscription identity. Generic
proforma conversion is insufficient: issuing such a document would settle the
receivable without creating authoritative entitlement or a billing anchor.

## Canonical policy

- `financial.invoices` owns invoice lifecycle and document state.
- `financial.account_credit_applications` owns the exact payment-backed credit
  projection and payment allocation.
- `financial.prepaid_funding_reconstruction` owns the signed, reviewed opening
  baseline. It remains a funding source, never a Payment.
- `financial.prepaid_service_renewals` owns direct renewal debit and entitlement
  evidence when no authoritative draft exists.
- `financial.prepaid_draft_reconciliation` is the only classifier and repair
  coordinator when a prepaid draft already exists. It alone writes opening
  funding consumption and durable reconciliation exceptions.

The same reconciliation owner coordinates reviewed adoption of a funded
onboarding proforma. Adoption is deliberately narrower than generic proforma
conversion. It requires all of the following exact evidence:

- one operator-named subscription belonging to the invoice account;
- active prepaid state with no billing anchor or active entitlement;
- one positive, unlinked proforma line whose quantity, unit price, and amount
  equal the frozen subscription base charge;
- internally exact subtotal, tax, gross total, and balance;
- no invoice financial activity and no competing financial draft;
- one sole native payment-backed source equal to the full gross balance;
- a successful verified funding-baseline read; and
- an authoritative `paid_at` timestamp and contracted billing cadence.

The sole payment instant is resolved through the canonical Africa/Lagos
settlement calendar. The invoice participant writes only the missing period,
subscription link, line provenance, and non-proforma document identity. The
adoption command posts no money, issues no invoice, creates no entitlement, and
does not advance `next_billing_at`. It commits an idempotency reservation,
audit, metadata, and `prepaid_proforma.adopted` event. The resulting valid
financial draft is then previewed and settled by the ordinary fingerprint-bound
draft reconciliation command. A failure between the commands leaves a valid,
visible prepaid draft that can be safely replayed; it never leaves an issued or
partially settled invoice.

An existing prepaid draft has first claim on the service-period document
boundary. A funding-change consequence checks it before an invoice-less direct
renewal:

- exact native payment-backed funding equal to or above the full balance:
  issue and fully settle the draft atomically;
- exact payment-backed funding plus enough unused reviewed opening funding:
  require operator confirmation, allocate Payments first, consume only the
  remaining opening amount, and settle the invoice atomically;
- an automatic funding event that reaches the mixed-source case: create or
  refresh one durable reconciliation exception and one operator alert without
  spending either source;
- any shortfall, including NGN 0.50: keep the draft unchanged and do not create
  entitlement;
- unbacked credit whose economic timestamp or Sub creation time crosses the
  active reviewed opening-position boundary: keep the draft unchanged for
  evidence reconstruction;
- one exact direct-renewal debit and entitlement overlapping the draft: close
  the duplicate draft through the invoice owner with zero economic delta;
- multiple drafts, mixed lines, partial activity, or ambiguous coverage:
  require manual review.

When an active reviewed opening baseline exists, account-credit classification
uses only native payment and ledger facts crossing its timestamp. Pre-boundary
rows are already absorbed into the signed opening amount: they are neither
reused as Payments nor quarantined again as current unbacked credit. Without an
active baseline, the generic all-history payment-backed classification remains
unchanged.

No path rounds a shortfall, invents a payment, represents opening funding as a
Payment, marks an underfunded invoice paid, double-spends an opening baseline,
or creates a second entitlement.

## Atomic mixed-source confirmation

The owner locks the customer account, invoice, eligible payment/settlement
records, and opening baseline in that order. Inside one owner transaction it:

1. issues the eligible draft at the reviewed effective time;
2. allocates settlement-backed Payments first and receives the exact
   post-allocation receivable from that participant outcome;
3. records the exact remainder in
   `PrepaidOpeningFundingConsumption`, linked to the baseline, invoice,
   structural ledger entry, approval evidence, preview fingerprint, and
   idempotency key;
4. recalculates the invoice to paid with zero balance;
5. re-anchors a lapsed service period to the effective payment/reconciliation
   time, creates the exact entitlement, and projects `next_billing_at` from the
   entitlement end;
6. clears eligible billing locks and asks the lifecycle/access owners to restore
   service and RADIUS projection; and
7. stages audit, event, and exception-resolution evidence before one commit.

Any stale preview, consumed baseline, reversal, post-boundary ambiguous ledger
credit, multiple draft, or partial result rolls the whole transaction back.
The participant remainder must equal the previewed typed-source requirement by
exact Decimal equality. A disagreement with the invoice owner's receivable is
an incomplete participant outcome and also rolls back; the coordinator does not
re-read a mutable displayed balance as an alternative funding domain.

For fee-inclusive provider receipts, `Payment.amount` remains the gross charge
and `Payment.provider_fee` remains separate receipt evidence. Classification and
allocation use only `PaymentSettlement.amount` and its canonical unallocated
capacity. Gross amount can never increase fundable credit.

Generic subscription Restore is not a financial repair path. An active prepaid
financial lock makes Restore ineligible and the admin UI sends the operator to
the draft-invoice reconciliation queue.

## Preview and confirmation

`scripts/billing/reconcile_prepaid_drafts.py` is read-only by default. It reports
the disposition, recommended action, authoritative funding, exact
payment-backed credit, reviewed opening funding available/required, unbacked
credit, shortfall, evidence identifiers, and a SHA-256 evidence fingerprint.
Invoke it from the repository root as a module:

```bash
poetry run python -m scripts.billing.reconcile_prepaid_drafts
poetry run python -m scripts.billing.reconcile_prepaid_drafts \
  --invoice-id INVOICE_UUID
```

For a reviewed onboarding proforma, preview and apply documentary adoption
first. Preview derives the period and exposes the sole payment identity; apply
requires the exact fingerprint but does not accept an operator-chosen service
date:

```bash
poetry run python -m scripts.billing.reconcile_prepaid_drafts \
  --adopt-proforma \
  --invoice-id INVOICE_UUID \
  --subscription-id SUBSCRIPTION_UUID

poetry run python -m scripts.billing.reconcile_prepaid_drafts \
  --adopt-proforma \
  --apply \
  --invoice-id INVOICE_UUID \
  --subscription-id SUBSCRIPTION_UUID \
  --fingerprint REVIEWED_SHA256 \
  --idempotency-key prepaid-proforma-INVOICE_UUID-v1 \
  --actor operator@example.com \
  --reason "Reviewed exact onboarding document and payment evidence"
```

After adoption, run the ordinary invoice preview and apply shown below, using
the previewed settlement payment timestamp as `--effective-at`.

Apply is limited to one reviewed invoice and requires:

- the exact preview fingerprint;
- an effective timestamp;
- a stable idempotency key;
- an actor and reason.

```bash
poetry run python -m scripts.billing.reconcile_prepaid_drafts \
  --apply \
  --invoice-id INVOICE_UUID \
  --fingerprint REVIEWED_SHA256 \
  --effective-at 2026-07-23T12:00:00Z \
  --idempotency-key prepaid-draft-INVOICE_UUID-v1 \
  --actor operator@example.com \
  --reason "Reviewed exact funding evidence"
```

The owner recomputes the preview after locking and fails closed if any source
fact changed. Invoice transition, payment allocations, opening consumption,
entitlement/anchor projection, eligible access restoration, audit/event
evidence, exception resolution, metadata, and idempotency reservation commit
together.

The admin invoice page is the interactive adapter to the same owner. It shows
the exact payment-backed, reviewed-opening, unbacked, and shortfall amounts plus
the recommended action and evidence identifiers. An authorized operator must
provide a reason and explicitly confirm the review. A short-lived signed token
binds the actor, invoice, owner fingerprint, and effective timestamp; the token
identifier becomes the stable idempotency key. Expired, altered, or stale
reviews fail closed and issue a fresh preview without changing the invoice.

The retired prepaid-recovery settlement command is not a fallback. Recovery
draft creation remains owned by `financial.prepaid_recovery_billing`, while
every resulting prepaid draft is classified and reconciled here regardless of
which approved path created it.

## Rollout

1. Deploy the funding-change draft-first guard.
2. Run the full dry-run cohort and retain the reviewed JSON.
3. Preview exact funded onboarding proformas one invoice/subscription pair at a
   time; adopt one canary and verify that only documentary identity changed.
4. Preview the adopted financial draft and settle it through the ordinary
   reconciler; verify entitlement and billing anchor before continuing.
5. Apply exact payment-backed cases in small canary batches, one invoice per
   command.
6. Apply mixed settlement/opening cases only where the signed baseline and
   approval evidence are verified.
7. Apply exact direct-renewal overlap closures separately.
8. Reconstruct legacy/unbacked funding only through its evidence owner; then
   re-preview.
9. Leave insufficient, multiple-draft, reversed/refunded, and ambiguous cases
   unchanged.
10. After every canary, verify invoice and ledger facts, opening consumption,
   entitlement and billing anchor, enforcement locks, billing events, and
   RADIUS access. Stop on any mismatch.

This change does not mutate historical customer records during deployment.
Backlog state changes occur only through an explicit reviewed apply command.
