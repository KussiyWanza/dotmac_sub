# Vendor project review UI

Status: implemented contract

Owner: vendor operations

## Purpose

Give staff exact, permission-scoped review pages for vendor routes, as-built
evidence, quotes, and purchase invoices. The main project page links to the
exact record being summarized. Queue pages remain work lists; they do not hide
the evidence required for a decision.

## Ownership

- `operations.vendor_project_workspace` owns quote, proposed-route, and
  as-built read/action projections and proposed-route review eligibility.
- `operations.vendor_project_records` owns proposed-route and as-built records,
  review state, immutable review evidence, and review events.
- `operations.vendor_route_review_confirmation` owns the signed, stale-safe,
  exactly idempotent staff confirmation for proposed-route accept/reject.
- `operations.vendor_as_built_review_confirmation` owns the equivalent
  confirmation for as-built evidence.
- `operations.vendor_purchase_invoices` owns purchase-invoice totals,
  attachment metadata, review eligibility, and current payables/payment
  observation.
- `ui.project_vendor_delivery` selects the current project records and supplies
  exact drill-down URLs. Templates do not choose a different quote, route,
  as-built submission, or invoice.

## Page contracts

### Vendor review queue

- Audience: inventory and accounts-payable staff.
- Job: find records awaiting a decision.
- First view: project/vendor identity, record version/number, current state,
  amount or evidence size, submitted time, and one exact detail link.
- Permissions: inventory sections require `inventory:read`; purchase invoices
  require `finance:ap:read`.
- Empty state: each section states that no records are waiting.
- Mobile: identity, state, impact, and the detail action remain visible.

### Proposed-route review

- Audience: staff with fiber-route read access; decisions require
  `inventory:write`.
- Job: compare submitted route geometry with network context and accept it or
  return it for correction.
- Evidence: exact revision, vendor, submitted time, estimated length, map,
  current notes, and immutable review history.
- Actions: accept with an optional note or reject with a required reason.
  Both use a signed preview and confirmation. A changed route fails closed.
- Consequences: route status and review evidence only. Quote approval and
  installation-project state remain separate.

### As-built evidence review

- Audience: inventory staff.
- Job: inspect the exact as-built submission before accepting or rejecting it.
- Evidence: submission-only map, version, submitted time, length, installed
  line items, variation, work-order reference, and review history.
- Actions: accept with an optional note or reject with a required reason,
  through the existing signed confirmation owner.
- Consequences: as-built evidence state only. Project verification remains a
  separate decision.

### Quote review

- Audience: inventory or finance staff with read access; the existing review
  routes accept `inventory:write` or `finance:ap:write`.
- Job: inspect line items, totals, validity, linked route revisions, and the
  current review note.
- Actions: approve or request revision through the existing quote owner.
- The screen does not infer route acceptance from quote status.

### Purchase-invoice review

- Audience: accounts-payable staff.
- Job: inspect line items, totals, vendor/project identity, attachment,
  submission evidence, and current ERP/payment observation.
- Permissions: the existing operations boundary accepts `inventory:read` or
  `finance:ap:read` for reading, and `inventory:write` or `finance:ap:write`
  for decisions. Payables settlement and payment-observation details remain
  visible only with `finance:ap:read`.
- Actions: approve or request revision through
  `operations.vendor_purchase_invoices`.
- The screen distinguishes invoice review, ERP document state, and payment
  observation. Unknown or unavailable payment state never renders as unpaid.

## Exact main-project links

The project Vendor Delivery panel links to:

- `/admin/vendors/operations/quotes/{quote_id}`
- `/admin/vendors/routes/{installation_project_id}?revision_id={revision_id}`
- `/admin/vendors/operations/as-built/{as_built_id}`
- `/admin/vendors/operations/invoices/{invoice_id}`

When no record exists, the summary may link to the relevant queue or route
workspace. Permission-scoped facts and links remain absent when the viewer
lacks their owning read permission.
