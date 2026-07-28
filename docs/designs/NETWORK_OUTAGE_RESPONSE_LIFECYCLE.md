# Network Outage Response Lifecycle Source of Truth

**Status:** Implemented (owner-output chain slice, 2026-07-27)
**System of record:** Sub
**Decision owner:** Michael

## Contract

```text
network observation (reconcile scan) / staff declaration
  -> outage suspected (classifier) or open (operator)
  -> outage confirmed (debounce satisfied) / declared
  -> impact resolved (affected customers from topology)
  -> operational owners/watchers attached, SLA escalations planned
  -> recovery observed (candidate absence) -> clearing
  -> outage resolved / discarded
  -> escalations canceled; recovery evidence emitted
```

Detection and recovery remain genuine observation loops
(`topology_outage_reconcile`, default 180s): physical reality changes without
a Sub command, so the classifier writes observations and drives the debounced
state machine. Owner-output chaining applies from each committed transition
onward.

## Owner-output chain

Every incident transition in `app/services/topology/outage.py` stages two
outputs atomically with the status write, dispatched after commit by
`events.dispatcher` with durable retry:

- the typed lifecycle event — `outage.created`, `outage.suspected`,
  `outage.confirmed`, `outage.clearing`, `outage.reopened`,
  `outage.rerooted`, `outage.discarded`, `outage.resolved`;
- the legacy `network.alert` fan-out with its unchanged payload for external
  webhook subscribers (CRM/mobile filter on `alert_type`).

The registered `OutageLifecycleProjectionHandler` consumes the typed outputs
and asks the next owners to apply consequences idempotently:

| Committed output | Consequence (owner) |
| --- | --- |
| `outage.created`, `outage.confirmed` | attach operational owner/watchers/room and enroll affected-customer watchers (`operations.sla_escalation` records via `outage_operations`); plan SLA escalation events and deliveries |
| `outage.resolved`, `outage.discarded` | cancel open escalation events; suppress pending deliveries |

A consequence that cannot be applied raises, so the event delivery stays a
failed, visible, retryable `event_store` row — never a warning log. A stale
replay after the incident terminated plans nothing.

Each consequence runs through the owner's receipted consumer commands
(`consume_outage_activation` / `consume_outage_termination`) inside
`execute_owner_command` on a fresh session: the effect and its unique
`(consumer, event_id)` receipt (`events.owner_outputs`, ADR 0007 §2) commit
atomically, so a redelivery is an exact no-op. `network.outage_lifecycle`
carries a complete typed `ServiceContract` in the executable registry.

## Named owners

| Decision or fact | Owner |
| --- | --- |
| Incident status vocabulary, transitions, typed outputs | `network.outage_lifecycle` |
| Debounced detection/recovery observation | reconcile scan (`app/services/topology/outage_reconcile.py`) |
| Affected-customer impact resolution | `network.outage_impact` |
| Escalation policy, events, deliveries, ack/cancel | `operations.sla_escalation` |
| Committed cross-owner consequence delivery | registered `OutageLifecycleProjectionHandler` adapter |
| Customer outage notifications (operator-gated) | outage notify console via `outage_notifications` |
| Customer-safe connection verdicts | `network.connection_health` |

## Boundaries

- **Outage resolution never closes Support Tickets or WorkOrders.** It emits
  recovery evidence (`outage.resolved`); Support and Field owners transition
  their own cases from that evidence. Architecture test:
  `tests/architecture/test_outage_lifecycle_chain_boundary.py`.
- Customer notification sending remains an explicit operator command
  (`OUTAGE_NOTIFY_ENABLED` + actor) — confirmation makes an incident
  *eligible* for notification; it does not auto-send.
- Suspected classifier incidents are internal: no operational owners,
  watchers, or escalations until confirmation (or operator declaration).
- Impact membership is derived state: consumers re-resolve affected
  customers from authoritative topology rather than trusting a declare-time
  snapshot; only the scalar `affected_count` is snapshotted on the incident.

## Failure and repair

A failed consequence is a failed `event_store` delivery retried by
`retry_failed_events`. The reconcile scan repairs nothing cross-owner; it
only advances the observation state machine. Stale open operator incidents
are surfaced (never auto-resolved) by the existing stale-incident alarm.

## Deferred (later slices)

- Durable per-entity SLA timers (per-level delays) via
  `runtime.durable_timers` (ADR 0007, merged); today escalation timing
  lives on `OperationalEscalationDelivery.cooldown_until` drained by the
  delivery runner.
- Field/support verification before resolution — recovery is currently a
  sustained-absence timer (`W_resolve`); a verification hop would be a new
  feature, not a chaining conversion.
- A typed `ServiceContract` for `network.outage_impact` (still
  uncontracted; `network.outage_lifecycle` is now fully contracted).
