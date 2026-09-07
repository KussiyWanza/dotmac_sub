# Work-Order Expense Entry

## Decision

Authenticated staff who can open an exact admin work-order detail page may
create an expense claim from that page. Technician assignment is not the access
rule for this web surface. Global dispatch-read access, or a matching reseller
or region scoped grant, permits viewing the page; creating a claim separately
requires the equivalent scoped `operations:dispatch:write` grant. Field/mobile
expense entry retains its assigned-technician scope.

Every claim created here is bound to the work order in the route. The browser
does not submit a work-order identifier and cannot create a work-order-less
claim. The authenticated session supplies the system-user and person identity;
requester identifiers and email are not form inputs.

## Owners and boundaries

- `ui.work_order_expense_projection` owns the typed form, field errors,
  requester-owned claim list, category help, action eligibility, and honest ERP
  delivery labels.
- `operations.expense_categories` owns the live ERP category observation,
  including receipt requirements and per-claim maximums. Unavailable and empty
  are distinct and both disable submission.
- `operations.expense_requests` owns validation and the atomic submitted claim,
  line items, requester evidence, idempotency fingerprint, receipt metadata,
  work-order activity mark, and durable ERP delivery staging.
- The admin route parses HTTP form and file values, enforces CSRF and exact
  work-order write-tier RBAC scope, releases its read transaction, and invokes
  the typed owner command. It does not commit or call ERP.
- ERP remains authoritative for approval routing, rejection, reimbursement
  account details, and payment. The form deliberately has no approver, bank,
  cost-centre, ERP task, fleet vehicle, or receipt-number controls.

Receipt bytes use the existing private attachment storage owner. Metadata is
staged flush-only inside the expense command transaction. A deterministic
per-line receipt client reference makes a repeated claim submission safe. The
existing general field receipt endpoint remains assigned-technician scoped.

## State and delivery semantics

The work-order card shows only claims created by the authenticated staff
identity. Local submission, durable delivery pending, delivered but awaiting
ERP acceptance, ERP accepted, rejected, failed/dead, approved, and paid facts
remain distinct. `sent` outbox evidence is not presented as ERP acceptance;
only an accepted event or ERP claim reference qualifies.

## Schema change

`requested_by_technician_id` on expense requests and
`uploaded_by_technician_id` on field attachments become nullable. System-user
and person identity remain mandatory for new web submissions. The downgrade
fails closed while any staff-created rows without technician links exist.

## Validation and recovery

The server requires purpose, claim date, a three-letter currency, and one to 50
positive-amount lines. Each line requires an active ERP category and a
description of at most 500 characters. Category receipt and maximum rules are
enforced again by the command owner. Browser calculations and required markers
are assistance only.

Text values and the stable claim client reference survive validation errors.
Browsers cannot repopulate file inputs, so a selected receipt is cleared and an
explicit field error asks the user to reselect it. ERP category or sync
unavailability never fabricates a usable fallback.

## Non-goals

This slice does not add a standalone expense page, edit/approval/payment/retry
controls, a second persistence model, synchronous ERP requests, integration
cutover changes, or Flutter changes.
