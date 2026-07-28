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
2. issue and settle that recovery draft from confirmed payment-backed credit;
3. request normal financial-access restoration after paid invoice evidence.

It does not void invoices, change a service status directly, spend the generic
account balance, or decide that a credit note is valid. Invoice voiding remains
the invoice owner. Credit notes remain the credit-note owner and are applied by
an administrator before Pay Now. Payment allocation, paid-invoice entitlement,
and lock resolution remain their existing owners.

## Operator flow

1. The administrator reviews and manually voids the obsolete draft invoice.
2. On the suspended prepaid service, select **Bill now** and review the exact
   full-cycle draft preview. Confirmation creates a draft only.
3. On that draft invoice, apply an eligible issued credit note if appropriate.
4. Select **Pay now**. It is allowed only when confirmed, unallocated payment
   credit can cover the remaining receivable in full.
5. The command issues and settles the invoice, creates the exact entitlement,
   advances `next_billing_at` to the invoice period end, and then re-evaluates
   financial locks. Access returns only if no other lock remains.

## Safety invariants

- The backend requires prepaid mode, suspended lifecycle state, and an active
  prepaid lock; hiding or showing a button is never authorization.
- One active recovery invoice per service is allowed. An existing draft must be
  explicitly voided; Bill Now never voids it automatically.
- A preview binds the price, tax, period, subscription state, and payment-credit
  capacity. Confirmation locks the account, service, and invoice and rejects
  stale evidence.
- Pay Now is all-or-nothing. Insufficient confirmed payment credit leaves the
  invoice a draft and leaves access unchanged.
- Credit notes are never treated as generic balance. Their application is a
  separate, fingerprint-bound command with its own funding evidence.
- The coordinator does not clear non-prepaid locks. The financial access owner
  performs the final remaining-lock gate and produces the normal resume event.

## Recovery and audit

The invoice line stores its recovery intent, exact subscription, and period
metadata. Payment allocations and the paid-invoice entitlement are the durable
financial and service evidence. A retry of an already-paid invoice is a stable
replay; a stale or incomplete action produces no partial settlement.
