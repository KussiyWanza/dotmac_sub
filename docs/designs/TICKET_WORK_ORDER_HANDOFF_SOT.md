# Ticket to Work-Order Handoff Source of Truth

Status: implemented

## Decision

A support ticket records an incident, its triage, the subscriber, and the team
accountable for the response. Field work is a separate operational command. An
active member of the ticket's active assigned service team must explicitly
issue each work-order scope. The HTTP adapters additionally require both
`support:ticket:update` and `operations:dispatch:write`.

`support.ticket_work_order_handoff`
(`app.services.ticket_work_order_handoff`) owns issuance eligibility,
idempotency, and native provenance. It delegates work-order creation to
`operations.work_order_commands`; dispatch and field execution continue to own
the resulting work order. One ticket may issue zero or many work orders.

When the incident belongs to planned project work, the team may select a
`ProjectTask` already linked to that ticket. The handoff validates that ticket
link, infers the task's project, and asks the work-order command owner to write
`work_order.project_task_id`. One task may therefore require several field
visits without storing a single-work-order cache on the task.

The canonical relationship is the nullable, indexed foreign key
`work_order.origin_ticket_id -> support_tickets.id`. It uses `RESTRICT` on
delete so operational evidence cannot be orphaned. Generic work-order create
and update contracts expose the field for reads only and cannot establish or
change it.

Ticket work-order site resolution belongs to this handoff coordinator. Explicit
operator input wins; otherwise a linked project's customer address is used,
followed by the subscriber's canonical service `Address`. The ticket UI displays
and prefills that canonical service location but does not create a private copy.

## Lifecycle

1. Helpdesk captures and triages the incident in the ticket owner.
2. Helpdesk assigns the ticket to the appropriate service team.
3. An active member of that team explicitly issues a reasoned field scope with
   an idempotency key.
4. The work-order command owner creates a draft native work order and records
   the origin foreign key in the same transaction as ticket-side audit evidence.
5. Dispatch assigns the work and field operations execute it under the existing
   work-order owners.
6. Completion or unable-to-complete atomically projects an internal system
   comment back to the official ticket timeline with the work-order and field
   event identities.
7. Support verifies the result and separately decides whether to resolve or
   request customer confirmation. Field completion never changes ticket status.
8. Authenticated self-care/mobile and the signed public link converge on the
   same active confirmation capability: confirmation closes the ticket and a
   dispute reopens it with the customer reason.

## Authority migration

Old owner/path: `app.services.support.Tickets._ensure_field_visit_work_order`
treated the `field_visit` tag as a command, created one work order, copied its
identity into `Ticket.metadata.work_order_id`, and stored the native ticket UUID
in the CRM provenance column `WorkOrder.crm_ticket_id`.

New owner/path: `support.ticket_work_order_handoff` decides explicit issuance;
`WorkOrder.origin_ticket_id` is the only native link.

Migration `382_ticket_work_order_handoff` backfills only work orders that carry
both sides of the retired native automation evidence, clears that overloaded
native UUID value, and removes the duplicate ticket/work-order metadata keys.
It deliberately leaves imported `WorkOrder.crm_ticket_id` provenance intact.

Migration `406_support_ticket_work_order_provenance` then joins preserved CRM
Work-Order provenance to the exact imported Ticket provenance in
`Ticket.metadata.crm_ticket_id`, requires subscriber agreement, fails on
duplicate or conflicting identity, and fills `origin_ticket_id` without
clearing `crm_ticket_id`. Unmatched external rows remain provenance-only; the
migration never guesses from titles, tags, or timestamps. Operator preflight,
verification, and repair are documented in
`docs/runbooks/TICKET_WORK_ORDER_PROVENANCE_CUTOVER.md`.

Cutover gates are:

- the tag-triggered method and both create/update call sites are absent;
- the generic work-order write schemas cannot accept `origin_ticket_id`;
- issuance enforces subscriber, team, membership, lifecycle, and idempotency;
- repeated issuance with the same key returns the same work order while a new
  key may create another;
- field outcome evidence is written exactly once with the field event and does
  not change ticket status;
- both ticket and dispatch projections link to the authoritative relationship.
- historical exact CRM links have native origins while retaining their CRM ids.

There is no runtime fallback. Repair consists of reconciling the native foreign
key from retained audit evidence, never re-enabling tag automation or metadata
caches.


## Owner-output chain and durable timers (2026-07-27)

Each owner's committed transition stages its typed output atomically
(`ticket.work_order_issued`, `work_order.field_outcome_recorded`,
`ticket.resolution_requested/confirmed/disputed`); the registered
`SupportLifecycleProjectionHandler` delivers outputs — and fired
`runtime.durable_timers` triggers — to receipted consumer commands
(`events.owner_outputs`), so each effect commits atomically with its unique
`(consumer, event_id)` receipt and a redelivery is an exact no-op.

- The field owner no longer projects onto the ticket timeline inline: field
  completion emits `work_order.field_outcome_recorded`, consumed by the
  handoff owner's `consume_field_outcome`. Field completion still never
  changes ticket status; support verifies and resolves separately.
- The resolution-confirmation grace period is a durable per-ticket timer
  (`resolution_confirmation_due`) staged with the request; its fired trigger
  drives the receipted auto-confirm consumer. The unscheduled
  `auto_confirm_pending` sweep remains legacy repair only.
- Conversation snooze wake is a durable per-conversation timer
  (`snooze_wake`) staged with the snooze; the 300s `team_inbox_snooze_waker`
  scan remains as legacy repair for pre-timer rows until its retirement
  gate (all live snoozes carry timers).
- Due-timer dispatch (`durable_timer_dispatch_runner`, permanent) is the
  first production driver of `runtime.durable_timers`.
- Deferred: response-SLA and appointment timers (no owner-command producer
  on the inbound path / no appointment entity on this chain yet); the
  CX-comms and inbox field-job consequences remain inline flush-only
  participants of the field transaction.
