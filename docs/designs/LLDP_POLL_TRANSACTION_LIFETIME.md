# LLDP Poll Transaction Lifetime

Owner: `network.lldp_observations` (`app.services.topology.lldp_poller`).

## Cause and Contract

The former `poll_all(Session)` held the implicit inventory read transaction
across serial router calls, then wrote observations through the same session.
Slow binary calls and REST fallback could exceed PostgreSQL's idle transaction
timeout. The task swallowed whole-run errors and returned an error dictionary,
so Celery reported SUCCESS despite failed persistence.

The task now owns two separate session lifetimes:

1. `read_snapshot(LldpReadQuery)` copies inventory, lazy jump-host configuration,
   matching fields, and existing adjacency into frozen dataclasses. Encrypted
   credentials are excluded from repr. Rollback and close precede polling.
2. `poll_all(snapshot=...)` has no Session input or database access. Decryption
   happens only after the read phase. Binary remains first; connection-class
   failures alone permit REST fallback. REST reuses the established connection
   owner's authentication, tunnel, and TLS implementation through an explicit
   one-shot request that cannot resolve database-backed retry settings. It still
   performs one GET attempt with the existing discovery timeouts.
3. `reconcile_poll(ReconcileLldpCommand)` enters `execute_owner_command` once
   on a fresh transaction-free session. PostgreSQL SHARE locks on inventory
   tables and SHARE ROW EXCLUSIVE on adjacency serialize conflicting writers,
   including inserts. A five-second lock timeout bounds acquisition. The owner
   re-reads and compares inventory and adjacency before applying any changes.
   A mismatch fails the whole run, preserving intervening changes and freshness.
   The owner stages `lldp.observations_reconciled` v1 in the outbox with counts
   and correlation data only; delivery is deferred to the durable dispatcher.

The snapshot covers the entire matching inventory and adjacency because fuzzy
matching can change when another device appears, and manual pairs can be
inserted during polling. This is conservative: even an unrelated inventory/link
edit can discard a run. Locks briefly block inventory/topology writers during
persistence only, never during router polling. Load/concurrency performance
requires migration-backed PostgreSQL validation before production acceptance.

## Compatibility and Failure Semantics

Task name, queue, schedule, 300/360-second Celery limits, 240-second collection
budget, selection, binary/REST timeouts, auth-failure handling, normalized/IP/
fuzzy matching, canonical pairs, medium and metadata, and all 15 result keys
remain intact. Routers without a local device are skipped as before. An
individual unreachable router remains a counted partial result, with no retry
loop added. Whole-run exceptions, including soft limits and database failures,
are now raised so Celery records FAILURE. Failure metrics retain an `error`
key and timeout marker. Metrics/cleanup errors cannot mask the original task
exception; failure does not advance observation timestamps.

Pruning uses the recorded `observed_from` router, not an arbitrary successful
endpoint. If legacy metadata lacks an observer, both endpoints must have been
polled successfully. This deliberately avoids pruning a failed observer's link
merely because its peer responded. Manual links are never mutated; upserts
remain source-scoped. Snapshots also reject overlapping runs whose links were
already updated, preventing stale observations from overwriting newer ones.
An explicit timestamp check also rejects observations older than existing LLDP
freshness, including when a newer run finished during the older run's read phase.

The old session-taking API is removed. Its one-off dry-run caller now collects
detached candidates without invoking persistence; printed counts describe
collection, not simulated create/update/prune results. It must not be run on
production without explicit operator authorization.

## Validation and Operations

Focused tests cover transport fallback, matching, metadata, idempotence, manual
links, safe pruning, task phase order, time limits, error propagation, stale
snapshots, rollback, metrics, and registration. Fast SQLite tests are unit
evidence only, not PostgreSQL lock/migration acceptance.

No schema or production configuration change is required. Deploy only through
the repository's staging and immutable-digest release gates after CI and a
disposable migration-backed PostgreSQL rehearsal. In staging, verify:

- Scheduled task registration and ingestion queue routing are unchanged.
- Successful and partial runs retain the existing counters.
- `last_seen_at` advances for observed pairs, and source freshness improves.
- No idle-in-transaction terminations coincide with router polling.
- Injected whole-run failures show Celery FAILURE and cached error metrics.
- Failed/skipped observers and manual links remain active and unchanged.
- Concurrent inventory/link edits reject stale writes; lock waits stay bounded.

Rollback is the standard rollback to the previously accepted application digest;
there is no schema reversal. Rollback restores the previous timeout and false-
SUCCESS defects, so monitor failure metrics and observation freshness. Never
manually prune links to compensate for a failed collection.
