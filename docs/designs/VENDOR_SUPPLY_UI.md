# Vendor supply UI

## Scope

This slice exposes the existing vendor material-release and mobilisation-advance
domains in the vendor project workspace and the staff vendor-operations queue.
It does not add inventory, warehouse, payment, settlement, or invoice-netting
authority to Sub.

## Ownership

| Concern | Owner |
|---|---|
| Material request eligibility, release decision, and provider issue observation | `operations.vendor_material_release` |
| Advance eligibility, quote ceiling, approval, and payables observation | `operations.vendor_advances` |
| Atomic vendor request command | `operations.vendor_project_workspace` |
| Typed vendor/staff read projection and exact review preview | `ui.vendor_supply_projection` |
| Signed, stale-safe staff review confirmation and replay guard | `operations.vendor_supply_review_confirmation` |
| Labels, tones, and icons | `ui.status_presentation` |
| Vendor and staff capabilities | `auth.permission_gate` |

Routes and templates are adapters. They authorize, validate transport values,
render typed projections, and map domain errors. They do not decide eligibility,
infer provider outcomes, or commit participant writes.

## Vendor workspace contract

The project detail page presents two independent panels:

- Materials: request eligibility, requested lines, Dotmac review state, review
  note, and the separately timestamped provider issue observation.
- Mobilisation advance: approved quote total, configured ceiling, already
  committed amount, remaining allowance, Dotmac review state, and the separately
  timestamped payables observation.

`vendor:material:request` is available to vendor owners and supervisors.
`vendor:advance:request` is owner-only. The UI renders the owner-supplied blocked
reason when project, assignment, quote, allowance, lifecycle, or capability
rules prevent a request. Commands repeat those rules inside the authoritative
transaction.

Absent provider state is explicit:

- `not_applicable` before Dotmac approval or after rejection/cancellation;
- `unknown` after approval when no provider outcome has been observed;
- `present` with an observation timestamp when a provider status exists.

Dotmac approval never renders as material issued or money paid.

## Staff review contract

The vendor operations queue separates inventory and accounts-payable authority:

- material rows require `inventory:read` to view and `inventory:write` to act;
- advance rows require `finance:ap:read` to view and `finance:ap:write` to act.

Approve and reject are important actions. Each action first issues a ten-minute
signed preview bound to the staff actor, record, action, reason, and exact state
fingerprint. Confirmation:

1. opens one owner-command transaction;
2. locks the material release or advance;
3. recomputes the preview and fails on drift;
4. reserves the signed proposal identifier for replay safety;
5. invokes the declaring participant owner once;
6. stores stable result evidence and commits atomically.

Rejection requires a reason. Approval may carry an optional note. A changed
status, release line, amount, quote allowance, committed total, vendor/project
binding, or review reason invalidates the preview.

## Freshness, drift, and repair

Material provider and payables data are observations, not decisions. Present
observations carry their provider-observed timestamp. The last good observation
is retained when refresh fails and must be labelled stale by the standard
`StateValue` contract when its freshness window is exceeded.

Drift signals are:

- an approved release without a later support observation;
- an approved advance without a later payables observation;
- a provider report that differs from the stored reference/status/timestamp.

Repair remains with the configured integration adapter, which reapplies the
current provider outcome through `apply_provider_outcome` or
`apply_payables_observation`. Rebuilding the UI projection is idempotent and
does not mutate authoritative state.

## Validation

Focused tests cover eligibility parity, typed projections, observation
semantics, role and staff permissions, signed preview/confirmation, stale
rejection, replay, rollback, templates, and architecture boundaries. The
repository-prescribed lint, typing, architecture, unit, integration, and
browser checks remain required before publication.
