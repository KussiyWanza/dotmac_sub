# ADR 0001: Typed source-of-truth architecture manifest

Status: accepted

Date: 2026-07-19

Representation amended: 2026-08-05

Decision owner: Michael / Dotmac architecture

Affected systems and domains: Dotmac Sub; all registered domains and adapters

## Context

The original `app/services/sot_relationships.py` named domains, service modules,
free-text concerns, and dependency edges in one file. That index caught some
missing or dead owners, but it could not prove what role owned each concern,
which facts were authoritative, where the transaction lived, how events and
errors behaved, how projections were repaired, or whether an authority
migration had actually cut over.

The repository has substantial indexed legacy debt. Requiring complete
contracts for all entries in one mechanical rewrite would create unverified
claims rather than trustworthy architecture.

## Decision

The relationship registry is the canonical architecture manifest. A fully
contracted service declares, with typed values:

- one role and authoritative-input mapping for every exact owned concern;
- the canonical writer for writer roles;
- transaction, locking, idempotency, retry, and stable domain-error behavior;
- versioned event delivery and replay behavior for state writers;
- freshness, stale behavior, drift signal, deterministic rebuild, and repair
  ownership for projections;
- native or explicit old-owner/new-owner migration and cutover state;
- accountable stewardship plus checked-in design and test evidence.

The canonical representation is modular but singular:

- `app/services/sot_manifest.py` owns the typed manifest schema and contract
  validation;
- each service declaration lives exactly once in an ownership-aligned module
  under `app/services/sot_registry/domains/`, with explicit capability shards
  for domains too large to review coherently as one module;
- `app/services/sot_registry/registry.py` explicitly assembles the ordered
  domains and owns global uniqueness, dependency, cycle, and query behavior;
- `app/services/sot_relationships.py` is an identity-preserving compatibility
  facade and contains no declarations.

Dependencies remain one directed cross-domain graph. Domain, capability/module,
and end-to-end journey hierarchies are derived views, not independent
registries. A domain-local relationship list may not duplicate dependency
edges from the aggregate.

Uncontracted entries are temporary indexed legacy debt. Their service names are
recorded in a shrink-only baseline. A new service cannot enter that baseline;
removing a name requires supplying and validating its complete contract.

Generated manifest sections in the relationship map are derived from the
typed registry and checked for exact parity.

## Invariants

- Every contracted concern exactly matches one string in `SOTService.owns`.
- Every concern names one role and only writer roles name a canonical writer.
- Canonical writers name themselves; adapters cannot be canonical writers.
- Every authoritative input names a registered owner, except typed external
  observations whose owner uses `external:<system>`.
- Stateful writers own a transaction, stable domain errors, and a versioned
  event contract.
- Projection writers and reconcilers name drift detection and repair.
- New and migrated owners are fully contracted; the legacy baseline only
  shrinks.
- Every domain name has one explicit declaration module or package, and every
  service is assembled exactly once.
- The compatibility facade re-exports the canonical objects by identity and
  never carries a second declaration.
- No domain-local `sot_relationships.py` may maintain a parallel owner or
  dependency graph.

## Consequences

Architecture review gains a machine-checkable contract and migration ledger.
Declarations become more verbose because authority evidence is explicit.
The baseline permits incremental migration but does not approve existing
architecture debt.

The declaration corpus is navigable by ownership domain and capability without
weakening global validation. Changes in unrelated domains no longer collide in
one 40,000-line source file. Callers retain the old import path during the
compatibility window, while new architecture code imports the canonical
aggregate directly.

Free-text descriptions remain useful context, but they cannot satisfy a typed
contract field or suppress a validation failure.

## Migration and cutover

The 2026-08-05 representation cutover is behavior-preserving:

- Old representation: declarations, assembly, and queries in
  `app/services/sot_relationships.py`, plus an independently maintained network
  relationship list.
- New representation: ownership-aligned declarations and one explicit aggregate
  under `app/services/sot_registry/`; the old path is a thin facade and the
  network-local list is retired.
- Cutover gate: exact equality of ordered domains, services, concerns,
  dependencies, notes, contracts, entrypoints, and rules; zero registry
  validation errors; generated-map parity; and focused architecture tests.
- Backfill: none. This changes code organization only and moves no persisted
  authority or runtime state.
- Fallback retirement: architecture guards prevent declarations from returning
  to the facade and prevent a second `sot_relationships.py` graph.

Owner-contract migration continues under the existing rules:

- Old owner and paths: existing uncontracted `SOTService` entries and their
  callers.
- New owner and paths: the same verified owner, represented by a complete
  `ServiceContract`, or a newly selected owner with an explicit migration.
- Backfill/repair: domain-specific and mandatory where persisted authority or
  projections move.
- Shadow or verification phase: represented by `AuthorityMigrationState` and
  its verification evidence.
- Cutover gate and evidence: required for every non-native migration.
- Fallback retirement: required for every non-native migration.
- Schema contract step: remove the service from the legacy baseline only when
  its typed contract and referenced tests pass.

## Verification

- Generic contract validation tests malformed roles, inputs, transactions,
  errors, events, projections, and migrations.
- Architecture tests reject new legacy entries and require the baseline to
  shrink after a contract is added.
- Design/test paths and generated relationship-map content must exist and match.
- Architecture tests enforce the explicit domain-module inventory, thin facade,
  facade/canonical object identity, and absence of parallel relationship modules.
- Domain behavior tests remain required; manifest validity alone does not prove
  correct implementation.

## Rollback or forward-fix

The schema and checks are additive. A faulty contract or declaration shard is
forward-fixed against the implementation and evidence. Rebuilding the
monolithic declaration file or restoring a domain-local dependency list is not
an accepted rollback because either recreates a parallel authority path.
Re-adding a migrated service to the legacy baseline is likewise not accepted.

## Review and retirement

- Review date: when the legacy manifest baseline reaches zero.
- Retirement condition: none; supersede this ADR if the canonical manifest
  representation changes.
- Supersedes or is superseded by: none.
