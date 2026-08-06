# Quote Discount and History

Status: approved by the requester on 2026-08-05.

## Decision

New Quotes use one optional Quote-level discount after the Line Item subtotal.
The discount is either a percentage or a fixed currency amount, never both.
Configured tax is calculated from the discounted subtotal, and Total is the
discounted subtotal plus tax.

The authenticated SystemUser and server time supply the applied-by and
applied-at evidence. The browser cannot submit either value. A discount reason
is optional.

`sales.quote_authoring` owns initial application, later replacement, removal,
the current Quote discount, recalculation, and append-only discount history.
Accepted Quotes remain immutable. Every public mutation locks the Quote,
requires the expected discount revision, and writes the Quote, history, audit,
and domain event in one owner transaction.

Previous Quotes retain their historical `QuoteLineItem.discount_percent`
values. That legacy field remains read-only for old detail and PDF evidence;
all new Line Items are gross-priced and every new writer sets the legacy field
to zero. A later contract migration may drop the legacy column only after its
retention requirement expires and accepted artifacts no longer depend on it.

## Calculation

```text
original subtotal = sum(new Line Item quantity * Unit Price)
discount amount = percentage of original subtotal OR fixed amount
discounted subtotal = original subtotal - discount amount
configured tax = discounted subtotal * Tax Rate
total = discounted subtotal + tax
```

Percentage values must be greater than zero and at most 100. Fixed amounts
must be greater than zero and cannot exceed the original subtotal. Any
discount amount greater than the subtotal fails closed.

## Discount history

`QuoteDiscountHistory` is append-only evidence. Each application, replacement,
or removal records the Quote revision, action, type, entered value, actual
discount amount, original subtotal, discounted subtotal, tax, final total,
optional reason, authenticated actor, command identity/fingerprint, and server
time. Removed entries preserve the last effective discount values.

The Quote row contains the current discount and a monotonic revision. The
history is the durable explanation for how that current state was reached.
Equivalent command replay returns the original outcome; changed evidence under
the same command identity fails closed.

## Page contract

- Screen: `sales-quote-discounts-list` at
  `/admin/sales/quote-discounts`; page type: operational history list.
- Audience/job: staff with `crm:quote:read` review every Quote discount and its
  changes or removal.
- Decision supported: identify who granted or changed a discount, its effect,
  and the related Quote state.
- Read owner: `sales.quote_discount_reporting`; command and eligibility owner:
  `sales.quote_authoring`; RBAC remains authoritative for access.
- Columns: Quote/customer, original subtotal, type/value and actual discount,
  discounted subtotal/final total, optional reason, actor/server time, action,
  Quote status, and a direct Quote link.
- Filters: inclusive applied date range, customer search, salesperson,
  discount type, and Quote status. Default sort is newest evidence first, with
  deterministic id tie-breaking and server-side pagination.
- States: an empty filtered result is distinct from a read failure;
  unauthorized users are rejected by the route dependency. Currency is always
  displayed with money values.
- Responsive projection: rows become stacked cards on narrow screens while
  retaining Quote, customer, value impact, actor/time, state, and the Quote
  link.
- Audit/observability: mutations stage canonical audit and domain-event
  evidence; read failures log structured diagnostics without customer search
  text.

## Migration and rollback

Migration 480 is additive. It adds current Quote discount columns, SalesOrder
snapshot columns, and the append-only history table. It performs no historical
inference and does not rewrite previous Quote or Line Item money. New writers
cut over immediately to gross Line Item pricing and Quote-level discounts.

The predecessor-to-head rehearsal must verify additive deployment from
revision 479. Rollback before new discount writes may drop the added objects.
After native discount history exists, rollback is forward-fix only because
dropping append-only commercial evidence is destructive.
