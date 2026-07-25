# Service-extension lifecycle source of truth

Status: active

## Ownership

`financial.service_extensions` is the sole owner of the `ServiceExtension`
aggregate, immutable `ServiceExtensionEntry` evidence, and the billing-anchor
changes caused by an applied extension. Its public create, apply, and cancel
interfaces accept immutable typed commands with `CommandContext` and return
typed outcomes with stable extension, command, correlation, status, count, and
replay evidence.

`observability.audit_log` remains the only audit-event persistence and query
owner. Service extensions stage exact entity-linked audit evidence through that
owner; there is no parallel activity table.

`ui.service_extension_detail_projection` is the read-only owner for the admin
detail page. It composes extension facts, exact audit events, safe actor labels,
status presentation, impact, scope samples, action eligibility, and legacy
provenance. Routes and templates do not query audit rows, resolve actors, or
derive lifecycle meaning.

Access restoration is not service-extension policy. The financial owner asks
`access.subscription_lifecycle` to resolve only billing-related locks, and the
access owner keeps the most-restrictive-wins decision.

## Two-person approval (fraud control)

A service extension grants free service, so applying one is a maker-checker
decision, not a single-actor action. The owner records the creating staff actor
in the immutable `created_by` at creation, and `apply` fails closed with
`self_approval_forbidden` when a **user** approver equals that creator. The
maker holds `billing:extension:create` and the checker holds
`billing:extension:apply`; because the applier must be a different person, at
least two staff are always involved in granting free service. Automated
service/api-key flows (for example the duplicate-reconciliation corrective path,
which is created already-applied and never enters `apply`) are not the
two-person fraud vector and are exempt.

Outage/SLA-data eligibility validation — requiring the extension window to match
a recorded outage and bounding the granted days by the SLA breach — is tracked
as a separate follow-up.

## Transaction boundary

Each create, apply, or cancel command enters `execute_owner_command` once on a
transaction-free adapter session. The owner locks the extension for apply and
cancel, resolves and locks apply targets in stable UUID order, and uses
flush-only helpers. Aggregate state, immutable entries, audit rows, and durable
domain-event rows commit or roll back together.

The lifecycle audits are:

- `billing.service_extension_created`
- `billing.service_extension_applied`
- `billing.service_extension_canceled`

They use `entity_type=service_extension` and the exact extension UUID. Metadata
contains bounded operational evidence: command and correlation IDs, hashed
idempotency evidence, previous/resulting state, days, scope type, and outcome
counts. It excludes emails, phone numbers, pasted identifiers, secrets, and
subscriber lists. The existing per-subscription `billing.service_extended`
events remain distinct customer consequences.

## Idempotency and concurrency

The create form gets a new UUID key on each new render and preserves it across
validation failures. The owner derives the extension primary key
deterministically from that key. PostgreSQL transaction advisory locking
serializes contenders before the deterministic primary-key insert; the primary
key remains the database uniqueness invariant.

The owner stores a SHA-256 fingerprint of normalized material inputs: reason,
outage window, days, scope type, scope ID, and the sorted canonical subscriber
IDs. An exact replay returns the original outcome. Reusing a key with different
material input fails with `idempotency_conflict`.

Apply-after-apply and cancel-after-cancel return the stored stable outcome
without adding entries, audit rows, or events. Apply-after-cancel and
cancel-after-apply fail with `transition_conflict`. The database unique
constraint on `(extension_id, subscription_id)` is the final entry invariant.

## Projection and historical provenance

The activity projection queries only exact `service_extension` entity events
for the exact UUID. It orders newest first by occurrence time and stable event
ID. New records prefer the immutable write-time `actor_label` snapshot.

Historical rows are not backfilled with manufactured audit events:

- missing canonical creation evidence yields one legacy “Created” item from
  `created_by` and `created_at`;
- missing canonical apply evidence yields one legacy “Applied” item only when
  reliable `applied_by` and `applied_at` lifecycle fields exist;
- canonical evidence suppresses its matching fallback;
- cancellation time is never inferred from the current canceled state.

Legacy actor UUIDs are resolved through canonical staff identity. Missing
actors use a safe human label and do not expose the UUID.

## Drift, repair, and cutover

`ServiceExtensionEntry` is immutable extension evidence. Its
`new_next_billing_at` is the minimum defensible anchor produced by that
extension. Drift exists when an applied entry's resulting anchor is later than
the current subscription anchor. A bounded repair must lock the extension,
entry, and subscription cohort; advance only anchors below the immutable target;
never shorten a later legitimate anchor; and stage reviewed repair evidence in
the same owner transaction.

Migration 414 is additive. It adds cancellation, count, and command evidence and
fails before adding entry uniqueness if historical duplicates exist. Operators
must review and reconcile any duplicate evidence rather than choosing a row
automatically. No historical lifecycle audit rows are manufactured.

Cutover is complete only while architecture guards confirm that:

- adapters use the typed command and detail owners;
- the owner contains no internal commit, rollback, or nested transaction;
- templates contain no status or eligibility maps;
- path-based request audits cannot enter the official activity projection; and
- `app.services.service_extensions` is absent from the legacy writer baseline.
