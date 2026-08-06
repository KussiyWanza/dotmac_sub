# Materials / Vendor / ERP Owner Chain

**Status:** Implemented (owner-output chain slice, 2026-07-27)
**System of record:** Sub (operational need, approval, allocation, consumption evidence); Dotmac ERP (inventory and accounting outcomes)
**Decision owner:** Michael

## Contract

```text
work order requires material (field request)
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
  `/admin/operations/material-requests`. Creation requires an active technician
  assignment and records a submitted request against the native work-order ID;
  staff may then approve, reject, or cancel it. The work-order detail page and
  Operations navigation expose the same workspace. Approval emits the existing
  durable `field_material_request.approved` output for ERP delivery.
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
