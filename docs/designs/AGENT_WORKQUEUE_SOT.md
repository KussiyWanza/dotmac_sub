# Native Agent Workqueue Source of Truth

Status: cutover-ready implementation; production cutover and CRM deletion
remain gated.

Owner: `operations.agent_workqueue`

Implementation: `app.services.workqueue.commands`

CRM source being retired: `app/web/agent/workqueue.py` and its three templates.

## Goal and boundary

Sub must provide the operational workqueue itself so CRM can be retired. The
workqueue is one ranked view across native Inbox conversations, support
tickets, and work orders. It is not another owner for those records.

`operations.agent_workqueue` owns:

- staff audience and item visibility after consuming the native service-team
  scope;
- deterministic provider aggregation, scoring, ranking, sectioning, and
  freshness presentation;
- each authenticated operator's `WorkqueueSnooze` state; and
- atomic, scope-checked dispatch of claim and complete commands to the source
  lifecycle owner.

The source modules retain authority:

- `support.ticket_lifecycle` decides ticket assignment and resolution;
- Team Inbox owners decide conversation assignment and resolution;
- native work-order owners decide dispatch and field transitions; and
- `operations.service_team_lifecycle` decides staff-to-team scope.

The work-order provider therefore exposes open and personal snooze only.
Adding work-order claim or completion to this screen requires a typed
participant contract from the native dispatch or field owner. A CRM
compatibility identifier is never action authority.

No ERP, HR product, or external workforce system is embedded in this boundary.
Provider-neutral HR synchronization may supply observations to the service-team
owner under its separate contract. Workqueue scope consumes only reconciled
native service-team facts.

## Operator page contract

Canonical page: `GET /admin/workqueue`

Primary user: authenticated support or operations staff with
`support:ticket:read`.

First-viewport decisions:

1. What requires attention now?
2. Why is it ranked there?
3. What is its current source state and deadline?
4. Is it assigned, claimable, completable, or personally snoozed?
5. Which source workspace should the operator open?

The first viewport contains:

- page identity and the explicit statement that source modules retain
  lifecycle authority;
- one primary page action, **Open Inbox**;
- audience, native service-team, and snoozed-item filters;
- a generated-at freshness label; and
- the cross-source `Right now` band.

Each row exposes identity, source kind, source state, urgency, ranking reason,
score, activity time, due time, and the canonical source link. `Open` is the
common visible row action. Claim, complete, snooze, and restore live in the
row's overflow control. Templates consume owner-produced labels and hints; they
do not infer status, urgency, or action eligibility.

Claim and complete use the shared server-owned `ActionForm` contract. Each
rendered form carries an owner-generated fingerprint of the current native
item state and available action. Completion also presents impact and requires
an explicit labeled confirmation because it resolves a customer-visible source
record. The command owner rechecks both under the target lock.

The page polls the owner projection every 30 seconds and also listens to the
existing workqueue SSE invalidation transport. Realtime delivery is
best-effort; the poll is the deterministic repair path. A transport failure
never changes business state.

## Native route replacement

| CRM behavior | Native replacement |
|---|---|
| `GET /agent/workqueue` | `GET /admin/workqueue` |
| `GET /agent/workqueue/_right_now` | `GET /admin/workqueue/_right-now` |
| `GET /agent/workqueue/_section/{kind}` | `GET /admin/workqueue/_section/{kind}` |
| `POST /agent/workqueue/snooze` | `POST /admin/workqueue/snooze` |
| `POST /agent/workqueue/snooze/clear` | `POST /admin/workqueue/snooze/clear` |
| `POST /agent/workqueue/claim` | `POST /admin/workqueue/claim` |
| `POST /agent/workqueue/complete` | `POST /admin/workqueue/complete` |

The native routes are thin adapters. The JSON API uses the same owner command
for snooze and restore. It does not retain a second committed snooze writer.

## Scope and identity contract

Authentication exposes `SystemUser.id` as the principal identifier. Native
service-team membership is Party-backed. Callers must never compare those UUID
domains directly.

`operations.service_team_lifecycle.resolve_staff_team_scope` owns the active
member, lead, and manager projection.
`list_active_team_member_system_user_ids` owns the reverse projection required
for team audience. Inactive teams, inactive memberships, inactive staff, and
inactive or non-Person Party identities are excluded.

Audience rules are:

- `self`: the operator's assigned work and unassigned work in accessible teams;
- `team`: work assigned across the operator's active member/managed teams; and
- `org`: unrestricted only for administrators or an explicit org-audience
  scope.

A requested audience is clamped to the principal's natural authority. An
explicit service-team filter outside that scope fails closed.

## Ranking projection

Providers emit typed `WorkqueueItem` records. The aggregator does not contain
source-specific queries.

Authoritative inputs are:

- support-ticket lifecycle and ticket SLA clocks;
- Team Inbox conversation, assignment, and latest-message projection;
- native work-order and dispatch assignment projection;
- native service-team scope;
- personal workqueue snooze state; and
- typed scoring configuration.

The projection sorts by score descending, last activity descending, configured
kind rank, then native item UUID. This tie-break is stable. Personal snoozes
filter only that operator's projection.

Unknown or retired provider kinds in old snooze rows do not become queue items.
The owner can rebuild the complete projection from authoritative inputs on
every request.

## Command and transaction contract

All state-changing forms carry CSRF and a stable request UUID. The UUID is the
command, correlation, and idempotency evidence for that rendered action.

`execute_action`:

1. enters `execute_owner_command` once on a transaction-free session;
2. locks an exact `(scope, idempotency key)` record;
3. locks the native target record;
4. rebuilds current native scope and action eligibility;
5. for claim and complete, recomputes the owner-produced action fingerprint and
   requires an exact match; complete also requires explicit confirmation;
6. reserves idempotency evidence bound to actor, item kind, item ID, and
   action;
7. delegates the lifecycle decision to the source owner as a flush-only
   participant;
8. writes personal snooze state when applicable;
9. stages audit and versioned outbox evidence; and
10. commits once, then emits best-effort realtime invalidation.

Ticket lifecycle already supports the participant pattern. Team Inbox
operator commands use the same pattern: they own their decisions but
participate in an active cross-domain coordinator transaction instead of
opening a nested root command.

Exact idempotency replay returns the prior outcome even when a completed item
has left the live projection. Reusing a key for another actor, item, or action
fails closed.

## Errors and adapter mapping

Stable domain failures include:

- item missing or outside native scope;
- action unavailable after current-state recheck;
- missing permission or owning service team;
- source-owner claim or completion rejection;
- missing confirmation or a missing/stale action-review fingerprint;
- missing, invalid, or conflicting idempotency evidence; and
- invalid item kind or snooze mode.

Web and API adapters map these errors. Domain services never raise HTTP
exceptions. Failed commands roll back idempotency, source state, snooze state,
audit, and outbox evidence together.

## Migration and retirement gates

This implementation is `cutover_ready`, not retired. The CRM module advances
to `retired` only when the shared ledger proves:

1. all seven native route behaviors and permissions;
2. authenticated browser behavior for filters, partial refresh, claim,
   complete, snooze, restore, error, empty, and narrow viewports;
3. production `WorkqueueSnooze` import/reconciliation or an approved,
   evidence-backed zero-data disposition;
4. caller and navigation cutover to `/admin/workqueue`;
5. shadow comparison of scope, eligible item membership, ordering bands, and
   source actions;
6. a rehearsed rollback that does not restore parallel source decisions;
7. removal of CRM workqueue routes, templates, action dispatcher, and snooze
   writer; and
8. a healthy 30-day zero-traffic observation from the ledger's named Loki and
   VictoriaMetrics evidence sources before CRM source deletion.

Production cutover evidence is operational evidence, not a code assertion.
Local worktrees and similarly named routes do not satisfy these gates.
