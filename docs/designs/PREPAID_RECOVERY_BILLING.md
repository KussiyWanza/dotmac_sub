# Prepaid recovery billing

## Intent

An administrator may replace a voided, unresolved prepaid renewal draft for a
service that is suspended specifically by an active `prepaid` enforcement lock.
The replacement is a full recurring cycle beginning at the confirmed Bill Now
instant. Its end becomes the service's next billing anchor only after the
invoice is fully settled.

## Ownership and boundaries

`financial.prepaid_recovery_billing` owns only the cross-domain coordination:

1. create one recovery draft for the exact suspended prepaid service;

It does not void or settle invoices, change service status directly, spend the
generic account balance, or decide that a credit note is valid. Invoice voiding
remains the invoice owner. Credit notes remain the credit-note owner. All
prepaid draft classification and settlement, including recovery drafts, belongs
to `financial.prepaid_draft_reconciliation`.

## Operator flow

1. The Bill Now eligibility read checks every active unresolved invoice with an
   active positive line for the exact subscription. If one exists, the service
   page disables Bill Now and links to that invoice. The administrator reviews
   and, only when legitimate, closes it through the invoice owner.
2. On the suspended prepaid service, select **Bill now** and review the exact
   full-cycle draft preview. Confirmation creates a draft only.
3. Open that draft invoice and select **Reconcile prepaid draft** when the
   authoritative owner offers the action.
4. Review its exact payment-backed and reviewed-opening funding breakdown,
   provide an operator reason, and explicitly confirm the signed preview.
5. `financial.prepaid_draft_reconciliation` locks and rechecks the evidence,
   then performs the exact settlement, entitlement, billing-anchor, and eligible
   access consequences atomically.

## Safety invariants

- The backend requires prepaid mode, suspended lifecycle state, and an active
  prepaid lock; hiding or showing a button is never authorization.
- Any active unresolved ordinary or recovery invoice with a positive line for
  the exact service blocks replacement. Payment allocations, ledger entries,
  credit-note activity, non-draft lifecycle state, overlapping entitlement, or
  multiple documents select a typed manual-review action; a pristine single
  draft selects the existing draft reconciler. Bill Now never voids it.
- The account is locked before the exact subscription and the service-scoped
  invoice query is repeated under lock. Unrelated subscriptions on the account
  do not block creation.
- Replay of the same confirmed recovery fingerprint returns the matching draft
  without creating another invoice.
- Draft creation binds the price, tax, period, and subscription state.
- Reconciliation is all-or-nothing. Insufficient, unbacked, stale, or ambiguous
  funding leaves the invoice and access unchanged.
- Credit notes are never treated as generic balance. Their application is a
  separate, fingerprint-bound command with its own funding evidence.
- The coordinator does not clear non-prepaid locks. The financial access owner
  performs the final remaining-lock gate and produces the normal resume event.

## Recovery and audit

The invoice line stores its recovery intent, exact subscription, and period
metadata. The draft-reconciliation owner records payment allocations, typed
opening-funding consumption when needed, entitlement, audit/event evidence, and
idempotency outcome. A retry is a stable replay; stale or incomplete evidence
produces no partial settlement.
