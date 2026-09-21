# Sub runtime-readiness inventory

Characterization only. Sub pins Kernel `0.1.0a94` (`pyproject.toml:52,80,116,371`)
and composes selective contracts and module storage, but **deliberately keeps
its own database/session authority** — `app/db.py`'s `get_engine()` /
`SessionLocal` / `install_session_hooks()`. That is selective adoption, not a
runtime cutover, and this document does not change it: no pinned version
moves, nothing switches to `dotmac_kernel.session_runtime.DatabaseRuntime`, no
writer retires.

Its purpose is narrower and more mechanical: name every local engine/session
construction site (so the count cannot silently drift — see "Ratchet" below),
and state precisely what semantics a shared `DatabaseRuntime` would have to
preserve if Sub ever adopted it. All facts below are MEASURED by reading the
cited file:line, not inferred.

## Capability comparison against `DatabaseRuntime` (measured on the Kernel
runtime-composition seam branch, per the orchestrator's own read of
`session_runtime.py`; not independently re-verified in this pass — see
"What this document does not re-derive" at the bottom)

| # | Capability | What Sub does today | Evidence | Expressible by `DatabaseRuntime` today? |
|---|---|---|---|---|
| 1 | Tenant GUC | Sets exactly one PostgreSQL transaction-local setting, `app.current_tenant`, to a single fixed operator-tenant UUID, via `SELECT set_config('app.current_tenant', :tenant_id, true)` (the `true` third argument is `set_config`'s `is_local` flag — PostgreSQL's parameterizable equivalent of `SET LOCAL`, so the value is discarded on commit or rollback and cannot leak to the next borrower of a pooled connection). Installed by a **global** `event.listens_for(Session, "after_begin")` listener that fires on every ROOT transaction of every ORM `Session` in the process (guarded by `if transaction.parent is not None: return`, so nested/savepoint sessions do not re-fire it) — re-arming on every new transaction, which is exactly the "no reset needed because nothing outlives the transaction" pattern `DatabaseRuntime.tenant_scope`'s own `after_begin` re-arming implements. There is no per-request tenant *resolution*: Sub is single-operator (ADR-0009), so the value is a compile-time constant, not a lookup. | `app/services/operator_tenant.py:64-89` (`apply_operator_tenant_transaction_scope`, `OPERATOR_TENANT_ID` at `:39`); wired at `app/services/session_hooks.py:116-124` (`_apply_operator_tenant_scope`, imported and called at `app/db.py:73,75` — `install_session_hooks()` is itself a no-op body at `app/services/session_hooks.py:34-36`; the real installation is the `@event.listens_for` decorators firing at import time, not the function call) | **Yes.** The setting name (`app.current_tenant`) is byte-identical to `DatabaseRuntime.CANONICAL_TENANT_SETTING`, so Sub needs no `legacy_tenant_settings` entry at all — a `tenant_lookup` that always returns the one operator tenant (or a direct `set_tenant(OPERATOR_TENANT_ID)` call once per checkout) reproduces this exactly. |
| 2a | Transaction isolation levels | Two named, reusable transaction modes layered onto an EXISTING session via `Session.connection(execution_options=...)` (not `SET TRANSACTION`, which must be the first statement in a transaction and never can be here — the tenant-scope hook above always runs one first; both docstrings state this explicitly as the reason `execution_options` is used instead): `REPEATABLE READ` + `postgresql_readonly=True` for reports (`READ_ONLY_SNAPSHOT_OPTIONS`), and `SERIALIZABLE` + `postgresql_readonly=False` for a writer that must not silently interleave (`SERIALIZABLE_WRITE_OPTIONS`). Both are live in production callers, not dead code. | Definitions: `app/db.py:124-143`. Appliers: `begin_serializable_write` `app/db.py:146-159`, `begin_read_only_snapshot` `app/db.py:161-173`, `read_only_snapshot_session` (context manager) `app/db.py:176-202`. Real callers: `begin_serializable_write` used at `app/services/party_identity_backfill.py:82`; `begin_read_only_snapshot` used at `app/services/migration_source_export.py:184`. | **No.** Zero occurrences of `isolation_level`/`execution_options`/`SERIALIZABLE`/a read-only session helper in `session_runtime.py` (measured by the orchestrator). This is a **named blocker**: a caller that needs a `SERIALIZABLE, READ WRITE` or `REPEATABLE READ, READ ONLY` transaction on a `DatabaseRuntime`-produced session has no seam to ask for one. |
| 2b | Statement / lock / idle-in-transaction timeouts — engine-wide defaults | Set once, on connection, as libpq `options` startup parameters (`-c statement_timeout=... -c lock_timeout=... -c idle_in_transaction_session_timeout=...`), applied to every connection the pool opens. Defaults: statement 120000ms, lock 10000ms, idle-in-transaction 60000ms — each an overridable env var. | `app/db.py:44-58` (`get_engine`); values `app/config.py:68,69,79-81` (`DB_STATEMENT_TIMEOUT_MS`, `DB_LOCK_TIMEOUT_MS`, `DB_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS`) | **No.** `DatabaseRuntime.from_urls`/`__init__` take no `connect_args`/timeout parameters (measured: zero `statement_timeout`/`execution_options` in the module). A caller could still pass raw `connect_args` if `DatabaseRuntime.__init__` accepted an `Engine` it did not build itself (`__init__` takes `engine: Engine` directly per its signature at `session_runtime.py:137-143`) — so this specific gap is narrower than 2a: a product could build its OWN engine with these `connect_args` and hand it to `DatabaseRuntime(engine=...)`. Still a named gap: there is no documented, product-independent way to express "this runtime's default connections carry these Postgres options." |
| 2c | Statement / lock timeout — ad hoc, PER-TRANSACTION overrides | Multiple call sites narrow or widen the engine default for one transaction only, via `SET LOCAL <setting> = '<value>'` executed as a plain statement (not `execution_options`) immediately after the transaction begins — every one is Postgres-only and no-ops on other dialects. Four distinct sites: (a) advisory-lock helper narrows `statement_timeout` to bound lock acquisition; (b) session-adapter's advisory-lock path does the same; (c) LLDP poller sets `lock_timeout = '5s'`; (d) an audit-settings refresh narrows `statement_timeout`; (e) a network reconcile widens `idle_in_transaction_session_timeout` well ABOVE the engine default via `set_config(..., true)` because a `SELECT FOR UPDATE`-held row must survive a slow OLT/TR-069 round trip that would otherwise exceed the engine-wide 60s idle cap. | (a) `app/tasks/_postgres_lock.py:38`; (b) `app/services/db_session_adapter.py:185`; (c) `app/services/topology/lldp_poller.py:658`; (d) `app/main.py:796`; (e) `app/services/network/reconcile/core.py:103-133` (`_widen_idle_in_transaction_timeout`) | **No.** Same gap as 2a/2b — no `execution_options`/timeout seam at all. These are all executed as ordinary SQL against whatever session/connection is handed to them, so they are mechanically portable to a `DatabaseRuntime`-produced `Session` (nothing here calls a `DatabaseRuntime`-specific API) — but the runtime itself provides no first-class way to express "this transaction gets a different timeout," so every one of these five sites would keep working only by continuing to issue raw SQL, never through the runtime's contract. |
| 3a | Session hooks — GLOBAL, class-scoped (survive any runtime swap) | Multiple `@event.listens_for(Session, ...)` / `event.listen(Session, ...)` registrations target the **base** `sqlalchemy.orm.Session` class, not a specific `sessionmaker` or engine. SQLAlchemy's event system fires class-scoped listeners for every `Session` instance in the process regardless of which engine or `sessionmaker` produced it. Confirmed by exclusion: the external/RADIUS accounting engines (`app/services/usage.py:_radacct_engine`, `app/services/external_radius_targets.py:get_external_engine`) are used only via raw `engine.connect()`/`engine.begin()` `Connection`s, never wrapped in an ORM `Session` — so these hooks never fire against them today, and preserving that separation matters as much as preserving the hooks themselves. Distinct hooks: tenant-scope install (item 1); root-transaction span timing/slow-transaction logging; deferred after-commit callback dispatch (`run_after_commit`); and a transaction-ownership guard that raises if any code other than the registered public command boundary tries to `commit()` a session mid-command (`_reject_helper_commit`) plus its companion rollback bookkeeping. | Tenant scope + span timing + after-commit dispatch: `app/services/session_hooks.py:82-193` (all `@event.listens_for(Session, ...)`). Command-boundary commit guard: `app/services/owner_commands.py:242-254` (`before_commit`) and `:260-266` (`after_soft_rollback`). Domain-level invalidation hooks (unrelated to session lifecycle, but same class-scoped mechanism): `app/models/domain_settings.py:520-521` (`before_flush`/`after_commit`). | **Yes, without any runtime change**, because these are registered on the base ORM class, not on anything `DatabaseRuntime` owns. They would keep firing unmodified even if `SessionLocal` were replaced by a `DatabaseRuntime`-produced `sessionmaker`, as long as the runtime still produces real `sqlalchemy.orm.Session` instances (it does). This corrects a possible over-read of "no first-class session-hook API" as a blocker — for Sub's actual hooks, it is not one. |
| 3b | Session/engine hooks — SCOPED to one engine (need the runtime to expose it) | One hook is engine-specific: a `handle_error` listener that fingerprints and logs schema-drift / idle-in-transaction errors without ever logging SQL text or parameters, installed once per engine object (idempotency guarded by a marker attribute on the engine itself, not a module-level flag) and invoked from inside `get_engine()`. | `app/services/db_error_observability.py:114-117` (`install_db_error_observability`); called from `app/db.py:60-62` inside `get_engine()` (`def get_engine():` starts at `:41`) | **Mechanically yes, ergonomically no.** `DatabaseRuntime` exposes a public `.engine` property (`session_runtime.py:258`), so `install_db_error_observability(runtime.engine)` would work verbatim — but there is no first-class `DatabaseRuntime` method for "attach an engine-level hook," so a caller must know to reach past the class into the exposed `Engine` object itself. Not a blocker; a real ergonomics/encapsulation gap worth naming for the seam's design. |
| 4 | Fork safety | Sub runs Celery in its **default prefork** pool (no `--pool` override found anywhere in compose/deploy) and FastAPI as a single `uvicorn` process per container (no `--workers`, no gunicorn anywhere in the repo — `app/config.py:34-38`'s "4 API workers plus 10 Celery prefork children" comment describes separate CONTAINER instances scaled horizontally, not `fork()` inside one process). Only Celery forks. On `worker_process_init`, the module-level `_engine` object's pooled connections are explicitly disposed (`_engine.dispose()`) so a prefork child creates fresh connections rather than reusing file descriptors inherited from the parent. There is no PID-keyed engine/session factory anywhere — one process-wide `_engine`/`SessionLocal` pair, disposed (not rebuilt) after fork. | Celery prefork hook: `app/celery_app.py:298-303` (`_dispose_inherited_db_connections`, `@worker_process_init.connect`). Engine/dispose definitions: `app/db.py:41-76` (`get_engine`, module-level `_engine` at `:65`, `dispose_engine` at `:78-80`). FastAPI process model: `Dockerfile:53`, `docker-compose.yml:72` (bare `uvicorn ... --host 0.0.0.0 --port 8001`, no worker flag). | **Named blocker, but only if the seam is meant to be reused across a fork boundary as-is.** `DatabaseRuntime` has zero occurrences of `register_at_fork`/`getpid`/a post-fork `dispose(close=False)` contract (measured by the orchestrator). Sub's own pattern is a plain module-level singleton plus an explicit `dispose_engine()` call wired into `worker_process_init` — this is fully reproducible against a `DatabaseRuntime` instance TODAY by calling `runtime.engine.dispose()` from the same Celery signal, since `.engine` is exposed; the gap is that `DatabaseRuntime` does not do this itself or document the contract, so every adopting product must independently remember to wire its own prefork hook, exactly as Sub already does for its private engine. This is a "no worse than today" situation, not a strictly new blocker — but it is not `DatabaseRuntime` doing anything Sub doesn't already have to do by hand. |
| — | Async | Sub uses zero async SQLAlchemy (`create_async_engine`/`AsyncSession`/`async_sessionmaker`) anywhere in the repository (measured: repo-wide grep, zero matches). | (absence, whole repo) | N/A — nothing to preserve. `DatabaseRuntime`'s own absence of async support (measured by the orchestrator) is therefore not a blocker for Sub specifically, whatever it means for other adopters. |

## What this document does not re-derive

The `DatabaseRuntime` capability facts in the right-hand column above (async
absent, fork-safety absent, transaction-mode/timeout API absent, no
first-class session-hook API) are stated as given by the orchestrator's own
measurement of `session_runtime.py` on the Kernel runtime-composition seam
branch, per its correction mid-task. This document independently verified the
LEFT-HAND side (what Sub does) against Sub's own source at the commit this
worktree was created from, and cross-checked the two named exceptions above
(3b's `.engine` property, 4's `.engine.dispose()` reachability) directly
against the same `session_runtime.py` copy visible at
`/private/tmp/kernel_runtime_seam/packages/dotmac-kernel/src/dotmac_kernel/session_runtime.py:258,262,266,270`
in this environment. It did not re-run a full line-by-line audit of that
file end to end.

## Named blockers, ranked by severity for a Sub adoption

1. **Transaction isolation levels and per-transaction timeout overrides (2a,
   2c) are the real blockers.** Sub has two live, production-load-bearing
   named transaction modes (`SERIALIZABLE, READ WRITE` and
   `REPEATABLE READ, READ ONLY`) and five ad hoc timeout overrides, none of
   which have any seam on `DatabaseRuntime`. A caller can still issue the
   same raw SQL against a `DatabaseRuntime`-produced `Session` — nothing
   observed here calls a `SessionLocal`-specific API — so this is not an
   outright impossibility, but it means `DatabaseRuntime` currently
   contributes NOTHING to this half of Sub's transaction discipline; every
   one of these sites would keep depending on hand-written SQL rather than a
   shared, tested contract.
2. **Engine-wide connection options (2b) are expressible only if the product
   builds its own `Engine`** and hands it to `DatabaseRuntime(engine=...)`
   — workable, but means the runtime's own `from_urls` convenience
   constructor cannot reproduce Sub's timeout defaults; a Sub adoption would
   have to bypass `from_urls` entirely.
3. **Fork safety (4) is a documentation/contract gap, not a code gap**, for
   Sub specifically: Sub's own pattern (module singleton + explicit
   `dispose()` on `worker_process_init`) transfers unchanged to
   `runtime.engine.dispose()`. It would become a real blocker only if a
   future Sub deployment relied on `DatabaseRuntime` constructing/disposing
   engines itself across a fork it does not control.
4. **Session hooks (3a, 3b) are not a blocker for Sub.** The class-scoped
   hooks need no runtime cooperation at all; the one engine-scoped hook is
   reachable through the already-exposed `.engine` property.
5. **Async is not a gap for Sub** — Sub uses none.
6. **Tenant GUC (1) is fully expressible today**, and is in fact the ONE
   item on this list that is a straightforward match: same setting name,
   same `SET LOCAL`-equivalent semantics, same re-arm-on-`after_begin`
   pattern, single fixed tenant with no resolver needed.

## Local engine/session construction inventory

Every place that builds a `sqlalchemy.Engine` or an ORM `Session` outside
`app.db`'s canonical `SessionLocal` factory (`app/db.py:69`), swept by AST
(not text search — see the ratchet's sensitivity proofs) across the whole
repository. Full raw counts:

| Family | Root(s) | Files | Construction sites |
|---|---|---|---:|
| Application services, tasks, main | `app/` | 10 | 11 |
| Alembic migration runner | `alembic/` | 1 | 1 |
| Standalone operational/migration scripts — live | `scripts/` | 4 | 5 |
| Standalone operational/migration scripts — historical residue (see below; excluded, not a `DatabaseRuntime` requirement) | `scripts/` | 7 | 8 |
| Test fixtures, migration rehearsals, integration/playwright harnesses | `tests/` | 65 | 119 |

The mechanical AST sweep itself found 22 production/operational files (25
sites) with no interpretation applied. Seven of those files are historical
residue, addressed in its own section below, clearly separated from the live
requirements this document exists to characterize — never mixed into the
table or list that follows.

### Production/operational sites (app/, scripts/, alembic/) — 15 files, 17 sites, LIVE ONLY (see "Historical / residual" below for the seven excluded)

- `app/db.py` (2) — the canonical factory itself: `create_engine` (`:51`)
  and `sessionmaker` (`:69`).
- `app/services/usage.py` (1) — `_radacct_engine` (`:85`): a **separate data
  plane**, the external FreeRADIUS `radacct` accounting database, cached
  per-URL with `NullPool`. Not Sub's own database; explicitly documented as
  guarding against a prior connection-pool leak (`:71-76`).
- `app/services/external_radius_targets.py` (1) — `get_external_engine`
  (`:62`): another external-database engine (the configured FreeRADIUS
  target), pool-recycled and pre-pinged, locked by a module-level
  `threading.Lock`.
- `app/tasks/_postgres_lock.py` (1) — `Session(bind=conn, ...)` (`:33`):
  reaches into `SessionLocal.kw["bind"]` to get a raw `Connection` pinned
  for the life of a PostgreSQL session-level advisory lock (locks are
  backend-connection-scoped, so the `Session` must not release its
  connection mid-lock the way a plain `SessionLocal()` session would across
  commits).
- `app/services/db_session_adapter.py` (1) — the same pinned-connection
  advisory-lock pattern (`:176-177`), as part of the `DbSessionProvider`
  Protocol this file also defines (`:16-37`) — the closest thing in Sub
  today to an abstracted session-lifecycle seam, already wrapping
  `SessionLocal` behind `session()`/`read_session()`/`owner_command_session()`/
  `advisory_lock()` methods.
- `app/services/auth_flow.py` (1), `app/services/financial_imports.py` (1),
  `app/services/events/dispatcher.py` (1) — the recurring
  "`Session(bind=db.connection(), join_transaction_mode="create_savepoint")`"
  pattern: a legacy/compatibility helper runs inside the CALLER's existing
  transaction via a SQLAlchemy savepoint, so it can commit/rollback
  independently without ending the parent transaction.
- `app/services/events/handlers/owner_session.py` (1) — a transaction-free
  sibling of the same pattern (`Session(bind=db.get_bind())`, no savepoint
  join), for owner commands that must NOT run inside the dispatcher's
  already-open transaction.
- `app/services/session_hooks.py` (1) — `Session(bind=bind, ...)` (`:95`),
  constructed inside the `after_commit` hook itself, to run deferred
  callbacks on a fresh session after the triggering transaction has
  already closed.
- `alembic/env.py` (1) — `engine_from_config` (`:153`), `NullPool`, its own
  `lock_timeout` (`:135-147`), no ORM `Session` at all — Alembic owns the
  deployed schema and is not part of application runtime authority.
- `scripts/*` (4 live files, 5 sites) — one-off migration/backfill/import
  scripts (ledger effective-date backfill, price-offer inventory, CI
  test-database bootstrap, a committed-rate shadow-diff one-off) that each
  open their own short-lived engine against an explicit target URL, never
  against `app.db`'s pooled engine — correct for a one-shot script, but
  each is a real construction site a shared runtime's contract would need
  to keep out of scope for.

### Historical / residual — not a `DatabaseRuntime` requirement

Seven of the 22 mechanically-swept production files are one-time
data-migration and preflight scripts written for a subscriber/ticketing
system integration that has since been fully decommissioned. Each
requires a second, external database URL naming a system that is no
longer reachable — they are retirement residue, not part of Sub's live
runtime surface, and this document does not treat them as anything a
shared `DatabaseRuntime` must preserve.

They are deliberately not named or path-cited in this document (or in
`session_construction_baseline.txt`): the repository already carries an
authoritative, frozen ledger of exactly which files belong to that
decommissioned integration's surface, and duplicating that list here would
only create a second, unmaintained copy of it. The exact exclusion
mechanism — which cross-references that existing ledger rather than
hand-listing paths, so it stays correct automatically as the ledger
shrinks — is `tests/architecture/session_construction_inventory.py`'s
`production_counts_by_file`; that module is where the specific paths are
legitimately named, once, alongside the ledger itself.

Whether any of these seven scripts remains literally executable today (the
Python module still imports and parses arguments, independent of whether
its target database is reachable) is a Sub repository-hygiene /
residue-deletion question for that decommissioning programme to close —
not a Kernel capability requirement, and out of scope for this inventory.

### Test/fixture family — 65 files, 119 sites (tracked as an aggregate, not
per-file — see the ratchet's rationale below)

Dominated by: migration rehearsal harnesses that build a real Postgres
engine per migration boundary (`tests/integration/test_kernel_lineage_rehearsal.py`,
8 sites), SQLite-per-test unit migration tests (~40 files, 1-2 sites each),
and Postgres integration concurrency/composition tests. None of these
construct a session against Sub's application `SessionLocal`; they are each
a private, disposable database/engine per test.

## Ratchet

`tests/architecture/session_construction_inventory.py` (AST-based scanner,
not text search) + `tests/architecture/session_construction_baseline.txt`
(checked-in per-file counts for `app/`, `scripts/`, `alembic/`) +
`tests/architecture/test_session_construction_ratchet.py` implement a
two-directional ratchet, mirroring the existing
`test_adapter_keyword_service_calls.py` shape:

- `test_no_new_local_session_construction_in_production_surfaces` fails if a
  file not in the baseline gains a construction site, or a baseline file's
  count grows.
- `test_session_construction_baseline_has_no_stale_entries` fails if a
  baseline count is now HIGHER than reality — i.e. a site was removed
  without deliberately lowering the baseline, so the debt figure cannot
  drift down silently either.
- `test_test_fixture_engine_family_is_swept` holds `tests/` to the same
  two-directional discipline at the AGGREGATE level (currently 115) rather
  than per-file, because test fixtures legitimately construct one ad hoc
  engine per test and per-file tracking there would churn with test
  authorship, not with runtime readiness.
- Six sensitivity-proof tests plant a real defect (a rogue `create_engine`
  call, a rogue bound `Session(...)` call) and confirm the scanner names it
  by line, then prove three near-misses are NOT flagged: prose merely
  mentioning `sessionmaker`/`create_engine` in a docstring or comment, a
  domain model unrelated to SQLAlchemy sessions named `class Session(Base)`
  (the exact real shape at `app/models/auth.py:367`), and a bare
  `Session(...)` call whose `Session` name was never actually imported from
  `sqlalchemy*`.

- `test_historical_residue_is_excluded_from_the_live_ratchet_but_still_swept`
  proves the residue-exclusion mechanism (see "Historical / residual"
  above) actually removes exactly the seven known files from the LIVE
  production baseline while the raw, unfiltered sweep still finds all 22 —
  a plant, not an assumption: if the exclusion silently stopped matching
  (e.g. because the referenced ledger changed shape), this test fails
  loudly rather than letting residue quietly reappear as an unaccounted
  "new file" or, worse, letting the exclusion silently swallow a live file
  that happens to share the same historical surface.

Both the AST scanner and baseline were independently re-derived and cross-
checked against the live repository before being committed (see this
document's construction-site tables above); the counts are exact as of this
worktree's base commit, not estimated.
