# Shared outage work-order handoff

## Purpose

An infrastructure ticket represents one shared outage affecting many
customers. Staff can issue one field work order for that outage without
creating one customer work order per affected subscriber.

## Authority

- `network.outage_lifecycle` owns the incident and its immutable audience
  revisions.
- `support.ticket_lifecycle` owns the canonical infrastructure ticket.
- `network.outage_work_order_handoff` decides whether field work may be issued
  and records the outage-to-work-order link.
- `operations.work_order_commands` owns the native work-order row and dispatch
  lifecycle.

The work order has `work_order_kind=infrastructure` and no `subscriber_id`.
The link stores the incident, target, audience revision, and membership token.
The exact customer audience remains on the outage revision; it is not copied
onto the work order.

## Flow

1. Staff opens an active outage in Outage Console.
2. The outage must have a current audience revision and a canonical active
   infrastructure ticket.
3. An active member of the ticket's assigned team enters the field instructions
   and submits the form.
4. The coordinator rechecks permissions, ticket/team membership, incident
   status, and the audience revision. A stale form is rejected.
5. The coordinator stages the infrastructure work order, provenance link,
   audit record, and event in one transaction.
6. Dispatch assigns and completes the work order through the existing field
   workflow. Completing it does not resolve the outage or ticket automatically.
7. Staff verifies restoration, then resolves the outage and closes the ticket
   through their existing owners.

Retries with the same request key replay the same work order. Reusing a key
with different instructions is rejected.
