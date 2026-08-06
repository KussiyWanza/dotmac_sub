# Invoice Discount History

## Decision

`financial.invoice_discounts` owns the current Invoice discount, its pricing,
and append-only revision history. A discount is one mutually exclusive
percentage or fixed amount applied to the original Invoice subtotal before tax.

The administrative draft authoring owner supplies the transaction for manual
Invoices. The Quote deposit owner supplies the transaction when it constructs a
deposit Invoice from a discounted native Quote. The discount participant only
flushes; the coordinator commits the Invoice, current discount, recalculated
totals, audit/outbox effects, and history together.

Every applied, changed, removed, or Quote-inherited revision stages the matching
`invoice.discount_*` event in that same transaction. The event identifies the
Invoice and financial change without customer contact details; the durable event
dispatcher owns delivery after commit.

## Authoritative facts

The `invoices` row stores the current discount type, entered value, actual
amount, optional reason, source, source Quote, applying staff user, application
time, and revision. `invoice_discount_history` is the immutable evidence for
every applied, changed, removed, or inherited revision.

The original subtotal remains the sum of the Invoice's active lines. The actual
discount is subtracted from that subtotal. Tax is then recalculated by
proportionally allocating the discount across the active line tax bases. The
stored Invoice total and balance due use the discounted subtotal plus the
recalculated tax.

## Controls

- A percentage must be greater than zero and at most 100.
- A fixed discount must be greater than zero and no greater than the subtotal.
- The reason is optional and limited to 500 characters.
- The applying person is the active logged-in `SystemUser`; the date is supplied
  by the server.
- Only a Draft Invoice can have a manual discount applied, changed, or removed.
  An issued Invoice uses the Credit Note workflow instead.
- A Quote-inherited discount records the source Quote and original Quote actor.
  It is locked against change or a second Invoice discount.
- History rows cannot be updated or deleted. PostgreSQL enforces this with an
  append-only trigger, and ORM guards provide the same protection in fast tests.

## Quote deposit inheritance

A native Quote deposit already charges a percentage of the discounted Quote
total. Construction now splits that same payable amount into its proportional
original subtotal, inherited discount, and tax. This changes presentation and
evidence only; it does not increase or reduce the customer's deposit amount.

CRM-only Quote deposits without canonical native Quote discount evidence remain
undiscounted Invoice records. The system does not infer financial facts from
metadata.

## History read model

The Invoice Discounts page reads the canonical history table joined to the
current Invoice, customer Party, and staff actor. It supports date, customer,
salesperson, discount type, Invoice status, and source filters. It links to both
the Invoice and the source Quote when present.

Freshness is transactional: a committed discount revision is immediately
visible. There is no cache or separately writable projection. A rebuild is the
same deterministic query over the Invoice and history rows; drift is detected by
revision gaps, current-state constraints, or a mismatch between the current
revision and latest history. Repair is owned by finance operations under the
`financial.invoice_discounts` boundary and must append evidence rather than edit
history.

## Existing Invoices

Migration 482 adds an empty current discount state to existing Invoices. It does
not invent historical discounts from totals, metadata, payments, or Credit
Notes. Any existing Invoice remains financially unchanged.
