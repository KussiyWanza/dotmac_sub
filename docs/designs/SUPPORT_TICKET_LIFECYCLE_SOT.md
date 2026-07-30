# Support Ticket lifecycle source of truth

Status: implemented

## Canonical owner

`support.ticket_lifecycle` (`app.services.support`) is the only lifecycle
writer. The retired `support.tickets` architecture name has no registry entry
and must not appear as an owner, dependency, or client contract.

The lifecycle owner controls, in one root owner command:

- Ticket creation, native identity, customer/Lead binding, and ticket number;
- guarded status transitions and their resolved/closed timestamps;
- team, technician, manager, coordinator, and additional-assignee state;
- comments, explicit mentions, attachment metadata, and the official timeline;
- links, duplicate evidence, merges, and merged-source immutability;
- resolution requests, active confirmation capabilities, confirmation, disputes,
  and automatic confirmation after the configured grace period;
- CSAT/satisfaction evidence; and
- transactionally staged audit records, domain events, notifications, and SLA
  consequences.

Adapters may create/close sessions and map `DomainError` values. They do not
commit lifecycle changes. Every public mutation enters
`execute_owner_command` once on a transaction-free session; nested helpers use
`flush()` only.

## Configuration and policies

`support.ticket_configuration` owns operator-managed status choices,
priorities, types, routing inputs, service-team membership configuration, and
priority/type SLA targets. It may only expose statuses from the lifecycle
vocabulary.

Assignment is split deliberately:

- `support.ticket_assignment_rule_configuration` owns typed assignment rules;
- `support.ticket_assignment_evaluation` evaluates active rules and owns only
  its locked round-robin cursor; it returns an immutable `AssignmentResult`;
- the Ticket owner rechecks and applies the proposed team/person consequence.

Automation has the same separation:

- `support.ticket_automation_rule_configuration` owns typed automation rules;
- `support.ticket_automation_evaluation` returns immutable
  `TicketAutomationProposal` values and never writes a Ticket or rule firing
  timestamp;
- the Ticket owner validates and applies accepted consequences.

Manual-review identity evidence fails closed for sensitive assignment and
automation. A policy never becomes a lifecycle writer merely because its
proposal is accepted.

Customer-portal creation uses a distinct, typed configuration resolution.
`support.ticket_configuration` resolves the first active Service Team whose
name matches exactly after case normalization, in this order:
`Customer Experience`, then `System Admin`, then intentional unassignment.
The resolver consumes native Service Team identity from
`operations.service_team_lifecycle`; it does not own or hard-code team UUIDs.

The portal validates Region against the same current canonical region
projection rendered by the admin ticket form. It passes that canonical value
and the typed team resolution into `support.ticket_lifecycle`.
The region projection is recomputed from workflow configuration and distinct
non-empty Region values on active Tickets in the caller's current database
transaction; it has no cache or stale fallback. Re-reading is its idempotent
rebuild path, and form-context parity tests are its drift signal.
`TicketCreationRoutingMode.preserve_requested_team` then prevents assignment
rules and `assign_team` creation automation from replacing either the resolved
team or the intentional unassigned result. Other creation automation continues
to run. Other ticket creation adapters retain the default
`evaluate_policy` mode.

The lifecycle owner persists Region and final team state and stages assignment
notifications, audit evidence, and the `ticket.created` event in its root
transaction. Audit and event evidence include the creation routing mode and
final team identifier, including a null identifier for intentional
unassignment.

## Related owners

`support.ticket_sla_clock` remains the Ticket SLA clock and breach owner.
`support.ticket_work_order_handoff` remains the only issuance/provenance
boundary into field work. Issuance requires ticket-update and dispatch-write
permission evidence plus an idempotency key. A field result may add internal
timeline evidence, but cannot resolve or close the Ticket.

Support and `communications.team_inbox` remain separate owners. No checked-in
workspace contract approves unification. Existing screens may compose their
read projections, but neither domain may mutate the other or introduce a
competing workspace lifecycle.

External CRM and communications products are observations, transports, or
provenance. Imported identifiers do not own Ticket status, assignment,
comments, resolution, or native Work-Order issuance.

## List and bulk UI contracts

`ui.support_ticket_list_projection` declares searchable fields, filters,
stable sorting, pagination, summaries, and export scope through one typed
`ListQuery`. `ui.support_ticket_bulk_action_projection` declares page-only
selection and action presentation. `support.ticket_bulk_commands` resolves
membership, normalizes the shared changes, previews eligibility, binds the
preview to a deterministic scope token, and detects drift. Confirmed mutations
delegate to `support.ticket_lifecycle`; there is no second bulk writer.

## Cutover and repair

The migration is complete only while architecture guards prove that:

- no Support service raises FastAPI `HTTPException` or completes a nested/root
  transaction directly;
- assignment and automation evaluators do not write Ticket lifecycle fields;
- every registered Support/UI service has a complete typed `ServiceContract`;
- the six completed services are absent from the shrink-only legacy manifest;
- the retired lifecycle-owner name is absent from architecture documents;
- Work-Order provenance is preserved and verified as described in
  `docs/runbooks/TICKET_WORK_ORDER_PROVENANCE_CUTOVER.md`; and
- Support/Inbox remain separate unless a later approved workspace contract is
  checked in.

Repair reruns deterministic list/preview queries, SLA reconciliation, or the
provenance verifier from canonical records. It never re-enables a legacy writer
or infers lifecycle authority from CRM, tags, templates, cached UI state, or
communication delivery.
