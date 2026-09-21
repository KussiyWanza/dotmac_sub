# Material request ERP cutover

## Authority and lifecycle

- ERP is the only source of item, warehouse, stock, serial, and issuance facts.
- Sub owns the `field_request_eligible` decision on projected ERP items.
- Requests start from tickets, projects, project tasks, or work orders; Sub captures that context automatically.
- Work-order linkage is optional.
- ERP-channel requests are submitted immediately, with no separate Sub approval.
- Manual-channel requests can never be delivered to ERP or accept an ERP status.
- ERP issues or refuses; Sub projects the signed ERP observation.

## Deployment prerequisites

1. Apply Alembic revision `516_material_request_erp_submission`.
2. Install DotMac ERP connector 1.2.0 and enable inventory read, outbox delivery, status read, and material-status webhook capabilities.
3. Create a non-human ERP identity limited to inventory read, material-request submit, and material-request status read. These are service scopes, not human roles.
4. Store its credential and a separate webhook signing secret in the connector bindings.
5. Configure ERP to POST to `/api/v1/webhooks/erp-material/{capability_binding_id}` with a stable `X-Dotmac-Delivery` ID and `X-Dotmac-Signature: sha256=<HMAC-SHA256 exact body>`.
6. ERP must send the Sub UUID as `source_request_id`, its ERP-owned request identity, status, update time, and issued serials by line sequence.

The unprefixed `/webhooks/erp-material/...` path is not mounted and must fail
with 404. The bootstrap output is the canonical callback URL; do not construct
or shorten it manually. Payloads using the retired `omni_id` field fail closed
with 422 and must be corrected at ERP rather than aliased in Sub.

## Controlled activation

1. Keep Sub flow ownership off while installing the integration.
2. Run a complete catalogue refresh and compare item/warehouse counts with ERP.
3. Explicitly enable eligible items; new ERP items remain ineligible by default.
   The request form resolves enabled items through the bounded
   `/api/v1/search/material-items` typeahead rather than loading the full
   catalogue into a select control.
4. Submit and issue one ERP canary, then prove both webhook and scheduled reconciliation converge.
5. Prove a manual canary creates no ERP delivery and rejects an ERP callback.
6. Confirm the retired CRM sender is absent before assigning material-request flow ownership to Sub.
7. After deployment, allow bounded reconciliation cycles to observe every
   existing ERP-managed in-flight request. Verify the oldest
   `last_reconciled_at` advances; never repair request status directly in SQL.

## Safety and external work

Disabling Sub flow ownership stops new sends without deleting records. Manual requests must never be replayed into ERP, and missing callbacks must be reconciled from ERP rather than issued locally.

ERP must support the signed callback and neutral Sub payload. If its current hook only sends unsigned CRM-specific events, ERP requires a separate deployment before activation. Connector installation, secrets, service-user creation, initial import, eligibility decisions, and flow-ownership cutover are production configuration and cannot be captured in this Git branch.

## Requester-history migration evidence

Revision `587_field_request_requester_history` repairs only exact requester
identity links and creates the Person Party and SystemUser history indexes.
Before rollout, record the count of active material requests with a null
`requested_by_system_user_id`. After migration, investigate every remaining
row: it has no exact technician, legacy SystemUser, or unique Person Party
binding and must not be guessed. Verify a migrated request appears for its
requester through `GET /api/v1/field/material-requests`. This repair does not
send, approve, issue, or replay any material request.
