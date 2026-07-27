# IP Assignment Lifecycle Source of Truth

Status: shadowing migration

Owner: `network.ip_assignment_lifecycle`

## Decision

`IPAssignment` is the desired-address authority at exact `Subscription` grain.
`Subscription.ipv4_address`, external RADIUS `Framed-IP-Address`, and active
sessions are projections or observations. They cannot create, move, reclaim, or
release an allocation.

The lifecycle owner owns reviewed IPv4 ledger repair:

- keep or create one desired assignment for an exact service;
- link a legacy subscriber assignment to an explicitly selected service;
- deactivate only an explicitly reviewed set of stale exact-service
  assignments; and
- release exact assignments only for a terminal service.

The ledger command never changes the subscription served-IP projection,
external RADIUS, or a live session.

The reviewed projection command is separate. It may update
`Subscription.ipv4_address` only when one active assignment is linked to that
exact service and the served-IP, policy-selected RADIUS projection, and active
session observations agree with the fingerprinted preview. Its transactional
event delegates external RADIUS repair and old-IP-only session
reauthentication to their canonical owners after commit.

## Command contract

`preview_service_ipv4_assignment_repair` is read-only. Its SHA-256 binds:

- exact subscription and subscriber identity;
- desired IPv4 address identity, or an explicit terminal release;
- the complete active subscriber assignment set plus any active owner of the
  desired address;
- the exact assignments selected for deactivation; and
- the typed decision.

`repair_service_ipv4_assignment` accepts the same typed inputs, the preview
fingerprint, actor, reason, and idempotency key. It enters
`execute_owner_command` once on a transaction-free session, locks the exact
subscription, subscriber, desired address, address pool, and relevant
assignments. On PostgreSQL it also takes a short `SHARE` table lock on active
routed-block and device-address inventories so a concurrent route or device
insert cannot invalidate the single-address eligibility decision. It then
recomputes the preview. Changed evidence fails closed.

The command may create a new active history row or attach an existing active
legacy row. It never repoints an assignment row to a different address or
customer. Deactivation preserves the historical row.

## Safety policy

The reviewed desired address must:

- exist in an active IPv4 pool;
- not be reserved or bound to an ONT;
- not be marked as a management allocation;
- not belong to an OLT management pool;
- not be a Router, OLT, NAS, monitoring-device, or RADIUS-client address; and
- not fall inside an active `SubscriberAdditionalRoute`.

An active desired-address assignment must already belong to the exact
subscription or be an unlinked legacy assignment for the same subscriber.
Cross-customer and cross-service ownership fails closed.

Only assignments already linked to the exact subscription may be deactivated.
The command refuses an incomplete deactivation set that would leave a second
active exact-service assignment.

Every nonterminal service status may retain or repair an assignment. Only
canceled or expired services may use the release action.

## Migration

Old writers remain migration debt during this shadowing slice:

- generic `IPAssignments` CRUD;
- provisioning allocation/reactivation helpers;
- admin IP assignment and bulk import;
- ONT WAN claims;
- terminal lifecycle release; and
- subscriber deletion cleanup.

They continue operating until their callers can enter a clean owner-command
boundary. They must not be treated as precedent or expanded. The later runtime
cutover migrates these callers, adds field-level architecture guards, and
removes subscriber-grain inference and helper transaction completion.

## Projection cutover

After the ledger is repaired and remaining ambiguity is quarantined, the
reviewed projection slice:

1. preview exact IPAM versus `Subscription.ipv4_address`, external RADIUS, and
   authoritative session observations;
2. changes the served-IP projection through
   `network.ip_assignment_lifecycle` in one fingerprint-bound owner command;
3. emits `ip_assignment.served_projection_repaired` transactionally;
4. lets the durable handler request `access.radius_projection` for the exact
   identity and reauthenticate only sessions still framed with the old IP; and
5. verify reconnect, accounting freshness, and traffic.

The projection command fails closed on a missing or multiple exact assignment,
cross-subscriber ownership, non-active service, shared-login selection,
unavailable or already-divergent RADIUS evidence, conflicting session
observations, and stale fingerprints. Event retries are safe because session
enforcement targets only the old framed IP; a session that has reauthenticated
onto the desired IP is not disconnected again.

The final runtime cutover still removes `trust_ipam`, makes exact-service IPAM
the unconditional RADIUS address input, retires legacy subscriber fallback,
and enables permanent idempotent drift repair.

## Operator flow

The adapter is invoked as
`python -m scripts.one_off.repair_service_ipv4_assignment` and is dry-run by
default. Apply requires the exact preview fingerprint, idempotency key, actor,
and reason. A fresh production backup, named target, reviewed cohort, and
post-state verification are operational gates; historical previews are not
authorization.

Served projection repair uses
`python -m scripts.one_off.repair_service_ipv4_projection` with the exact
subscription and assignment identifiers. It is also dry-run by default and
requires the exact preview fingerprint, idempotency key, actor, and reason.

## Verification

- `tests/test_ip_assignment_lifecycle.py` proves create, link, deactivate,
  release, served projection, stale-preview, idempotency, and fail-closed
  safety.
- `tests/test_ip_assignment_projection_handler.py` proves RADIUS is projected
  before old-IP-only session enforcement.
- `tests/test_ip_consistency_audit.py` proves exact-service assignment lookup
  and policy-aware RADIUS reply expectations.
- `tests/test_ip_assignment_repair.py` preserves the ownership-only migration
  behavior under the same owner.
- `tests/architecture/test_ip_assignment_service_ownership.py` verifies the
  typed manifest, dry-run adapters, transaction ownership, and separation from
  served-IP/RADIUS/session projections.
