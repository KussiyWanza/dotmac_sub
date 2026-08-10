# Materials / Vendor / ERP Owner Chain

**Status:** ERP submission redesign in implementation (2026-08-10)
**System of record:** Sub (contextual need, field eligibility, consumption evidence); Dotmac ERP (items, warehouses, stock, serials, issuance, and refusal)
**Decision owner:** Michael

ERP-channel requests leave Sub immediately with no separate Sub approval. Requests may originate from a ticket, project, project task, or work order, and work-order linkage is optional. Manual-channel requests are permanently excluded from ERP delivery. ERP outcomes return through a signed idempotent webhook, with scheduled status reconciliation as fallback. The production activation sequence is in `docs/runbooks/MATERIAL_REQUEST_ERP_CUTOVER.md`.

## Contract

```text
ticket / project / project task requires material
  -> assigned native work order selected   [context projection; one owner]
  -> work order requires material          [field request]
  -> material request approved            [operations.material_dependencies]
  -> ERP issue requested                  [receipted consumer -> durable outbox]
  -> ERP issue observed                   [polling write-back, fail-atomic]
  -> material allocated                   [allocation rows + fulfilled output]
  -> field consumption verified           [operations.material_consumption]
vendor invoice approved                   [operations.vendor_purchase_invoice_records]
  -> ERP payables export                  [receipted consumer -> durable outbox]
  -> ERP payables observation             [polling projection + observed output]
```

Typed outputs stage atomically with each owning transition:
`field_material_request.approved` / `.fulfilled`,
`field_material.consumption_recorded`, `vendor_purchase_invoice.approved` /
`.payment_observed`. The `MaterialsLifecycleProjectionHandler` delivers the
approval outputs to receipted consumers (`events.owner_outputs`), so each
ERP-transport enqueue commits atomically with its unique
`(consumer, event_id)` receipt — replacing the previous swallowed
best-effort enqueue whose only trace was a metadata breadcrumb.

## Boundaries

- Staff manage service-work-order material dependencies in Sub at
  `/admin/operations/material-requests`. Ticket, project, project-task, and
  work-order detail pages expose the same request through the work order's
  native relationships and open the form scoped to eligible field work.
  Creation requires an active technician assignment and records one submitted
  request against the native work-order ID; it does not create parallel
  ticket/project/task request records. Staff may then approve, reject, or
  cancel it. Approval emits the existing durable
  `field_material_request.approved` output for ERP delivery.
- The staff workspace accepts the configured ERP warehouse code but does not
  query ERP tables, reserve stock, choose serials, or expose local issue and
  fulfil actions. ERP remains the owner of availability, serial allocation,
  issuance, and refusal; Sub displays only the observed support reference and
  outcome projected through the existing adapter.
- ERP failures remain durable pending deliveries in the `field_erp_sync`
  outbox (8-attempt dead-letter). Sub never infers issuance or payment;
  ERP outcomes return only through the fail-atomic write-back and polling
  observations (`refresh_material_request_statuses`,
  `refresh_purchase_invoice_statuses`) — legitimate observation of
  ERP-owned reality, not drift repair.
- The material-request detail page shows the current single-writer owner,
  durable outbox state, attempt count, last error, ERP reference, and observed
  outcome. Pending deliveries retry automatically with their stable
  idempotency key. Until `sync_flow_ownership.material_request` is explicitly
  cut over from `crm` to `sub`, the page states that Sub will not deliver.
- Delivery and status schedules are enabled by validated ERP capability
  bindings. The retired `dotmac_erp_sync_enabled` setting is not a runtime gate.
- `operations.material_consumption` (app/services/field/materials.py) is
  now a registered owner (left the shrink-only writer baseline): monotonic,
  allocation-capped consumption evidence with a typed output.
- The vendor material-release and advance lifecycles keep their existing
  typed events; their ERP transport does not exist yet
  (`apply_provider_outcome` / `apply_payables_observation` have no
  production caller) — building it is a new integration, deferred, not a
  chaining conversion.
- Materials timers deferred: the ERP boundary is covered by durable
  pending deliveries; an "approved but never issued" escalation timer is a
  candidate once an escalation consumer is defined.
