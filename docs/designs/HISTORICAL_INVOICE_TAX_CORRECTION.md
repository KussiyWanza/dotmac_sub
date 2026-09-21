# Historical Invoice Tax Correction

## Decision

`financial.historical_invoice_tax_corrections` owns the exceptional,
Finance-reviewed correction of one paid invoice that omitted VAT. Existing
issued invoice lines remain immutable. A tax-only line, generic adjustment,
raw database update, or multi-commit operator sequence is not a substitute.

The correction is deliberately narrow. One preview must prove all of the
following for a single customer and currency:

- the source invoice is active, paid, base-only, and funded by one exact native
  payment allocation with structural ledger evidence;
- the subscription invoice is one pristine positive draft;
- a separate, already-voided and never-funded Finance draft proves the intended
  installation description, taxable base, tax rate, and gross total;
- the selected payment is succeeded, unrefunded, unreversed, and has no other
  active allocations;
- current payment-backed customer credit equals exactly the subscription total
  plus the missing tax; and
- the current customer policy is taxable and the selected active TaxRate still
  matches the immutable snapshot on the voided Finance draft.

## Atomic outcome

Confirmation enters `execute_owner_command` once and performs these steps in
one transaction:

1. `financial.invoices` voids the incorrect source invoice and releases its
   exact payment allocation through append-only reversal evidence.
2. `financial.invoices` issues the named subscription draft with explicit
   manual due-date provenance.
3. `financial.account_credit_applications` settles that invoice from the named
   payment only.
4. `financial.invoices` constructs and issues a new full installation invoice
   with the reviewed VAT rate and snapshot.
5. `financial.account_credit_applications` settles the replacement from the
   same payment only.
6. The replacement records typed lineage to the source invoice and line, source
   closure, voided Finance evidence, subscription invoice, payment, tax rate,
   and both new allocations. Audit and a PII-free domain event are staged in
   the same transaction.

The command succeeds only when both invoices are paid and both the selected
payment availability and the customer's spendable credit are exactly zero.
Any intermediate failure rolls the entire correction back.

## Locking, replay, and drift

The owner locks the customer account first, then the three reviewed invoices in
UUID order, their active lines and source allocation, followed by the selected
payment and tax rate. It recomputes the complete preview under those locks.

One 16-to-120-character idempotency key reserves the resulting replacement
invoice. Child invoice-void keys are derived from it. A replay returns the same
closure, replacement, and allocations only when the stored preview fingerprint
and typed lineage still agree; conflicting or incomplete evidence fails closed.

## Operator boundary

The CLI is preview-only unless `--apply` is explicitly supplied with the exact
preview fingerprint, command UUID, actor, active staff UUID with
`billing:invoice:update`, idempotency key, reason, and every reviewed document,
payment, tax, and issuance identifier. The adapter owns only argument parsing,
permission resolution, serialization, and session lifecycle.

## Rollout

This is an additive command with no schema change. It must follow the normal
feature-branch, CI, immutable-image, staging-acceptance, and production
authorization sequence. Production execution is a separate explicitly
authorized action against a named host; merging this change does not execute a
correction.
