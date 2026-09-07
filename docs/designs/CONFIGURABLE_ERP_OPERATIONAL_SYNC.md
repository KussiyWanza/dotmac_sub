# Configurable ERP operational sync

Self-Care owns projects, project tasks, tickets, and service work orders. DotMac
ERP receives read-only operational projections for finance and resource context.
ERP never becomes a second workflow writer for those source records.

The `dotmac.erp` installation exposes one
`erp.operational_context.sync.v1` binding. Its reviewed scope contains an
allowlist of enabled domains (`projects`, `project_tasks`, `tickets`, and
`work_orders`); its policy fixes the destination to
`/api/v1/sync/sub/bulk` and bounds batch size. Administrators configure all
profiles from `/admin/integrations/erp`. They cannot enter arbitrary URLs.
The configured ERP service credential must carry ERP scope `sub:domain:write`;
otherwise ERP answers 403 and Sub refuses to enable the operational binding.

Each domain advances an independent `(updated_at, id)` watermark only after ERP
accepts the complete bulk request without errors. Replays are safe because ERP
upserts source identities within the authenticated organization. Project tasks
require the project profile and are sent after projects and tickets.

ERP responds with `contract_version: 2` plus separate project, ticket,
project-task, and work-order counts. The typed client rejects a missing or older
response before any watermark advances. Item errors return the neutral
`source_reference` for the Sub source UUID. No predecessor-system alias is
accepted. Deploy ERP and its migration first, then enable the Sub capability.

The dedicated scheduler is enabled only while the capability binding is
enabled. The same page provides a manual run action. Disabling every profile
disables only this capability, leaving inventory, material support, payables,
attendance, and other ERP capabilities untouched.

## ERP projection and expense use

ERP upserts every entity by authenticated organization, entity type, and the
Self-Care UUID. Tasks are stored as ERP project tasks and retain their project,
optional parent-task, and optional ticket links. Imported records populate the
Project, Ticket, and Task choices on both the employee expense-claim form and
the Finance expense form. ERP remains the owner of the expense records; it does
not become the owner of the source project or ticket lifecycle.

## Verification

- `tests/test_dotmac_erp_domain_sync.py` verifies typed payload construction,
  watermarks, no-op sweeps, and error behavior in Self-Care.
- The ERP repository's
  `tests/integration/test_sub_operational_sync_v2.py` sends the exact wire shape
  through the real ERP API route and service on migration-built PostgreSQL. It
  verifies all three requested entities, their links, both expense form data
  sources, and idempotent replay. The success response is produced by ERP, not
  invented by a Self-Care mock.

## Limitations

- The incremental feed projects status changes but does not delete an ERP copy
  when a source row disappears.
- Parent tasks must be processed before children; the sender replays missing
  ancestors to guarantee this ordering.
- A batch with any ERP item error is retried from the same Self-Care watermarks.
  ERP may already hold successful items, so its source-ID upsert must remain
  idempotent.


## Durable retry admission and safe diagnostics

The operational-context owner also owns the singleton erp_operational_sync_state
row, seeded by migration 579. Before cursor selection every scheduled or manual
run locks this row with FOR UPDATE SKIP LOCKED. A contender returns already_running
without transport. The transaction retains the lock through response validation,
failure recording or watermark advancement. No expiring lease permits a second
sender while the first is still running. HTTP transient retries remain bounded
at at most two retries with a twenty-second per-attempt timeout for this feed
(other ERP calls cap retries at three); worker crashes release the database lock.
HTTP 429 returns immediately to the durable owner instead of sleeping inside the
HTTP transport while retaining a database transaction.

A permanent rejection, item error, invalid payload/response or configuration
failure commits sanitized failure evidence and next_attempt_at six hours ahead.
Transient failures back off from five minutes to one hour between scheduled
attempts, respecting bounded Retry-After evidence. Beat still checks every five
minutes; retry_not_due does not send another request. A changed binding, immutable
configuration revision, manifest, domain scope or policy fingerprint permits an
earlier attempt and resets the failure budget. Remote-only corrections are picked
up by the next six-hour probe. The existing manual run obeys the same admission
and never bypasses it. A successful no-op or accepted batch clears blocking state.

Failure evidence contains bounded allowlisted codes/messages, HTTP status when
available, operation name/ID, unique correlation ID and HTTP request ID. Unknown
provider messages/codes, HTML, validation input and response bodies are omitted.
Use the request ID to obtain redacted receiving-side evidence. A code not on the
allowlist requires explicit review before it can be preserved; regex filtering is
not sufficient to prove a provider string contains no private information.

Each admitted failure stages erp.operational_context.retry_deferred in the same
transaction as its durable evidence; delivery is after commit through the outbox.
The task reports its typed outcome and next retry in the job heartbeat projection.
Blocked/retryable/completed-without-delivery runs never stamp a successful ERP
heartbeat. The durable table remains authoritative if Redis is unavailable.

## Deployment and controlled recovery

1. Validate source changes and migration 579 on a non-deployment development host
   or GitHub CI, including fresh and real predecessor migration rehearsals and
   PostgreSQL contention tests. Missing PostgreSQL or private dependencies are
   failed validation prerequisites, not permission to substitute SQLite acceptance.
2. Follow feature branch -> main -> origin/main exact-commit validation, rolling
   version metadata and a single candidate build. Freeze main merges while the
   selected candidate is in flight. Deploy its immutable digest to the explicitly
   named staging host, obtain staging acceptance, then authorize and deploy that
   same digest to the explicitly named production host. Do not attach latest to
   an unaccepted candidate. This source PR does not authorize deployment.
3. Apply migration 579 before starting new workers. Its new table is additive;
   no source rows, cursors, mirrors or history are changed. Stop old ERP workers
   before new admission-controlled workers send: old versions do not take this
   lock. The migration uses a five-second lock and thirty-second statement budget.
4. Inspect the durable status, next_attempt_at, fingerprint and safe diagnostic.
   Obtain the real ERP response status/code, the receiving implementation version,
   scope names and organization/source mappings. No ERP payload or permission
   change is justified from a generic rejection alone.
5. BEFORE resuming production, verify ERP deduplication on the deployed receiver:
   bulk does not send the runtime idempotency key. Replay safety depends on
   organization/entity/Selfcare-source-ID upserts, including partial acceptance.
   If this cannot be proved, pause only the operational capability through its
   administrative owner. Do not change cursors or replay production work.
6. After an approved correction, let the due probe run, or apply a reviewed new
   configuration revision through the integration owner and use the existing
   manual run. Neither path clears pending work or overrides concurrency admission.
   Observe accepted entity counts, zero item errors, advancing watermarks, falling
   eligible-source age, and no duplicate ERP entities before normal catch-up.
7. For rollback, first pause the ERP operational capability and stop new workers.
   Keep the additive retry table and evidence; never downgrade by deleting it.
   An older worker ignores the gate, so do not re-enable it without equivalent
   admission protection. Keep quote-transport retirement when rolling back ERP changes.

This feed preserves pending source state after unchanged watermarks. It does not
capture every intermediate source version, and a count of failed sweeps is not a
count of distinct affected entities. No local mocked response establishes live
ERP compatibility or production duplicate protection.
