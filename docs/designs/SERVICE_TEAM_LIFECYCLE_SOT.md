# Service-team lifecycle source of truth

Status: cutover-ready implementation; reviewed Party/membership adoption and
production migration evidence pending.

## Ownership

`operations.service_team_lifecycle` owns native service-team identity,
activation, manager assignment, membership lifecycle, role changes, and the
active-team/admin projections. Inbox, ticket configuration, ticket assignment,
workqueue, outages, dispatch, projects, and delivery services are consumers.

`party.registry` owns Person Party identity and the reviewed
`SystemUser.person_party_id` binding. `auth.staff_provisioning` owns the staff
authentication principal. Service-team `person_id` fields persist Person Party
IDs; operational adapters translate to current SystemUser principal IDs only at
their existing assignment/delivery boundaries.

An agent is not a second identity model. In Sub, an operational agent is an
active staff `SystemUser`, reviewed active Person Party binding, and active
`ServiceTeamMember`.

External HR observation and reconciliation is a separate follow-up slice. This
slice neither names an HR provider nor gives an external system authority over
team or membership state. That follow-up must keep native records
authoritative, treat provider payloads as observations, and reconcile through
this owner with explicit completeness, idempotency, provenance, and drift
repair semantics.

`operations.service_team_party_cutover` is the one-time pre-migration
coordinator for the already-imported CRM boundary. It consumes an exact,
reviewed CRM Person-to-SystemUser decision plan, delegates Party creation and
binding to `party.registry`, and adopts the CRM membership snapshot through a
serializable transaction. CRM identifiers remain provenance; the coordinator
does not give CRM continuing write authority.

## Command and lifecycle rules

- Public writes enter `execute_owner_command` once on a transaction-free
  session.
- Team and membership rows are locked; team names are unique
  case-insensitively.
- New manager/member selections require an active SystemUser, an active Person
  Party, and a reviewed binding between them.
- Reactivation and member-role changes revalidate the current staff principal
  and Person Party state; a retired or ambiguous identity fails closed.
- Create binds a caller-supplied UUID. Exact desired-state commands replay;
  changed evidence or a deactivated row under the same identity fails closed.
- Team changes use `updated_at` stale evidence.
- A team with active members cannot be deactivated.
- Membership removal deactivates the row. Team or membership hard-delete is not
  exposed.
- Audit and versioned domain events are staged in the owner transaction.
- `resolve_staff_service_team` is the typed principal-to-Party membership query.
  It returns resolved, identity-unavailable, no-membership, or ambiguous
  outcomes and never selects a team by row creation order.

## Admin page contract

- Screen: `admin.system.service-teams`; list, detail, and lifecycle editor.
- Audience/job: operations administrators maintain shared team topology and
  membership used by Inbox, tickets, workqueue, dispatch, projects, outages,
  and delivery.
- Decision: identify the active team, its type/region/manager, current active
  membership, and the one valid lifecycle or membership action.
- Read owner: `operations.service_team_lifecycle` active-team and administration
  projections. Command and eligibility owner:
  `operations.service_team_lifecycle`. Authorization owner:
  `auth.permission_gate`.
- First viewport: team identity, active state, type, region, manager, active
  member count, and the applicable edit or activate/deactivate action.
- Actions: list has one create action; detail has edit, activation/deactivation,
  add member, role change, and remove member. No hard delete or bulk mutation is
  exposed. Deactivation requires a reason and is unavailable while active
  memberships remain.
- Fields and sensitivity: team metadata is operational; staff display name and
  work email are internal identity data and appear only behind service-team
  read/membership permissions. Party identifiers remain evidence-level data.
- List behavior: server-owned search and active/inactive filter, active teams
  before inactive teams, name order within state, bounded pagination, and no
  export because the screen is a control plane rather than a reporting source.
- The secondary role/region projection is derived from native active
  membership. CRM-only designation labels have no canonical Sub source and are
  retired rather than recreated.
- States: empty, filtered-empty, not-found, stale, invalid identity, duplicate
  name, active-member deactivation rejection, and unauthorized are explicit.
  The UI never falls back to retired workflow-setting payloads.
- Responsive projection: identity, state, member count, manager, and next action
  remain visible; secondary metadata and evidence move below the summary.
- Audit/observability: every mutation returns to the committed read projection
  and is backed by the canonical audit event and versioned domain event.

## Migration and retirement gate

Migration 426:

1. rejects duplicate case-insensitive team names;
2. backfills any remaining workflow-setting teams;
3. resolves workflow and native compatibility member UUIDs through reviewed
   SystemUser-to-Person-Party bindings;
4. fails on unbound, inactive, ambiguous, conflicting, or duplicate identity
   evidence, including active membership without an active SystemUser principal;
5. adds Party foreign keys, existing team-provenance integrity constraints, and the
   case-insensitive name index;
6. deletes `support_service_teams` and `support_service_team_members`.

Ticket settings now consumes the native active-team projection and cannot write
team or membership rows. The CRM hard-delete capability is intentionally
retired: deactivation retains the identity required by historical tickets,
conversations, work orders, projects, outages, and audit evidence.

Production cutover is complete only after migration preflight/apply evidence,
caller traffic verification, and confirmation that no CRM route/job remains a
writer. Authenticated browser lifecycle, CSRF, and permission parity is enforced
by `tests/playwright/e2e/test_service_teams.py` before that production gate.

Before migration 426, the protected workflow in
`docs/runbooks/SERVICE_TEAM_PARTY_CUTOVER.md` must report zero aggregate
identity blockers. Empty native membership rows are not completeness evidence:
the reviewed plan is built from the CRM membership source snapshot and must be
applied before the migration rehearsal and release authorization.

## CRM parity disposition

The CRM `service_teams` module’s list, create, detail, edit, activate,
deactivate, add-member, and remove-member capabilities are replaced by
`/admin/system/service-teams`. Hard delete is removed by policy and replaced by
audited deactivation. The module cannot be marked retired in the CRM retirement
ledger until production usage and retirement evidence are attached.
