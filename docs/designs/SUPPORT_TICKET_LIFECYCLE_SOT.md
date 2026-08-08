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
- explicit customer-publication decisions for descriptions, comments, and their
  attachments;
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

`support.ticket_vocabulary` owns the closed typed `TicketStatus` vocabulary and
terminal-state semantics consumed by both lifecycle and configuration. It is a
pure value boundary and owns no Ticket or configuration rows.

`support.ticket_configuration` owns operator-managed status choices,
priorities, types, routing inputs, service-team membership configuration, and
priority/type SLA targets. It may only expose statuses from the ticket
vocabulary owner. `support.ticket_region_projection` separately resolves the
current region choices from configured values and canonical Ticket observations.
This separation prevents lifecycle and configuration from depending on each
other while preserving the provenance of both inputs.

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
The database normalizes configured and observed values to a trimmed lowercase
identity, deduplicates the combined inputs, and orders the result by region
value ascending before it reaches forms and filters. Ticket list, count,
summary, and export queries compare that same normalized identity, so legacy
case variants such as `Garki` and `garki` remain one operational cohort even
before stored values are repaired.
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

The admin create form passes the typed
`TicketCreationAcknowledgementMode.customer_email` intent to the lifecycle
owner. After the Ticket, number, routing, audit, and event evidence are staged,
the owner requests one email-only customer acknowledgement using the linked
Subscriber identity. Other create adapters use `none` unless they deliberately
adopt this contract, so API, inbox, integration, and portal behavior does not
change. Missing customer identity or disabled support notifications produces no
email. Queue failure is isolated in an owner savepoint and recorded as durable
Ticket audit evidence without rolling back the Ticket.

Internal operational queues use the closed
`TicketCreationConsequenceMode.silent_internal` path rather than constructing a
Ticket row. The unmatched-radio coordinator is the only approved caller. The
lifecycle participant allocates the human-readable number and stages creation
audit/event evidence, while suppressing assignment policy, SLA clocks,
automation, staff assignment notifications, and customer acknowledgement. A
repeat observation repairs a legacy open queue item whose number is missing and
records that repair in audit/event evidence. The per-radio advisory lock remains
the deduplication authority; the Ticket owner remains the only identity writer.

Customer-authored public replies have one staff-email consequence owned by the
lifecycle command. After the comment is staged, the owner resolves active
individual assignees from current Ticket assignment fields and queues one email
per distinct staff address in the same transaction. It does not fan out to all
members of the assigned Service Team. When no active individual assignee has an
email address, the customer-scoped branding `support_email` is the single
helpdesk fallback. Internal, staff, system, or customer-identity-mismatched
comments do not trigger this consequence. The durable notification queue owns
post-commit SMTP delivery and retry; transport failure never removes the saved
reply.

Ticket assignment consequences are independent of the legacy customer-support
notification toggle. Newly assigned direct users and active members of an
assigned Service Team receive an in-app notification and, when an email address
exists, a queued email. Explicit comment mentions use the same individual and
Service Team group semantics and the same two channels. The retired Site
Project Coordinator column remains readable and filterable on historical
Tickets, but new-ticket input and assignment configuration no longer populate
it.

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

## Customer portal publication boundary

Selfcare is the authoritative origin for new customer tickets and replies. A
customer-authored Selfcare description or reply is customer-visible. Staff and
system descriptions, comments, and their attachments are internal unless a
staff command explicitly publishes them. Portal adapters consume these stored
decisions; they never infer publication from subscriber linkage, CRM metadata,
or the absence of an internal-note checkbox.

CRM ticket import is retired as an authority. Any residual retry or historical
observation is provenance-only and is forced internal; it cannot publish
narrative into the customer portal.

Legacy CRM-era narrative has no evidence of customer publication because the
old system exposed no customer ticket timeline. Migration 503 therefore marks
every description and comment already stored at cutover as internal. Ticket
identity, number, status, priority, and dates remain available for customer
reference. Operators may deliberately publish a reviewed description or post a
new reviewed customer reply after the cutover through the normal Selfcare
controls.

## List and bulk UI contracts

`ui.support_ticket_list_projection` declares searchable fields, filters,
stable sorting, pagination, summaries, and export scope through one typed
`ListQuery`. Targeted HTMX refreshes replace the result table and refresh its
summary, sort state, and export URL without rebuilding the filter and column
controls. The control layer announces loading in place, retains the current
results when a read fails, and offers retry. After a successful list read, the
browser stores the canonical `ListQuery` URL under a signed-in-user-specific
key. A bare return to the list restores that applied URL; an explicit URL wins
and replaces the stored view. The browser cache controls navigation only and is
never authoritative for Ticket facts. `ui.support_ticket_bulk_action_projection`
declares page-only selection and action presentation. `support.ticket_bulk_commands`
resolves membership, normalizes the shared changes, previews eligibility, binds
the preview to a deterministic scope token, and detects drift. Confirmed
mutations delegate to `support.ticket_lifecycle`; there is no second bulk writer.

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
  checked in; and
- unmatched-radio code cannot construct a Ticket directly or call the
  silent-internal lifecycle participants from any other service module.

Repair reruns deterministic list/preview queries, SLA reconciliation, or the
provenance verifier from canonical records. It never re-enables a legacy writer
or infers lifecycle authority from CRM, tags, templates, cached UI state, or
communication delivery.

## Staff Talk consequences

Assignment changes and explicit ticket-comment mentions stage a durable
`nextcloud_talk` staff notification inside the Ticket owner command. The
assignment command ID or comment ID is part of the delivery dedupe identity,
and the comment author is excluded. The Ticket owner does not resolve
Nextcloud credentials, create rooms, or perform HTTP; those consequences belong
to `communications.nextcloud_talk_staff` after commit. Staging failure is
isolated in an owner savepoint and cannot reject the ticket mutation.
