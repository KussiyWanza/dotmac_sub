# IP Assignment Lifecycle Source of Truth

Status: shadowing migration

Owner: `network.ip_assignment_lifecycle`

## Decision

`IPAssignment` is the desired-address authority at exact `Subscription` grain.
`Subscription.ipv4_address`, external RADIUS `Framed-IP-Address`, and active
sessions are projections or observations. They cannot create, move, reclaim, or
release an allocation.

| State                         | Owner / role                                             |
| ----------------------------- | -------------------------------------------------------- |
| Desired customer IPv4         | `network.ip_assignment_lifecycle` / active `IPAssignment` |
| `Subscription.ipv4_address`   | Rebuildable compatibility projection                      |
| RADIUS `Framed-IP-Address`    | `access.radius_projection`                                |
| NAS configuration             | Transport/runtime projection; no independent customer IP  |
| Live framed IP                | `sessions.radius_reconciliation` observation              |

NAS configuration carries **no independent customer IP**. A NAS-local
per-customer address record — notably a RouterOS `/ppp secret` with
`remote-address` — is a parallel authority and is prohibited. RouterOS consults
RADIUS only when the username is absent from `/ppp secret`
(<https://help.mikrotik.com/docs/spaces/ROS/pages/132350049/PPP+AAA>), so a
matching local secret does not merely override an attribute: it bypasses the
RADIUS projection entirely and shadows this document's authority. Removing such
a record is the correction; adding an update path that keeps it in sync is not,
because that preserves the parallel authority.

Where the ledger is ambiguous — more than one active assignment for an exact
service — consumers fail closed. Choosing a winner, whether by insertion order
or by any deterministic tiebreak, is an unauthorized ownership decision.

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

The administrative subscription form uses a dedicated **Replace service IPv4
only** action. That adapter confirms the selected address through the reviewed
assignment owner and then confirms the exact served projection through the
projection owner. It does not submit account, offer, billing mode, recurring
add-on, billing contract, adjustment, invoice, or cadence fields. Replacing an
address therefore preserves the customer's existing commercial entitlement
and paid service period.

An existing service's router and primary address move together through the
reviewed coordinator documented in
`docs/designs/SERVICE_ACCESS_MOVE_SOT.md`. That coordinator does not take over
IPv4 authority: it invokes the lifecycle owner's required flush-only assignment
and served-projection participants inside one root transaction. The generic
subscription edit and legacy bulk migration paths cannot write the NAS/IP move.

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

### Serving the ledger, not the projection

The address-side uniqueness index stops two services sharing one address. It
never stopped one service holding two, so "which address does this service own?"
had no answer, and `radius_population.populate()` answered it by unordered query
position — an ownership decision the projection does not hold, and one that
could differ between two runs over identical data.

One guardrail closes half of it, and the other half needs a modelling change
first.

`populate()` refuses an ambiguous ledger instead of choosing. The login's
existing RADIUS rows are PRESERVED and the refusal is counted as
`skipped_ambiguous_ipv4_ledger`, so ambiguity degrades to "no change, reported"
rather than "a coin-flip address, silently served". Adding deterministic
ordering would not have fixed this: a repeatable arbitrary choice is still an
unauthorized one.

**Multiple active IPv4 assignments per subscription is a supported product
shape** — the admin subscription form allocates one assignment per selected
block, and the edit form renders them as a list. A unique index on
`(subscription_id) WHERE is_active` would forbid that feature, not protect an
invariant.

What was missing was the concept of a PRIMARY assignment: `IPAssignment` records
that a service HOLDS an address, and nothing recorded which held address RADIUS
serves as `Framed-IP-Address`. `IPAssignment.is_primary` now records it, and
`uq_ip_assignments_primary_ipv4_active` permits a service to hold several
addresses while forbidding two active primaries. The marker is backfilled from
evidence — the served column first, then a sole active holding — and anything
undecidable is left unmarked rather than guessed, because consumers already fail
closed on that state.

`is_primary` has exactly ONE writer: `network.ip_assignment_lifecycle`, through
`mark_primary_ipv4_assignment`. It demotes any sibling and flushes before
promoting, because the partial unique index permits one active primary per
service and the reverse order violates it mid-statement.

Adapters may REQUEST the marker; they may not decide it. The reviewed repair
marks the address it was asked to make desired. The admin allocation form asks
for its first allocation, since the caller serves `allocated_ips[0]`. Generic IP
CRUD asks only where the assignment is the service's sole active IPv4 holding —
there is exactly one possible answer then, and skipping would leave a
first-provisioned customer with no served address. With several holdings and
none marked, it fails closed and leaves the served column alone.

The flag is readable but NOT writable through the generic API: it is absent from
`IPAssignmentCreate` and `IPAssignmentUpdate` and present on `IPAssignmentRead`,
so a thin adapter cannot change the served-address decision through a payload.
`tests/architecture/test_ipv4_primary_marker_ownership.py` pins all of this.

The contract is unchanged by this: an additional address that must coexist with
the primary belongs to `SubscriberAdditionalRoute` and is projected as
`Framed-Route`. The marker does not make a second `IPAssignment` a legitimate
way to serve two addresses; it makes the served one identifiable.

#### Cutover: RADIUS must consume the assignment, not the served column

NOT YET DONE, and deliberately so. `populate()` still prefers
`subscriptions.ipv4_address` and falls back to the assignment. That ordering is
the defect that produces duplicate `Framed-IP-Address` values, because the
column carries no uniqueness and is projected verbatim.

Flipping the order today would be an outage, not a fix. Measured read-only on
production 2026-08-01 across the full projected population (9,829 subscriptions
with a login, the set `populate()` actually considers):

- **410 distinct logins are currently served a `Framed-IP-Address` with no
  active exact-service assignment behind it.** Under an assignment-first rule
  they emit no address at all — de-IPed, then torn down by the BNG. 283 of them
  are online right now.
- 30 subscriptions have a column that disagrees with their assignment and would
  be silently re-addressed on the next sweep.

Population matters here and is easy to get wrong: `audit_ip_consistency` scopes
to 1,204 active pinned-IP subscriptions and reports 62 missing assignments.
That is a real number for a narrower question, and it understates the flip's
blast radius by more than sixfold. `populate()` projects every ACTIVE or
BLOCKED subscription carrying a login, so the flip must be sized against that
set. A further 5,827 rows lack an assignment but carry no radreply row at all
(reject or no projection) and would be unaffected.

The ordered sequence is therefore:

1. **Ledger repair first.** Backfill an exact-service assignment for every
   subscription whose column has none, and adjudicate every column-versus-
   assignment disagreement, through the reviewed repair command. Repair is the
   prerequisite for the cutover, not a consequence of it.
2. **Shadow.** `scripts/one_off/audit_nas_session_ip_divergence.py` reports the
   would-change and would-break sets: `served_projection_stale` is what the flip
   would re-address, `served_projection_unowned` is what it would break.

   Those two are NOT a sufficient gate. The audit's per-subscription classes are
   mutually exclusive — a subscription is reported under the first class it
   matches — so an ambiguous ledger, a legacy subscriber-level assignment, or a
   broken uniqueness guarantee SUPPRESSES the stale/unowned finding for that
   same subscription. Gating on the two visible classes would therefore read
   zero precisely where the ledger is worst. The gate requires ALL of:

   - `served_projection_stale` = 0
   - `served_projection_unowned` = 0
   - `ambiguous_service_assignment` = 0
   - `legacy_unbound_assignment` = 0
   - `ledger_integrity_violation` = 0
   - zero unresolved duplicate-login bindings

   plus parity between the served projection, external RADIUS `Framed-IP-Address`
   and the fresh live-session address for every projected login — a column that
   agrees with its assignment while RADIUS still carries the old value is not
   converged, merely half-written.

   Hold every one of those conditions across **two complete population and audit
   cycles** before enabling the gate. One clean cycle proves a moment; two prove
   that a sweep in between did not reintroduce drift.

   Verified on production, not in a test.
3. **Flip behind a database-owned gate**, in the same shape as the
   `Simultaneous-Use` cutover, so deployment alone cannot change fleet-wide
   addressing.
4. **Retire the fallback** and reduce the column to a rebuildable projection.

Until step 1 completes, the column-first ordering stays. It is wrong, and it is
less wrong than removing 62 customers' addresses.

### Retiring a NAS-local secret

Owner: `network.nas_local_secret_boundary`
(`app/services/nas/local_secret_policy.py`).

Creating, suspending, unsuspending, or re-addressing a MikroTik PPPoE local
secret is prohibited in code on both execution surfaces — the activation command
builder and the operator-editable provisioning template runner — so a database
template cannot reintroduce it without a code change. A PPPoE activation
therefore sends no NAS command and records a typed
`NAS action not applicable — RADIUS owned` ruling, while queue mapping for
bandwidth monitoring still runs.

`change_speed` is prohibited on the same grounds. Migrating how speed
enforcement works is a separate slice, but continuing to mutate the shadow
record while the boundary exists would contradict it.

The prohibition is enforced twice, because an action label is not a boundary.
The requested action is ruled on, and the RENDERED command text is inspected
before it reaches a device — a template filed under `reset_session` or
`backup_config` can still contain local-secret text, and template bodies are
operator-editable data. The same text guard runs when a template is saved, so a
bad row is rejected at authoring time rather than only when an operator runs it.

Removing a pre-existing secret is corrective rather than a second authority, and
is the one permitted local-secret operation. It carries one of two typed
intents, which assert opposite things about RADIUS:

- **`migrate_to_radius`** — the service continues. RADIUS must verifiably serve
  exactly one unambiguous login, because removal hands authentication to it.
- **`terminal_retirement`** — the service is canceled. RADIUS absence is
  expected and correct, and no nonterminal subscription may still depend on the
  login.

Both refuse a login carried by more than one nonterminal subscription, verify
the device afterwards by existence COUNT, and fail rather than reporting success
when the secret is still present. The readback is never a detail print: that
echoes the stored PPP password. An already-absent secret is a verified no-op.
Each refusal has its own stable code, so an operator can tell a shared login
from an unconverged projection without parsing prose.

Every attempt that reaches a device opens a `NetworkOperation`
(`nas_local_secret_retire`): intent and provenance as input, verified counts as
output, and a durable failure row when the device errors or the removal cannot
be proven. One active operation per `(NAS, login)` makes a duplicate delivery or
a concurrent operator run a rejection rather than a repeat.

Authorisation is provenance, not a synthesised reviewer. Operator-driven
retirement records the actor and reason; event-driven retirement records the
originating event. Terminal retirement is staged from the durable
`subscription.canceled` handler AFTER the terminal RADIUS projection succeeds,
and is best-effort by construction: cancellation is authoritative and already
decided, so an unreachable NAS must leave a retryable, visible operation failure
rather than roll the lifecycle transition back.

The operator entry point is a CLI, not an HTTP route: dry-run by default, an
exact named NAS, a bounded explicit cohort with no `--all`, and an apply that
must echo the plan fingerprint from its own preview. Execution still requires
the application/DB host and the NAS to be named separately and explicitly.

## Migration

Old writers remain migration debt during this shadowing slice:

- generic `IPAssignments` CRUD used by subscription creation and remaining
  network administration tools;
- provisioning allocation/reactivation helpers;
- admin IP assignment and bulk import;
- ONT WAN claims;
- terminal lifecycle release; and
- subscriber deletion cleanup.

They continue operating until their callers can enter a clean owner-command
boundary. They must not be treated as precedent or expanded. The later runtime
cutover migrates these callers, adds field-level architecture guards, and
removes subscriber-grain inference and helper transaction completion.

The generic subscription-edit IPv4 writer is cut over. Generic Save rejects a
changed address and directs the operator to the dedicated replacement action;
it cannot invoke legacy IP CRUD. The replacement action is intentionally
separate from additional routed-block add-on purchase or quantity changes.

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

The admin subscription detail page exposes the same owner preview as a
server-owned **Reconcile served IPv4** action. It is absent when the projection
is already aligned, enabled only for `ready`, and otherwise displays the typed
blocker. The form carries the owner-produced assignment identifier and
fingerprint, uses a per-render idempotency key, requires explicit confirmation,
and delegates directly to `repair_service_ipv4_projection`; its adapter does
not commit, roll back, or write ORM state. The impact preview names the current
served/RADIUS evidence, desired IPAM address, old-address session count, and the
fact that billing and entitlement remain unchanged.

Selecting an address that IPAM already assigns to the service is no longer a
successful replacement no-op when the served projection is stale. The replace
adapter directs the operator to the reviewed reconciliation action instead of
claiming success without repairing RADIUS and sessions.

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
- `tests/test_web_ipv4_projection_reconciliation.py` proves the server-owned UI
  preview, explicit confirmation contract, owner delegation, composed template,
  and same-address false-success regression.
