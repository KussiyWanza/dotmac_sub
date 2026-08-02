# ONT WAN service intent source of truth

Status: implemented (owner and commands); partial uniqueness deferred

## Canonical owner

`network.ont_wan_service_intent` (`app.services.network.ont_wan_service_intent`)
is the only writer of declared WAN service intent. It answers one question:

> Is *this exact service* declared to terminate *this connection type* on *this
> ONT*, and is that declaration currently active?

Nothing else may answer it. Desired configuration, staged delivery plans,
credential fingerprints and surviving assignment fields are evidence that
something once wrote a value; none of them is a declaration of intent.

## Why the record needed an owner

`OntWanServiceInstance` modelled service intent but had **no application
writer**: no constructor existed outside tests, and production held 8 rows
against 1,523 ONTs. A table written by nothing cannot authorise anything.

This is the second time the same mistake was caught in this domain. An earlier
gate authorised on `OntAssignment.pppoe_username`, which migration 084 had
copied into desired config and then explicitly set `NULL`
(`084_backfill_ont_desired_config_from_assignments.py:112`). The 12 surviving
values were unexplained residue, not intent. **A field's existence is not its
provenance** — before a value may authorise anything, name the writer.

## Grain: exact service, not device

Intent binds `ont_id` **and** `subscription_id`. An ONT-grain claim says "this
device may terminate PPP", which is not "this service terminates here". A
delivery ruling built on the weaker claim can hand one service's credential to
another service sharing the device — the failure this owner exists to prevent.

A ruling for service A must be unusable for service B. Both identifiers are
therefore required on the read path (`active_primary_internet_intent`); there is
deliberately no ONT-only variant.

## Authority fields

| Field | Role |
| --- | --- |
| `lifecycle_state` | **The authority.** `planned`/`unverified` → `active` → `retired`. |
| `is_active` | **Derived.** Kept in step by the owner; never a second authority. |
| `is_primary` | Which instance carries the service's primary Internet termination. |
| `priority` | May *order* instances. **Never selects authority.** |
| `revision` | Bound into a delivery ruling; a ruling taken before a replace cannot authorise a write after it. |
| `declared_by`, `declared_reason`, `evidence_ref` | Provenance. Required on every transition. |

`priority` ordering and authority selection are kept separate on purpose: an
ordering field silently becomes a tie-breaker for authority the moment anything
reads "the first one", and a tie-break is exactly the ambiguity that must fail
closed instead.

## Invariants

- **One active primary Internet instance per subscription**, and one per ONT.
- Other service types (IPTV, VoIP) remain multi-WAN capable and are not
  constrained by the singular-primary rule.
- The Internet scoping is enforced on the enum *value*: `str(OntServiceType.internet)`
  yields `"OntServiceType.internet"`, so a naive string comparison silently
  treats every Internet instance as unconstrained. `_enum_value()` exists for
  this reason and must be used for every service-type comparison.
- Ambiguity is a refusal, never a selection.

## Commands

All four are typed owner commands with locking, idempotency, actor, reason,
evidence, revision and staged events:

- `declare_wan_service_intent` — creates in `planned`; never authorising.
- `activate_wan_service_intent` — `planned`/`unverified` → `active`, with
  optional `expected_revision` for optimistic concurrency.
- `replace_wan_service_intent` — retire-then-activate as one transition, linking
  `replaced_by_id`.
- `retire_wan_service_intent` — the single path out.

Refusals are typed (`IntentRefusal`) and distinct: a duplicate primary on the
subscription and a duplicate primary on the ONT are different problems with
different repairs, so they are different codes.

### In-transaction retirement

ONT lifecycle flows (return-to-inventory, decommission) reset many facets of a
device as one unit of work and are already inside a transaction, while
`execute_owner_command` requires a transaction-free session at entry.
`retire_ont_intents_in_transaction` serves them: same transition, same
provenance validation, same staged event, with the transaction boundary left to
the caller — whose atomicity requirement is the stronger one, since a device
must not end up half-returned with its intents still active.

These entry points do not yet thread an operator identity, so `context.actor`
names the owning system. Attributing a machine reset to a person would be the
worse record.

## History is not deleted

Assignment release, service movement, cancellation and return-to-inventory
**retire** through this owner. `reset_ont_service_state` previously deleted
every WAN service instance row for the ONT.

Deletion destroys the only record of what a service was declared to be, and that
record is the evidence a later adjudication depends on. No application code path
may delete an `OntWanServiceInstance`.

## Existing rows start non-authorising

Migration `456_ont_wan_service_intent_owner` lands **every** pre-existing row in
`unverified` regardless of `is_active`, with a reason recording that its
provenance is unknown. Adopting them as intent would repeat the residue mistake
above.

## Deferred: partial uniqueness

The singular-primary invariants are enforced by the owner commands now. The
partial unique indexes come in a later migration, **after inventory, backfill
and verification**. Adding them in the introducing migration would either fail
on unadjudicated data or force the migration to pick a winner — an ownership
decision a migration must not make.

## Related owners

- `network.ont_reconcile` — converges ONT state; must not decide PPP intent.
- `network.cpe_dialer_credential_sync` — producer gate, currently disabled
  (`default=False, on_missing=False`).
- Delivery-time authorization consumes `active_primary_internet_intent`. The
  last-boundary invariant is enforcement inside `apply_plan`, where an absent or
  wrong-scope ruling equals refusal, and refused PPP drift stays visible rather
  than being silently reconciled away.
