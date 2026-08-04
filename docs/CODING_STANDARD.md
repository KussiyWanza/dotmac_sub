# Dotmac Sub coding standard

Status: normative target contract

Owner: Dotmac Sub architecture

This standard turns the source-of-truth relationship map into implementation
rules. Existing code that violates a rule is migration debt, not precedent.
New code must comply immediately; migrated domains must remove their debt in
the same coherent ownership slice.

Run `python -m scripts.architecture.sot_debt` to reproduce the current writer,
decision-input, adapter-transaction, registry, and HTTP-coupling debt
inventory. Baselines reported by that command are shrink-only migration
ledgers, not exceptions to this standard.

## 1. Authority and layers

Every important concern has exactly one owner for each applicable role:

| Question | Owner role |
| --- | --- |
| What happened? | authoritative record or ledger |
| What does current state mean? | resolver or read service |
| May state change? | policy or transition service |
| Who performs the change? | canonical command writer |
| What follows from the change? | event and consequence policy |
| How are copies repaired? | reconciler |
| How is it delivered? | transport adapter |

The normal direction is:

```text
observation or command
  -> input adapter
  -> owning command/query
  -> authoritative transition
  -> transactional domain event
  -> consequence policy
  -> projection/reconciliation
  -> external transport
```

Dependencies must point in that direction. A route, task, webhook, event
handler, template, mobile client, cache, or external system cannot introduce a
second interpretation or writer.

### Executable architecture manifest

`app/services/sot_relationships.py` is the canonical architecture manifest.
New and migrated owners use the typed contracts in
`app/services/sot_manifest.py` to declare:

- the role and authoritative inputs of every exact owned concern;
- the canonical writer for writer concerns;
- transaction, locking, idempotency, retry, and domain-error contracts;
- event version, delivery, compatibility, and replay behavior for writers;
- projection freshness, stale behavior, drift, rebuild, and repair ownership;
- native or explicit old-owner/new-owner migration and cutover state;
- steward, design evidence, and enforcing tests.

Uncontracted registry entries are shrink-only migration debt. Never add a new
service to `tests/architecture/sot_manifest_legacy_baseline.txt`. Generated
manifest rows in `docs/SOT_RELATIONSHIP_MAP.md` must exactly match the registry;
verify them with `python -m scripts.architecture.sot_manifest_docs`.

## 2. Commands, queries, and coordinators

### Commands

A public command:

- has a name describing one business intent;
- accepts a typed, validated command object rather than a free-form mapping;
- validates authorization scope and current authoritative facts;
- defines its transaction, locking, idempotency, audit, event, and retry rules;
- returns a typed outcome containing stable identifiers and decision evidence;
- raises typed domain errors without importing FastAPI, Celery, Jinja, or a
  delivery SDK.

New and migrated write commands enter
`app.services.owner_commands.execute_owner_command` exactly once. Their typed
business command carries `CommandContext`, and their static
`OwnerCommandDefinition` names one exact writer or coordinator concern in the
typed manifest. Adapters and nested helpers never call this executor.

Commands do not expose a generic CRUD update for decision-bearing state. State
transitions are explicit operations such as `suspend_subscription`,
`confirm_payment`, or `assign_work_order`.

### Queries

A public query:

- names the read or projection owner;
- accepts typed filters, scope, sorting, and pagination;
- returns a typed read model rather than leaking mutable ORM entities across
  the adapter boundary when a stable contract is required;
- includes provenance, reason, and freshness for derived or operational state;
- does not commit or produce business side effects.

### Cross-domain coordination

When one use case spans owners, a registered application coordinator owns the
coordination contract. It calls domain owners; it does not duplicate their
validation or write their records directly. The registry must name the
coordinator and its dependencies.

## 3. Transaction ownership

Session lifecycle and business transaction ownership are separate:

1. The adapter creates and closes the database session.
2. The registered public command owner begins and completes the atomic business
   transaction before returning its outcome.
3. Nested domain helpers use the provided session, call `flush()` when needed,
   and never call `commit()` or `rollback()` independently.
4. A coordinator may isolate an explicitly optional participant consequence
   only through `app.services.owner_commands.execute_owner_savepoint`. The
   callback stays flush-only; the coordinator handles its domain failure and
   records the resulting reconciliation or failure evidence. Direct participant
   savepoints and nested savepoints are forbidden.
5. Routes, API handlers, tasks, event handlers, and CLI commands never call ORM
   mutation or transaction methods for business state.
6. A cross-domain coordinator may own one transaction only when its SOT
   contract explicitly names the coordinated invariant.

The standard executor rejects an active caller transaction, nested public
command, helper commit or rollback, direct or unclosed savepoint, uncontracted
owner, or manifest role/mode mismatch. Its savepoint API is the sole nested
transaction-completion authority inside an active owner command. It commits
before returning success and rolls back before propagating failure. Use
`db_session_adapter.owner_command_session()` in task adapters that need only
session lifecycle. The auto-committing session context and `UnitOfWork` are
legacy migration paths; do not introduce them in a new or migrated command.

Do not catch an exception merely to roll back and re-raise it at multiple
layers. The transaction owner performs rollback. Adapters map the resulting
domain error after the session is safe to reuse or close.

Infrastructure session helpers may close or release a read transaction, but
they must not become business writers and must be registered as infrastructure
owners when they perform persistence.

A contracted canonical writer may use manifest transaction mode `participant`
only when it is a non-public collaborator of named command/coordinator owners.
Participant writers lock their own records, stage their state and event evidence,
and use `flush()` only. They cannot be called directly by routes, tasks, webhooks,
or CLI adapters, and they cannot initiate or complete a transaction. This mode
records nested ownership explicitly; it is not a compatibility path for an
adapter-owned transaction.

## 4. Concurrency and idempotency

Every retryable or externally triggered command defines:

- idempotency scope and key source;
- a fingerprint of the material command inputs;
- replay behavior and the stable replayed outcome;
- conflict behavior when a key is reused with different inputs;
- lock target and lock order;
- database constraints that arbitrate concurrent winners;
- retry classification for serialization, deadlock, timeout, and provider
  failures.

Check-then-write without a lock or constraint is not concurrency control.
In-memory locks do not protect multiple workers or hosts.

## 5. Errors and outcomes

Domain errors have stable machine codes, safe messages, and structured details.
They do not contain HTTP status codes, redirects, templates, Celery retry calls,
or provider response objects.

Adapters map domain errors consistently:

- web/API adapters map them to HTTP or form responses;
- task adapters map them to retry, reject, or dead-letter behavior;
- CLI adapters map them to exit codes and redacted operator output;
- integration adapters map provider errors into local domain or transport
  outcomes.

Unexpected exceptions remain unexpected: log them once with correlation data,
roll back at the transaction owner, and do not convert them into a misleading
validation error.

## 6. Models and updates

- Use SQLAlchemy 2 typed mappings for new and migrated models.
- Prefer explicit constructors and explicit field assignment from validated
  commands.
- Never pass untrusted request dictionaries directly into ORM constructors.
- Never loop over arbitrary keys with `setattr()` for decision-bearing models.
- Enforce invariants with database constraints as well as service validation.
- Use UTC-aware timestamps. Name temporal meaning explicitly: `observed_at`,
  `effective_at`, `recorded_at`, `resolved_at`, or `reconciled_at`.
- Unknown, stale, unavailable, disabled, and not-applicable are distinct states.

## 7. Events and side effects

Authoritative state and its domain event are written in the same transaction.
The durable event store/outbox delivers consequences only after commit.

Event contracts define:

- event type and schema version;
- event, command/idempotency, correlation, and causation identifiers;
- actor and tenant/scope;
- aggregate type, identifier, and version;
- occurrence and recording times;
- typed payload and data-classification rules;
- compatibility, replay, retention, and consumer behavior.

Handlers are consequence adapters. They must be idempotent and may not reinterpret
the transition or become an alternative writer for the source aggregate.

## 8. Projections and reconciliation

Every derived field, cache, counter, summary, mirror, search index, external
projection, and materialized status declares:

- authoritative inputs and provenance;
- canonical projection writer;
- observation/effective/recorded time semantics;
- freshness target and stale behavior;
- deterministic rebuild operation;
- drift query or metric;
- idempotent repair owner;
- alert and escalation owner.

Callers update source state or request reconciliation. They do not maintain the
source and its copies in parallel.

## 9. Control planes and configuration

Environment values, settings rows, feature flags, module toggles, scheduler
controls, RBAC, and integration capabilities are decision inputs and therefore
have owners. Application modules consume the effective value from the owning
resolver; they do not independently merge environment, database, and legacy
fallback values.

Migration controls require an owner, default, scope, effective-value
provenance, cutover gate, and retirement condition. Retired flags and aliases
are removed rather than preserved indefinitely.

## 10. External systems

Payment gateways, CRM, ERP, RADIUS, network controllers, monitoring stores,
WhatsApp, email, and collaboration systems provide observations or delivery.
They are not authoritative for local business state unless a checked-in
contract explicitly says otherwise.

External identifiers include provider, account/tenant scope, provenance, and
lifecycle. They cannot be the only copy of a local identity or decision.
Outbound delivery uses an idempotent outbox or intent record and preserves the
local decision identifier.

Inbound observations cross one named admission boundary. That boundary must:

- label the durable trust source, such as administrative input, verified
  webhook, or provider reconciliation;
- persist normalized decision inputs and an exact decision-evidence fingerprint
  before requesting consequences;
- reject identity reuse when the admission trust class or normalized decision
  evidence changes. Two independently verified transports may converge on one
  canonical result only when their normalized decision evidence is identical;
- require the command scope associated with that trust source;
- keep administrative or otherwise unverified input from invoking a verified
  participant or changing financial state; and
- fail closed when a required amount, fee, net value, currency, provider
  reference, or status was not observed. It must not insert a business default
  merely to make an external payload complete.

Raw provider payloads are diagnostic evidence, not a parallel decision API or
part of a financial consequence fingerprint. Consequences consume the
persisted normalized fields owned by the admission service.

Bearer capabilities, reset links, one-time codes, and other delivery secrets
are ephemeral transport material. Durable events, intents, notifications, and
delivery outcomes persist only an allowlisted action plus non-secret canonical
context. The worker revalidates that context, mints and renders the secret
immediately before transport, keeps URL bearers in fragments where the client
supports them, and never writes rendered secret content back to an outbox row.

## 11. Migrations and cutovers

Authority migrations use these stages:

1. inventory old readers, writers, decisions, jobs, and projections;
2. add the new owner and additive schema;
3. backfill through a reviewed, bounded, idempotent operation;
4. verify constraints, counts, outcomes, and drift;
5. shadow-compare when the risk requires it;
6. cut reads and writes to the new owner;
7. reconcile existing drift;
8. remove old writers, fallbacks, flags, tasks, and imports;
9. contract obsolete schema only after rollback requirements expire;
10. add architecture tests that prevent reintroduction.

Every migration states lock and statement-time budgets, retry behavior,
downgrade or forward-fix policy, data-volume assumptions, and operator evidence.
Production execution requires a named target and a reviewed runbook.

## 12. Deletion, retention, and privacy

Deletion is a state transition. The owning contract defines retention, legal
hold, tombstone, audit evidence, external propagation, projection removal, and
rebuild exclusion. Hard deletion must not make authoritative history or repair
impossible unless the approved retention policy requires it.

Secrets never enter tracked files, logs, events, reports, prompts, or durable
memory. Store only an OpenBao path or approved local pointer.

## 13. Typing, imports, and module shape

- Typed contracts are a release gate for every implementation change, including
  fixes, migrations, jobs, scripts, routes, tasks, webhooks, integrations, and
  tests. A small diff, an internal caller, or a temporary workflow is not an
  exception.
- Any cross-component boundary touched by a change must leave that change with
  an immutable typed input, typed outcome or error, precise domain identifiers,
  and typed provenance. Mocks and tests construct those same contracts instead
  of bypassing them with primitive bags.
- Existing untyped boundaries are migrated when touched. A deviation requires
  an explicitly approved architecture decision naming the owner, rationale,
  drift prevention, compatibility/cutover plan, and retirement condition.
- New and migrated owner modules use strict typing.
- Use the actual domain identifier type, such as `UUID`, inside owner contracts;
  transport strings are parsed once at the adapter boundary.
- Represent closed actions, phases, states, policy issues, reason sources, and
  provenance with enums or explicit value objects instead of duplicated magic
  strings.
- Public decisions use typed immutable outcomes. Make optionality explicit and
  type collections and mappings precisely; `dict[str, Any]`, `list[Any]`,
  `set[Any]`, and free-form primitive bags are not owner interfaces.
- Derived, defaulted, imported, reconstructed, or policy-selected values include
  typed source provenance when callers must distinguish how the value was
  obtained.
- Keep UUIDs, enums, decimals, dates, and value objects typed through the domain.
  Convert them to scalar persistence, transport, hash, or report shapes
  explicitly at that boundary.
- Normalize unavoidable dynamic or third-party input in one narrow adapter. Do
  not propagate `Any`, add permissive fallbacks, or use casts merely to silence
  the type checker.
- Missing or malformed authoritative input raises a stable, transport-neutral
  domain error with a code and structured context. Generic `ValueError` is not
  a public service contract.
- Domain modules do not import FastAPI, Celery, templates, or delivery SDKs.
- Adapters may import owners; owners do not import adapters.
- Cross-domain imports pass through registered public contracts.
- Split modules along ownership boundaries when their size obscures transaction
  or decision ownership. File size alone is not the boundary; authority is.
- Temporary ignores and dependency exceptions are narrow, justified,
  shrink-only, and have a retirement condition.

## 14. Observability and security

State-changing commands record actor, scope, reason, target, command or
operation identifier, result, and relevant evidence without logging secrets or
unnecessary PII. Logs are structured and use bounded-cardinality fields.

Security, permission, audit, secret, dependency, and migration checks are
blocking. A non-blocking scanner is advisory and cannot satisfy a required
security gate.

## 15. Tests and Definition of Done

Alembic is the source of truth for the deployed database schema. A test may
claim integration, migration, database-constraint, concurrency, transaction,
or production-parity evidence only when it runs on PostgreSQL/PostGIS whose
schema was created by the real checked-in migration chain and verified at the
exact candidate head. `Base.metadata.create_all()`, SQLite substitutes, and
hand-created tables are useful only for explicitly non-authoritative fast unit
tests; they cannot satisfy deployed-schema acceptance.

The canonical integration command migrates one explicitly disposable test
database once per environment before pytest starts. Tests then isolate their
state with transactions/savepoints. Replaying the complete chain before every
test is unnecessary, while silently accepting an unmigrated target is
forbidden. Integration fixtures fail rather than skip when PostgreSQL or the
expected Alembic head is absent. Reports name the engine, migration starting
point and ending head instead of saying only "PostgreSQL".

Fresh-baseline and incremental upgrade tests answer different questions. When
schema behavior changes, retain the fresh deployable-baseline-to-head gate and
also exercise the real predecessor-to-head boundary. A squashed bootstrap that
constructs current model metadata cannot substitute for the latter.

Each ownership slice includes:

- command/query contract tests;
- transition and invariant tests;
- concurrency and idempotency tests;
- authorization and tenant/scope tests;
- event/outbox and retry tests;
- projection drift and repair tests;
- adapter delegation tests;
- architecture tests for forbidden writers/imports;
- migration and compatibility tests when schema or contracts change.

During development, run the smallest focused test set that proves the changed
boundary. Before accepting a slice, run `make test-architecture`, which uses the
measured four-worker architecture-test default. Use
`make test-architecture-serial` only to isolate worker/order failures or to
confirm an ordering-sensitive diagnosis; parallel success does not replace the
other required unit, integration, migration, security, or browser/mobile gates.

Full-suite owners run only on a non-deployment development host or in
GitHub-hosted CI. A host identified by the exact staging markers may run no
more than ten explicitly named Python test files, serially, for diagnosis. A
production host runs no pytest command. Unknown or conflicting deployment
markers fail closed. The pytest and Make adapters enforce this boundary before
application import, test collection, or integration-database preparation.

A slice is complete only when the new owner is live, existing drift is
repairable, old authority is retired, the executable registry and documentation
match implementation, and the applicable validation suite is green.

## 16. Deviations

A deviation requires an accepted architecture decision record under
`docs/adr/`. It names the alternative owner, affected scope, rationale,
security and operational consequences, migration/cutover implications, drift
prevention, review date, and retirement condition. Silence or legacy behavior
is not an accepted deviation.
