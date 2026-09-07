# Replaceable Backoffice Integration Boundary

Dotmac Sub is the source of truth for subscribers, services, provisioning,
billing, operational service workflows, and their official timelines. It must
remain operationally complete without Dotmac ERP.

Dotmac ERP is the current backoffice provider for selected procurement,
inventory-support, expense, workforce, and payables collaborations. It is not an
enterprise control plane. It may be replaced by Zoho or another product without
moving Sub domain authority or rewriting Sub's core services.

## Boundary rules

1. Sub domain owners call `app.services.backoffice`, a local anti-corruption
   port. They never import `app.services.dotmac_erp`.
2. Provider adapters translate versioned API/event contracts and own only
   transport, retry, idempotency, and provider-observation concerns.
3. There are no cross-system database queries, database foreign keys, shared
   ORM models, shared transactions, or required cross-system runtime services.
   Sub's integration runtime is local to Sub and selects providers through
   versioned capability bindings.
4. Sub stores provider-neutral references plus an explicit source-system name.
   A reference is correlation evidence, not delegated decision authority.
5. A valid Sub state transition commits independently. Provider delivery failure
   is recorded for retry and reconciliation; it does not reverse the Sub
   decision.
6. Inbound provider observations are validated for provenance and reconciled
   idempotently before a Sub owner projects them into Sub state.
7. Each product owns its own identity systems, including its own tax-identity
   records and validation policy. There is no enterprise tax-ID registry.
8. Secrets remain provider-local. Contracts carry opaque references, never
   credentials.

For field expenses, Sub's manager approval is the authoritative release point.
Submission records the technician's request but does not create an ERP delivery
intent. After expense-flow cutover, approval and the idempotent expense-claim
outbox row are committed together; a staging failure rolls both back for safe
retry. ERP transport happens asynchronously and cannot reverse the local
approval. The retained `exp-{request_id}-submit-v1` key prevents duplicates from
older submission-triggered deployments. Existing approved expenses are not
automatically backfilled; any historical repair requires a separately reviewed,
explicitly scoped operation.

## Current provider mapping

| Sub collaboration | Provider-neutral Sub fields | Current adapter |
| --- | --- | --- |
| Material support | `support_system`, `support_reference`, `support_status` | `app.services.dotmac_erp.material_sync` |
| Expense claim | `expense_system`, `expense_claim_reference`, `expense_claim_status` | `app.services.dotmac_erp.expense_sync` |
| Procurement order | `procurement_system`, `procurement_order_reference` | `app.services.dotmac_erp.purchase_order_sync` |
| Supplier/payables document | `payables_system`, `payables_document_reference`, `payment_status` | `app.services.dotmac_erp.purchase_invoice_sync` |
| Workforce observation | `workforce_system`, `workforce_employee_reference` | configured workforce adapter |

Replacing Dotmac ERP means implementing the same versioned capabilities in
another connector, selecting it with a Sub-local capability binding, migrating
correlation references with an explicit cutover plan, and retiring the old
connector after shadow verification. It does not mean creating a new
enterprise-wide integration service.

## Current migration and cutover

- **Old coupling:** core Sub rows and domain services exposed Dotmac-ERP-named
  fields and imported the provider adapter directly.
- **New boundary:** Sub domain owners call the local port; provider-neutral
  references are scoped by source system, while `app.services.dotmac_erp` owns
  only the current wire mapping, outbox delivery, and observations.
- **Data migration:** revision `383_replaceable_backoffice` renames legacy
  fields, backfills `dotmac_erp` provenance, and scopes external-reference
  uniqueness by `(source_system, reference)`. It also converts unused ERPNext
  IDs embedded in project, task, ticket, and organization models into explicit
  provider-neutral correlation fields, backfilled with `erpnext` provenance.
- **Verification phase:** the Dotmac ERP adapter continues using its existing
  versioned wire keys while architecture tests prohibit provider imports and
  provider-named fields in Sub domain owners.
- **Cutover gate:** enable a flow only after its adapter behavior tests pass,
  pending/dead-letter reconciliation is clean, and its source-flow ownership is
  explicitly assigned to Sub.
- **Fallback retirement:** provider-specific task settings and connector tables
  remain inside the Dotmac ERP adapter until that adapter is retired. They are
  not compatibility fields in the Sub domain model.
