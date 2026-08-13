# Network Map V2 governed asset changes

Status: proposed stacked implementation for `GET /admin/network/map-v2`.
Depends on PR #2336. The established `GET /admin/network/map` route, template,
frontend entry point, and behaviour remain unchanged.

## Decision and schema gap

`network.fiber_asset_changes` owns reviewed passive-fibre asset mutations. Its
legacy `FiberChangeRequest` transport is not a safe persistence contract for
the V2 workflow because it does not durably bind:

- a typed creation, edit, or movement intent;
- immutable before-and-after values and their confirmation digest;
- independent proposer and reviewer identities;
- submit and review idempotency fingerprints;
- the canonical asset version reviewed by the approver; or
- an applied result identity distinct from a create proposal's absent target.

Its legacy service also accepts free-form payloads. V2 must not widen that
boundary or make the browser a canonical writer. A dedicated
`NetworkMapAssetChangeProposal` evidence table is therefore introduced. The
new `network.map_asset_change_governance` coordinator owns only the proposal,
review policy, audit/event evidence, and the atomic coordination of approval.
It delegates the approved, explicitly typed passive-asset mutation to
`network.fiber_asset_changes`; the map and proposal table never become a second
network-inventory owner.

## Supported assets

The first governed slice supports canonical point assets already owned by the
passive-fibre workflow and projected on V2:

- FDH cabinets;
- fibre splice closures;
- fibre access points; and
- fibre support structures.

POP sites, OLTs, monitoring devices, service buildings, fibre segments, and
termination/connectivity records are intentionally excluded. Their canonical
writers or change semantics differ, so adding them here would create a
competing owner. V2 renders a clear unavailable state for unsupported proposal
types instead of accepting a generic asset bag.

## Command and review contract

- `network:fiber:write` may submit a creation, edit, or movement proposal.
- `network:fiber:review` may approve or reject a proposal. The permission is
  separately assignable and is not granted to the default operator role.
- The proposer cannot review their own proposal, even when they hold both
  permissions.
- Submission validates the closed asset-type vocabulary, permitted fields,
  finite WGS84 coordinates, required identity fields, and current canonical
  asset snapshot.
- Edits cannot alter coordinates; movements cannot alter non-coordinate
  fields. This prevents an edit from bypassing topology review.
- Approval locks the proposal and target, verifies the proposal digest, and
  rejects stale canonical input before delegating the exact mutation.
- Rejection records reviewer comments and never changes canonical state.
- Submit and review commands are idempotent by hashed keys plus material-input
  fingerprints. Key reuse with different inputs fails closed.
- Proposal and canonical-application audit rows retain before/after values,
  actor identity, command correlation, reason, and proposal digest.

## Fibre and movement safety

Connectivity is derived only from canonical termination-point and segment
relationships. Coordinate proximity is never queried or considered.

Approval of a movement is blocked when the asset is referenced by an active
fibre segment endpoint or, for a support structure, has an active mount. V2
does not move termination points, rewrite route geometry, draw replacement
routes, or infer a connection. A later topology-governance slice must define an
atomic asset-move plus affected-route review before such a proposal can be
approved.

Proposal previews are presentation overlays only. A dashed movement guide is
labelled as proposed movement and is never inserted into a fibre/topology
layer or persisted as route geometry.

## V2 page contract

- Screen: `admin.network.map_v2_asset_changes`; map workbench embedded only in
  `/admin/network/map-v2`.
- Audience: network operators proposing exact passive-plant changes and
  independently authorized reviewers approving or rejecting them.
- Read owners: `ui.network_map_projection` for map facts and
  `network.map_asset_change_governance` for bounded proposal/history state.
- Mutation owner: `network.map_asset_change_governance` coordinates review;
  `network.fiber_asset_changes` remains the canonical asset writer.
- First viewport: the existing V2 map, governance availability, pending count,
  and permission-appropriate proposal/review actions.
- States: loading, empty proposal queue, missing migrated canonical assets,
  unsupported asset type, validation failure, stale proposal, topology-review
  required, rejected, and applied remain distinct.
- Preview: before/after fields and proposed point geometry, explicitly marked
  non-canonical and non-topological.
- Evidence: proposal identity, proposer/reviewer, reviewer comments, timestamps,
  immutable digest, and audit timeline.

## Migration and rollback

The migration is expand-only: create the proposal table, indexes, constraints,
and `network:fiber:review` permission. It changes no production network row and
performs no backfill. Rolling back removes only unused proposal evidence and
the new permission; it cannot reverse already approved canonical asset changes,
which remain ordinary audited owner mutations. Operational rollback is to
disable or remove the V2 proposal actions while retaining evidence.
