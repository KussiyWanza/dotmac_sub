# Composable service-team source of truth

Status: composable authority in shadow; legacy scalar contraction pending
complete-cohort verification.

## Ownership

`operations.service_team_lifecycle` owns only stable native team identity,
activation, Party-backed membership, and their administration projections.

`operations.service_team_composition` owns:

- registered capability definitions and team capability bindings;
- multiple responsibilities per membership;
- explicit parent/child, escalation, and collaboration relationships;
- typed global or `GeoArea` scope bindings;
- multiple provider-neutral external observations; and
- domain route keys that select an exact team.

`party.registry` owns Person Party identity.
`auth.staff_provisioning` owns staff principals and RBAC remains the permission
owner. A responsibility can narrow operational scope; it never grants a
permission. A caller must hold the relevant RBAC permission and be within the
returned team scope.

External workforce or CRM systems are observations. They never define local
team identity, membership, responsibility, or access.

## Data contract

`ServiceTeam` is a stable operational group identity with a name, active state,
and lifecycle timestamps. The following facts are composed:

- `ServiceTeamCapabilityDefinition` is governed, versioned vocabulary. Code may
  consume only registered keys with a named contract owner.
- `ServiceTeamCapability` assigns zero or many capabilities to a team.
- `ServiceTeamMember` records Party-backed membership and has no authoritative
  role.
- `ServiceTeamMemberResponsibility` assigns zero or many of
  `accountable_manager`, `queue_lead`, `agent`, `dispatcher`, and `on_call`.
- `ServiceTeamRelationship` records explicit directed topology.
- `ServiceTeamScopeBinding` represents either global scope or one typed
  `GeoArea` reference. New scope kinds require new typed columns and checks;
  arbitrary JSON or polymorphic string identifiers are forbidden.
- `ServiceTeamExternalReference` records provider, account scope, external
  identifier, provenance, observation time, and lifecycle. Many observations
  may refer to one team; no observation is a local identity.
- `ServiceTeamRoutingPolicy` records a domain, route key, exact team, optional
  typed team scope, priority, and lifecycle. Code-consumed domain/route pairs
  are registered with a contract owner, version, and required capability;
  callers cannot invent a route key or substitute a different capability.

Capability vocabulary is seeded by migration 440. Teams, members, managers,
responsibilities, scopes, relationships, routing decisions, and access grants
are never seeded.

## Query and routing rules

Principal-to-team resolution is set-valued. Zero teams means no membership;
many teams are normal. A domain requiring one team must use its exact work
assignment or an explicit route, never creation order, a team name, or scalar
type.

Capability and responsibility queries return stable sets. Scope filters may
only narrow them. Explicit routing derives eligibility from the registered
domain contract and rejects unknown route keys or multiple policies at the
winning priority.

The migrated consumers are:

- outage coordination: `network.outage` route keys select exact primary,
  support-watcher, and field-watcher teams, with capability eligibility;
- Team Inbox outbound: explicit route metadata or caller activity wins,
  otherwise one unambiguous governed capability activity may be used;
- Team Inbox conversation and performance projections expose the full
  capability set rather than a scalar team type;
- field-job chat: the exact `WorkOrderAssignmentQueue` and its active
  `DispatchRule.service_team_id` decide the team;
- workqueue: membership scopes self work, while team audience requires both the
  RBAC audience scope and `queue_lead`/`accountable_manager` responsibility.

## Commands, transactions, and events

Public lifecycle and composition writes enter `execute_owner_command` once on a
transaction-free session. Teams lock before memberships, definitions, scopes,
relationships, external observations, and routing policies. Nested helpers
flush only. Unique/check/foreign-key constraints arbitrate concurrent winners.

Exact desired state replays. Identity reuse, unregistered capability, inactive
team/member/scope, provider-reference conflict, and ambiguous routing fail
closed. State changes stage versioned service-team events and aggregate,
PII-minimized audit evidence in the owner transaction.

## Admin page contract

- Screen: `admin.system.service-teams`; list, detail, and identity editor.
- Audience/job: authorized operations administrators maintain stable team
  identity and composable operational facts.
- First viewport: name, active state, capabilities, typed scope summary,
  accountable managers, member count, and next lifecycle action.
- Identity edit changes only the name. Capability and responsibility actions
  are independent. Membership add never asks for a scalar role.
- Staff identity data remains permission-gated. Provider identifiers and Party
  identifiers are evidence-depth data.
- No hard delete or bulk mutation is exposed. Deactivation retains historical
  references and is blocked while active memberships remain.

## Migration and verification

Migration 426 is not rewritten. The pre-migration procedure in
`docs/runbooks/SERVICE_TEAM_PARTY_CUTOVER.md` replaces the historical CRM
membership-adoption guard. It verifies five native pointers and retires only
the workflow sources, scalar manager pointer, and compatibility memberships
that migration 426 would reject.

Migration 440 adds the composable schema and idempotently backfills:

- legacy team type to one registered capability;
- legacy membership role to one or more responsibilities;
- scalar manager to `accountable_manager` responsibility on membership; and
- workforce system/reference to a provider-neutral external observation.

It does not infer `GeoArea` from `region`.

The legacy columns `team_type`, `region`, `manager_person_id`,
`service_team_members.role`, `workforce_system`, and
`workforce_department_reference` remain nullable shadow inputs. Contract
requires all of the following:

1. a complete-cohort shadow run reports zero missing capability,
   responsibility, manager-responsibility, reviewed geographic-scope, and
   external-reference bindings;
2. every consumer reads composition or an explicit assignment/route;
3. existing drift has an idempotent repair path;
4. architecture tests prevent scalar decision reads from returning;
5. production traffic and source-retirement evidence are attached; and
6. rollback requirements have expired.

Only then may a later forward migration drop the scalar columns and legacy
enums. Reconstructing or dual-authoring scalar authority is forbidden.

## CRM parity disposition

The native surface replaces list, create, detail, edit, activation,
deactivation, membership, and responsibility administration. CRM hard delete
is intentionally removed. CRM membership import, email identity matching, and
the 169-person review direction are not part of the target and must not return.
