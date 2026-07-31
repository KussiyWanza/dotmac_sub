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

1. The administrator reviews and manually voids the obsolete draft invoice.
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
- One active recovery invoice per service is allowed. An existing draft must be
  explicitly voided; Bill Now never voids it automatically.
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
