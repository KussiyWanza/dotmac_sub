# ADR 0002: Manifest-verified owner-command transaction boundary

Status: accepted

Date: 2026-07-19

Decision owner: Michael / Dotmac architecture

Affected systems and domains: Dotmac Sub public write commands, application
coordinators, database-session adapters, and transactional event producers

## Context

Dotmac Sub has several transaction idioms: direct service commits, adapter
commits, the legacy `UnitOfWork`, and auto-committing session context managers.
Documentation says a public command owner completes the business transaction,
but no runtime primitive proves that the caller is contracted, that the
session is clean at entry, or that a nested helper did not commit early.

That ambiguity permits partial commits, caller-owned transactions, and
architecture declarations that are disconnected from execution.

## Decision

`app.services.owner_commands.execute_owner_command` is the standard root
transaction executor for new and migrated public write commands.

- A command has typed business input plus `CommandContext`, carrying command,
  correlation, optional causation and idempotency identifiers, actor, scope,
  and reason.
- `OwnerCommandDefinition` links the command to one exact concern in the typed
  SOT manifest.
- Runtime admission requires a contracted writer, reconciler, projection
  writer, observation collector, authoritative-record owner, or application
  coordinator with the matching transaction mode.
- The session must have no active transaction at entry. A violating caller
  transaction is rolled back and rejected with a stable domain error.
- A nested public command, helper commit, helper rollback, or unclosed nested
  transaction cannot produce a successful outcome.
- The executor commits the owned root transaction before returning. It rolls
  back on every failure and does not translate unexpected exceptions.
- The authoritative state change and event-store/outbox record are staged in
  the same transaction.

Adapters continue to create and close sessions. The non-committing
`owner_command_session` lifecycle context is the standard task-side adapter.
Existing auto-committing session contexts and `UnitOfWork` usages are migration
debt; they are not precedent for new or migrated commands.

## Authority boundary

The registered public service owns the business transaction. The database
session adapter owns only opening, defensive cleanup, and closing. Nested
domain helpers may query, add, delete, execute domain SQL, and flush within the
owner's transaction, but cannot complete it. Event delivery remains owned by
`events.dispatcher` after the atomic commit.

The typed manifest represents a canonical nested writer with transaction mode
`participant`. A participant is callable only by contracted owners or
coordinators, locks its own authoritative records, stages its domain event in the
same caller transaction, and never enters the public owner-command executor.
Adapters cannot call participant writers directly.

## Consequences

Transaction ownership is executable and tied to the canonical manifest. A
caller that queries before invoking a command must finish that read and pass a
clean session, or open a dedicated command session. Cross-owner atomicity
requires an explicitly contracted application coordinator rather than nested
public commands.

The strict entry rule makes hidden adapter work visible during migration. It
may require splitting read preparation from command execution or moving the
authoritative read into the owner.

## First migration slice

`network.device_projection` is the first contracted runtime user. Its
reconcile command owns the projection write and event transaction, takes a
PostgreSQL transaction-scoped advisory lock, converges by natural key, prunes
orphans, and emits `device_projection.reconciled` version 1. The Celery task
owns only session lifecycle, derives stable command/idempotency identity from
the task delivery ID, and retries transient database-operational failures with
bounded backoff. Its periodic repair is a permanent scheduler responsibility:
there is no environment, settings, module, or feature control that can freeze
canonical-to-projection convergence. Cadence remains configurable.

## Migration and cutover

- Old paths: service-level direct `commit()` and the task's auto-committing
  session context.
- New paths: typed reconcile command through `execute_owner_command` and a
  non-committing adapter session context.
- Verification: focused transaction behavior, projection behavior, manifest,
  adapter, and architecture tests.
- Cutover gate: no direct transaction completion remains in the migrated owner
  or its task, and projection plus outbox commit/rollback tests pass.
- Fallback retirement: the old direct-commit function signature is removed;
  `network.device_projection` is removed from the legacy manifest baseline.

Other domains migrate one coherent owner slice at a time. Existing legacy
boundaries remain indexed debt until their callers and behavior are verified.

## Verification

- Success is committed and the session is transaction-free before return.
- Operation failures, nested commands, and helper commits roll back all state.
- Active caller transactions and uncontracted owners fail closed.
- The first task cannot call the executor or transaction methods directly.
- The first owner cannot call `commit()`, `rollback()`, or `UnitOfWork`.
- Projection idempotency, freshness, pruning, event, and manifest tests pass.
- Adapters do not hand an unreleased decision-input read transaction into a
  command, and that debt baseline only shrinks.

## Decision-input reads at command entry

Amended 2026-08-09 after a production regression.

Resolving a database-authoritative decision input is a query. `settings_spec`
resolvers read through a 30-second Redis cache, so on a cache hit they never
touch the session and on a miss they open a read transaction on it. An adapter
that resolves settings on the session it is about to hand to a command
therefore fails `active_caller_transaction` only on cache misses, which reads
as intermittent and hides the defect during review and testing.

Argument position is the trap. Python evaluates arguments before the call, so

```python
sweep(session, Command(delay=resolve_integer(session, ...)))
```

opens the read transaction before `sweep` is entered. The read looks like it
belongs to the callee; it does not.

Required shape in an adapter: resolve the inputs, call
`db_session_adapter.release_read_transaction(session)`, then enter the owner
command. That helper fails closed if the session holds pending mutations, so it
cannot be used to discard business writes.

This rule was already implied by *Consequences* above. It was unenforced, so it
regressed. `tests/architecture/test_unreleased_read_handoff.py` now enforces it
as a shrink-only ratchet, with the outstanding migration debt recorded in
`tests/architecture/unreleased_read_handoff_baseline.txt`.

Adapters are identified by registration rather than by directory, following
`test_adapter_identifiability`: every file under `app/api`, `app/tasks`,
`app/web` and the event handlers, plus each `app/services/web_*.py` presenter
the registry does not declare an owner. A presenter therefore leaves this debt
list two ways — by releasing the read, or by being declared an owner when it
genuinely owns its reads.

## External I/O inside a transaction, and the one approved exception

Added 2026-08-09 while repairing this defect class in production.

A database transaction must not stay open across slow or unbounded external
I/O — device SSH, HTTP to a third party, TR-069/ACS, RADIUS. Latency is a
property of the transport, not of the happy path. PostgreSQL closes the
backend at `idle_in_transaction_session_timeout`, and the damage is not a
clean error: the device write already succeeded while the record of it is
lost, so the aggregate status lies and blind retry is unsafe.

The required shape is: project the rows into plain typed values, finish the
read transaction, perform the I/O, then open a fresh short transaction and
persist by the captured identifiers. Where a pass must interleave I/O with
writes, commit at the phase boundaries instead — legitimate only when the
pass is idempotent, so a failure between phases is re-derived by the next run.

Never raise `idle_in_transaction_session_timeout` as containment. It converts
a loud failure into a slow one and hides every remaining site.

Repaired sites: `fetch_olt_running_config` (#1549), `app/tasks/olt_config_backup.py`
(#2211), and the UISP topology sync (#2212).

### Approved exception: the ONT reconcile row lock

`app/services/network/reconcile/sweeper.py` holds the `OntUnit` row lock
across device contact **deliberately**, and must keep doing so.

`eligibility_under_lock` re-reads operator holds and admissions inside that
same transaction, while holding the row lock. The pass-level held set is only
a pre-filter: a hold placed after the pass began is invisible to it. Releasing
the lock before device contact reopens exactly that race — the reconciler
touches a customer device an operator had just decided to protect, which is a
worse failure than the timeout it would fix, and it silently undoes work
someone deliberately built.

So the rule is not "never hold a lock across I/O". It is **never hold one
incidentally**. Where the lock is the enforcement point for a safety property,
holding it is correct and the timeout is the cost of that property; bound the
I/O instead of splitting the transaction.

Drift prevention: this section is the record. A future change that "completes"
the repair programme by splitting the reconcile is a regression, not a fix,
and must be rejected on review unless it first replaces the hold-enforcement
mechanism that the lock currently provides.

## Rollback or forward-fix

Forward-fix the command, manifest, or adapter together. Reintroducing direct
service or adapter commit is not an accepted rollback because it restores the
ambiguous transaction path. The projection has no runtime disable control. If
an emergency worker stop is required to contain a faulty release, roll back or
forward-fix the owner, treat the projection as stale/unavailable rather than
current, and resume its permanent schedule after repair; source device records
remain authoritative.

## Review and retirement

- Review date: after three materially different command owners have migrated.
- Retirement condition: none. Supersede this ADR if a replacement executor
  enforces at least the same manifest, transaction, nesting, and event
  invariants.
- Supersedes or is superseded by: none.
