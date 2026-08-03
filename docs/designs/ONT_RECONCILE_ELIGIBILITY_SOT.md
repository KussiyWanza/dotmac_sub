# ONT reconciliation eligibility source of truth

Status: implemented (owner, sweeper integration); rollout in progress

## Canonical owner

`network.ont_reconcile_eligibility` (`app/services/network/ont_reconcile_eligibility.py`)
is the only writer of per-ONT automatic-reconciliation eligibility. It answers
one question:

> May the sweeper touch *this* ONT, for *this* scope, right now?

## Why it exists

The only previous way to stop the sweeper reaching a device was the fleet-wide
`network.ont_reconcile` control. That is far too blunt for excluding a handful
of devices:

- it halts convergence for **every** ONT (~1,500), not the few under review;
- `_close_expired_remote_access()` and `_reconcile_dialer_credentials()` run
  **inside** `run_ont_reconcile_sweep` *after* the gate, so disabling it also
  silently pauses expired remote-access cleanup and the dialer reconcile.

Excluding five devices should not cost the rest their convergence, nor stop
unrelated maintenance as a side effect nobody sees.

## What a hold is

A reviewed, evidenced decision that one ONT is excluded from one scope. It is
not a feature flag and not a cache — it records a judgement about a customer
device, so the evidence is mandatory:

| Field | Why it is required |
| --- | --- |
| `reason_code` | Stable machine code for reporting |
| `explanation` | The human sentence; a code alone does not explain a decision |
| `actor` | Who asked |
| `reviewer` | Who agreed — **must differ from `actor`** |
| `review_due_at` | When a human must look again |
| `idempotency_key` | So a retry cannot create a second hold |

**Self-review is not review.** Suppressing convergence on a live customer
service is a two-person decision, and the owner refuses `reviewer == actor`.

## `review_due_at` is NOT an expiry

Nothing releases a hold on a timer. An expiring hold would hand a suppressed
device back to the sweeper at an arbitrary moment — precisely the surprise a
hold exists to prevent, and it would do so without anyone deciding.

An overdue hold **stays active** and becomes reportable through
`overdue_holds()` so it can be escalated. `release_reconcile_hold` is the only
way one ends. A guard test asserts there is exactly one assignment to
`released` in the module and that no expiry/scheduling construct appears.

## Scope

`automatic_sweep` only, deliberately. An operator doing reviewed repair must
still be able to drive a device explicitly — otherwise a held ONT has no
legitimate path back to convergence, and the hold becomes a trap rather than a
pause.

## Uniqueness

One **active** hold per ONT and scope, as a **partial** unique index. Released
holds accumulate as history and must not block a future hold; a full unique
constraint would turn history into a permanent lockout.

The predicate is declared for **both** dialects (`postgresql_where` *and*
`sqlite_where`). Setting only the Postgres clause left the test database with a
full unique constraint, so released-then-rehold behaved differently there and
the case had to be skipped — hiding the very difference that mattered. With
both declared it runs everywhere, and a schema assertion pins that both are
present.

## Lock order

**`OntUnit` → active hold.** Placement, release and the sweep all take it, and
nothing may reverse it. Reversing it would deadlock placement against release.

The `OntUnit` lock is the parent lock and is held through reconciliation and
transaction completion, so a placement that lands mid-pass waits rather than
racing.

## Sweeper integration

The decision is taken **at the point of use**, per ONT, inside the transaction
that acts on it (`eligibility_under_lock`). `held_ont_ids` remains only as a
pre-filter: a set captured at the start of a pass cannot see a hold placed
during the pass, so acting on the snapshot means acting on stale state.

Held ONTs are skipped **before any ping, read or write** — contacting a device
to discover it is held would defeat the point. `_sweep_one` returns a typed `SweepOutcome` carrying a `SweepDisposition`
(`reconciled` / `unreachable` / `held` / `missing`). A `(reachable, success)`
tuple could not express "we chose not to", so a hold discovered at the point of
use was counted as `skipped_unreachable` — reporting a deliberate exclusion as
an outage. `held` is surfaced in both the task result and the
`sweep_cycle_complete` structured log.

## Idempotency

The key is **mandatory** and non-null in the schema. A replay is honoured only
when the *entire* command matches (ONT, scope, reason, explanation, reviewer,
actor, review date); any other reuse returns
`reconcile_hold_idempotency_conflict`, so a recycled key cannot silently
substitute one decision for another.

## Concurrency proof

`tests/integration/test_ont_reconcile_hold_concurrency.py` runs on PostgreSQL
with two sessions and proves placement waits behind the sweep's `OntUnit` lock,
that a hold committed before lock acquisition produces zero device contact and
is recorded as `held`, and that a losing concurrent release returns its stable
refusal. Static guards cannot establish any of this.

## Rollout

1. Keep the fleet-wide hold active.
2. Deploy this owner.
3. Place reviewed holds on the cohort ONTs.
4. Verify eligibility refusals and audit records.
5. Re-enable the global sweep.
6. Confirm the held ONTs remain untouched while fleet convergence **and
   expired remote-access cleanup** resume.

Expired remote-access grants must keep being checked until step 5 completes,
because the fleet-wide hold pauses that cleanup as described above.

## Related owners

- `network.ont_reconcile` — fleet-wide control, retained as an emergency stop,
  no longer the mechanism for excluding individual devices.
- `network.ont_wan_service_intent` — the adjudication that typically motivates
  a hold.
