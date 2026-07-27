# SOT coding standards refactor

Status: historical implementation record; not a current branch or merge plan

Current ownership contracts are generated into `docs/SOT_RELATIONSHIP_MAP.md`.
Current financial-access behavior and operations are specified only in
`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`.

Change classification: major

Owning system: Dotmac Sub

Historical working branch: `feat/sot-coding-standards-refactor-main-sync`

Historical base: `origin/main` at `b3899a0492ae0d61c3ed5571cd834570ffe0f965`

## Baseline at branch creation

- 253 undeclared persistence-writing service modules.
- 334 direct decision-input bypass occurrences: 133 environment reads and 201
  raw setting reads.
- 86 adapter files containing 379 transaction operations or legacy
  transaction-helper calls.
- 212 service files containing FastAPI `HTTPException` references.
- 66 architecture-test modules.

The foundation scanner additionally records 379 transaction operations or
legacy transaction-helper calls across 86 API, web, task, and event-handler
adapter files. The executable registry contains 28 domains and 261 service
entries. Its one exact duplicate concern claim has been split into distinct
observation-evidence and customer-resolution concerns.

Typed manifest migration now has 64 fully contracted services and 212 indexed
legacy services on the reconciled v5.1.0 registry in a shrink-only baseline.
New registry services cannot
be added without role-qualified concerns, authoritative inputs, transaction and
error semantics, migration state, stewardship, checked-in evidence, and any
applicable event or projection/repair contract.

The current-main reconciliation contracts the new material-dependency,
ERP-material-support, and vendor-invoice ERP projection owners without growing
the legacy baseline. ERP payment reconciliation depends on the canonical invoice
record owner; vendor reads consume its timestamped observation. This direction
keeps transport, observation, and business-decision authority explicit and
prevents a dependency cycle through UI presentation services.

The architecture suite now has a measured four-worker development gate:
115 seconds versus 241 seconds serial on the foundation worktree (52% faster).
Using all detected workers took 170 seconds and substantially more CPU, so
unbounded `-n auto` is not the local architecture default. Shared scanners are
cached within a worker; broader cross-module indexing remains a future
optimization if the suite grows materially.

The first runtime transaction slice is `network.device_projection`. The typed
reconcile command is admitted through the manifest-verified owner-command
executor, rejects caller and nested transaction ownership, takes a
transaction-scoped PostgreSQL single-flight lock, atomically stages a versioned
outbox event, and commits or rolls back before returning. Its Celery adapter now
owns only non-committing session lifecycle. ADR 0002 makes this the required
runtime pattern for subsequent migrated write owners.

The next completed runtime slice is `auth.staff_provisioning`. ERP staff create,
managed-role convergence, activation/deactivation, credential state, session
revocation, audit evidence, and outbox events now share one coordinator-owned
transaction. Managed role mutation is delegated to the system-user assignment
owner. Invitation delivery is a replay-safe event consequence, and its password
capability is minted only after the worker revalidates the exact staff principal
at transport time.

The completed reseller onboarding slice is `auth.reseller_onboarding`. Reseller
record creation, optional portal identity and credential bootstrap, legacy
subscriber initialization, reseller link, authorized role grant, audit evidence,
and versioned events now share one coordinator-owned transaction. Both legacy
subscriber-backed and first-class `ResellerUser` principals use a deduplicated
communication intent; the worker revalidates the exact reseller/principal/email
binding and mints the reset capability only in memory at delivery time. The old
multi-commit path, compensating deletion, portal-level principal writer, and
synchronous invite call are retired.

The completed credential recovery slice is `auth.credential_recovery`. Public
email requests, exact-principal administrative requests, capability claims and
lifetime, delivery context, credential replacement, session revocation, audit,
and events now have one owner. A request commits only an email digest and exact
principal identifiers to the event/outbox pipeline; the communication worker
revalidates canonical identity and local-credential state before minting the
bearer in memory and placing it in a URL fragment. Redemption locks the exact
principal and credential, rejects expired or spent capabilities, and commits
the password transition, database-session revocation, PII-safe audit, and
versioned event atomically. API and portal adapters map domain errors, and the
legacy synchronous recovery delivery and service-owned HTTP/commit path are
retired from production callers. The completion event also has one idempotent,
retryable session-projection handler for auth-cache invalidation and customer or
reseller portal-session revocation; a cache outage remains visible as a failed
event-handler attempt instead of silently losing the security consequence.

The completed referral credential-enrollment slice is
`auth.customer_credential_enrollment`. Referral delivery requests and
capability redemption now enter typed owner commands on transaction-free
sessions. The request intent, audit, and versioned event commit atomically and
deduplicate by the exact referral; the delivery worker revalidates the
Referral/Party/Lead/Subscriber/email-digest binding and mints the bearer only in
memory. Redemption locks that canonical context and commits the local
credential, Subscriber email verification, audit, and completion event as one
transition. Password minimum, invite lifetime, and bounded request-rate policy
come only from `control.settings_spec`; event replay owns strict authentication
cache repair. Service commits, savepoints, transport-coded errors, duplicate
intents, and best-effort cache invalidation are retired.

The completed referral account-conversion slice is
`referrals.account_conversion`. Public signup, staff account creation, and
reviewed existing-account attachment now enter typed coordinator commands on
transaction-free sessions. The exact Referral/Party/Lead context and selected
Subscriber are locked and revalidated; account initialization, Party binding,
Lead and Referral attachment, PII-free audit, `subscriber.created`, and
`referral_account.converted` commit or roll back together. Exact replay returns
the attached account without duplicate evidence. The public capability lifetime
resolves only from the bounded `control.settings_spec` input, while its purpose,
version, claim allowlist, token size, and clock-skew rules remain protocol and
security invariants. Service commits, savepoints, transport errors, keyword
mutation entry points, hardcoded lifetime, and adapter-owned post-conversion
transaction completion are retired.

The completed referral-program slice is `referrals.program`. Code issuance,
Party-first capture, exact Referral/Party/Lead account attachment,
qualification, rejection, reward issuance, and reward reconciliation now enter
typed coordinator commands on transaction-free sessions. The owner locks the
canonical records, makes exact replays idempotent, and commits Referral state,
PII-free audit evidence, and versioned events together. Reward accounting is
delegated to `financial.credit_notes`; customer notification is an idempotent
event consequence. Program enablement, reward amount and currency,
qualification window, automatic approval, and share base URL resolve only from
`control.settings_spec`. HTTP errors, adapter transactions, direct CRM credit
calls, direct push delivery, environment reads, and legacy keyword mutation
entry points are retired from the owner boundary.

The completed account-adjustment slice is `financial.account_adjustments`.
Direct debit and reversal confirmations now enter typed owner commands on
transaction-free sessions; prepaid plan changes, add-on purchases, and prepaid
service renewals use explicit typed staging collaborators inside their wider
coordinator transactions. Account and adjustment locks, origin-scoped database
uniqueness, exact replay validation, append-only ledger links, audit evidence,
and PII-free versioned events form one atomic boundary. Omitted currency uses
only `control.settings_spec`'s billing default. Structural evidence
inspection detects drift without inventing monetary provenance, while the
billing alignment audit's zero historical adjustment-debit drift is the
cutover verification. Service-owned HTTP errors, conditional commits and
rollbacks, commit flags, and the ambiguous mutation facade are retired.

The completed event-policy slice is `access.event_policy`. Event handlers and
the RADIUS projection now consume typed group-routing, session-refresh, and FUP
policy outcomes. Canonical defaults live only in `control.settings_spec`;
invalid event action evidence, invalid setting values, and missing or malformed
throttle-profile evidence fail with stable domain errors instead of silently
falling back or skipping enforcement. Invoice-overdue events remain
observations, with every consequence owned by financial dunning.

The completed access-resolution slice is `financial.access_resolution`. The
duplicate `access.control_resolution` registry identity is removed, and
`customer_service_state` retains only outage/support observations. Customer
impact, invoice eligibility, prepaid enforcement, funding, and RADIUS callers
now consume one typed decision implementation. Currency validation raises a
stable domain error; no caller compares amounts under a local currency default.

The completed captive-restriction slice is `access.walled_garden_policy`.
Eligibility, explicit opt-in, network readiness, terminal lifecycle state, and
active-lock precedence now produce one typed reason outcome. Hard reject remains
the deterministic result for missing, invalid, stale, or conflicting evidence;
financial, event, RADIUS, connectivity, and status callers share that policy.

The completed billing-profile slice is `financial.billing_profile`. Account,
collectible-subscription, offer, and requested billing-mode evidence now resolve
through typed source and reason contracts with stable domain failures. Generic
account updates, catalog writes, cleanup remediation, collections, access, and
reporting consume the same resolver or transition policy. Cleanup revalidates
the live profile immediately before applying an account-mode alignment, while
grace policy fails closed instead of selecting a caller-local fallback when the
profile is missing, mixed, or contradictory.

The completed prepaid funding-input slice separates
`financial.prepaid_currency` from `financial.access_resolution`, removing the
hidden threshold-to-access-resolution callback cycle. Access, funding position,
threshold, and enforcement planning now consume one normalized,
fail-closed currency policy. `financial.prepaid_threshold` returns typed minimum
and exact taxed contracted-renewal provenance from the batched owner. The duplicate
service-status derivation is removed, and missing accounts, invalid minimums,
unresolved collectible renewal terms, and cross-currency prices are typed,
fail-closed outcomes instead of guessed or silently ignored inputs. Current
behavior is specified only in `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`.

The completed collections grace slice is `financial.grace_policy`. Account,
reseller, offer/version, policy-set, and billing-mode default precedence now
returns typed policy-set provenance, grace provenance, deadline, and phase.
Default policy identifiers resolve only through `control.settings_spec`; the raw
setting-row bypass is retired. Invalid identifiers and negative or malformed day
values are stable domain failures, never silent zero-day grace that can trigger
an immediate financial access consequence. Naive input timestamps are normalized
to UTC before phase decisions. Retiring the direct setting-row query reduces the
decision-input inventory from 350 to 346 occurrences and from 83 to 82 files.

The completed prepaid planning slice is `financial.prepaid_enforcement`. Cohort,
funding-only eligibility, repair inclusion, policy settings, and each account's
warn/wait/defer/shield/health/suspend/restore outcome now use typed identifiers,
actions, policy issues, and reason provenance. Missing accounts and malformed
time-window or communication policy evidence fail with stable domain errors.
Every calendar day is eligible, the permanent sweep and dry-run consume the
same read-only owner, and execution remains with the lifecycle and access
writers. Current behavior is specified only in
`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`.

The current completed access slice is `auth.system_user_assignments`. It is the
only application writer for system-user role and direct-permission grants. Local
administration and ERP HR now converge only their own grant source, profile edits
cannot erase source-managed access, and active-state changes use the staff owner.
Admin-role removal and deactivation serialize on the canonical admin role row and
fail closed if they would leave no active administrator. Assignment state, audit
evidence, the versioned event, and post-commit cache invalidation share one owner
command boundary.

The completed RBAC catalog slice is `auth.rbac_catalog`. It is the single
application and seed writer for roles, permissions, and role-permission policy.
API and admin adapters now submit typed commands; role form updates and policy
replacement commit atomically with audit and versioned event evidence. Catalog
identities are normalized and protected by database functional uniqueness,
assigned identities fail closed on rename or deactivation, and protected
permissions cannot be granted outside the canonical admin role. The remaining
subscriber authorization boundary is now `auth.subscriber_assignments`, the
single application and seed writer for subscriber role and direct-permission
grants. It enforces active catalog references, explicit global or
region/reseller scope, `rbac:assign` command evidence, atomic audit/event
evidence, and post-commit cache invalidation. The legacy `auth.rbac` module and
reseller/seed parallel writers are retired. The catalog cutover reduces the
undeclared writer-like module baseline from its branch-start value of 270 to
269; the assignment cutover removes one service-layer HTTP-exception module and
reduces that separate baseline from 224 to 223.
The referral account-conversion cutover removes another service-layer
HTTP-exception module and reduces that baseline from 223 to 222.
The referral-program cutover removes the next service-layer HTTP-exception
module and reduces that baseline from 222 to 221. It also retires the owner's
direct environment read and delivery-transport bypass, reducing the
decision-input inventory from 351 to 350 occurrences across 83 files and
removing `referrals.program` from the communication-ledger bypass backlog.
The account-adjustment cutover removes another service-layer HTTP-exception
module and reduces that baseline from 221 to 220; its manifest migration
reduces the indexed legacy-service baseline from 222 to 221.

After reconciling `origin/main` through PR #1487, five additional upstream
ownership slices reduce the current undeclared writer-like baseline from 269 to
264. Those upstream declarations must satisfy this branch's typed manifest and
transport/transaction boundaries before the integration state is green.

The integration-platform and vendor field-review cutovers from PRs #1495 and
#1497 retire six more undeclared writer-like integration modules, reducing the
current baseline from 261 to 255. Their four new integration authorities and two
new staff-review confirmation coordinators are fully contracted rather than
being added to the legacy-service baseline; removal of the retired hook and
webhook-delivery owners reduces that baseline from 213 to 211.

The current-main reconciliation through PRs #1501 and #1502 contracts the new
real-time projection, operational SLA escalation, operational SLA policy-command,
and ticket SLA-clock owners without adding them to the legacy baseline. The
real-time owner is explicitly policy/transport with no database transaction or
durable-state authority. Operational SLA record writes are flush-only
participants; the typed admin policy coordinator owns the root transaction, and
ticket breach evaluation participates in the support lifecycle transaction. The
SSE adapter now releases its read transaction through the canonical database
session boundary instead of owning rollback.

The completed payment-proof slice is `financial.payment_proofs`. Customer,
reseller, API, and admin-web adapters now enter typed submit, verify, and reject
commands on transaction-free sessions and serialize an immutable
`PaymentProofResult`. Review commands lock the exact proof before the credited
subscriber or billing account; proof state, canonical payment or consolidated
settlement, tax-owned WHT source and timeline evidence, reviewer work items, audit rows,
customer communication intents, and versioned outbox events commit or roll back
together. One HTTP adapter owns domain-code mapping for every transport. Direct
service commits, framework exceptions, request-shaped audit calls, nested
savepoints, swallowed consequence failures, and primitive command dictionaries
are retired. The slice moves the manifest baseline from 211 to 210 legacy
services and removes both payment-proof service-layer HTTP exception modules,
reducing that baseline from 214 to 212.

The completed direct-transfer proof-link slice adds
`financial.topup_intents` as the flush-only participant for the exact pending
intent's submitted transition and PaymentProof/configured-bank evidence link.
The payment-proof owner locks the intent before creating the proof and commits
the proof, reviewer work item, proof event, intent transition, evidence link,
and intent event in one root transaction. The customer portal now supplies a
typed intent/bank-evidence command and owns neither status/metadata keys nor a
follow-up commit. Gateway completion, reconciliation, replacement, expiry, and
reseller transitions still use the shared validated status policy. The later
completed slices below close those remaining top-up lifecycle boundaries.

The completed direct-transfer creation slice adds
`financial.direct_transfer_intent_commands` as the typed root coordinator for
customer invoice-payment and account-credit deposit requests. It resolves the
feature gate, configured destinations, amount limits, and intent lifetime from
their named control owners; locks the account and payable invoice before
deriving the invoice balance; and composes flush-only participants for the
canonical intent write. `financial.topup_intents` now owns the direct-transfer
configuration projection plus invoice-intent creation and explicit replacement,
while `financial.account_credit_deposits` stages deposit-intent eligibility and
evidence without completing the caller transaction. Intent creation,
replacement cancellation, and versioned event evidence commit or roll back
together. The portal is a typed adapter, caller amounts cannot override a
locked invoice balance, idempotent replay cannot create a second intent or
event, and the previous random configured-account identifier and local
seven-day lifetime constant are retired. Completion, expiry, gateway creation,
and reseller creation are completed in the following lifecycle slices.

The completed top-up completion/expiry projection slice extends
`financial.topup_intents` as the sole writer of `completed_payment_id`,
`external_id`, `actual_amount`, `completed_at`, and gateway-expired status.
Typed completion commands identify only the intent, canonical succeeded
Payment, and named source; the participant locks subscriber or billing-account
scope, the exact intent, and the Payment, then derives every projected money,
provider-transaction, currency, and time value from payment evidence. Deposit,
webhook, customer verify, reconciliation, and reseller callers no longer assign
those fields. Same-payment replay is a no-op, conflicting evidence fails closed,
and versioned completion/expiry events share the caller transaction. Confirmed
cash remains authoritative and cash-first payment owners may commit it before
this repairable projection, so a projection failure cannot erase received
money. Reconciliation routes typed deposits through
`financial.account_credit_deposits`, while its stale window, max age, and expiry
grace all resolve from `control.settings_spec`; the former local expiry constant
is retired. The broader webhook/reconciliation coordinators remain legacy until
their transaction and transport concerns are migrated.

The reviewed-proof resolution follow-up closes the missing downstream half of
that boundary. `financial.payment_proofs` remains the submit/verify/reject root
transaction owner and supplies exact linked proof evidence to
`financial.topup_intents`. The participant completes verified transfers from
their canonical Payment and cancels rejected transfers with typed provenance,
so proof state, intent state, audit, delivery intents, and events commit or roll
back together. `financial.topup_intent_proof_reconciliation` discovers only
persisted exact proof links and repairs historical submitted drift through the
same participant; verified proofs whose payments are no longer active and
succeeded remain explicitly quarantined. Account-level proof joins, direct SQL
status mutation, and event-handler decision paths are not repair authorities.

The completed gateway creation/failure slice adds
`financial.gateway_topup_intent_commands` as the typed root coordinator for
customer invoice checkout, customer account-credit deposit, reseller
consolidated checkout, and saved-card failure consequences. Invoice amount and
currency are re-derived from the locked invoice; reseller currency and owner
are re-derived from the locked billing account; deposit limits remain owned by
`financial.account_credit_deposits`; and the bounded
`gateway_topup_intent_ttl_minutes` setting is the single gateway-intent lifetime
policy. `financial.topup_intents` is now the sole generic invoice/reseller
gateway record writer and the sole failed-status writer. Customer and reseller
portals retain only provider selection, card-token transport, and response
serialization. A charge decline re-enters a separate owner command that marks
the intent failed and releases an unused saved-card idempotency reservation in
one transaction, so a retry cannot be stranded between two commits. The former
portal constructors, duplicated 30-minute constants, raw intent-metadata
assembly, and split failure/retry-release writes are retired. Creation and
failure events carry versioned scope and command-correlation evidence without
exposing gateway error details.

The completed account-credit deposit settlement slice contracts
`financial.account_credit_deposits` as the single settlement owner. Customer
gateway verification and gateway reconciliation enter one typed root command
on transaction-free sessions; payment-webhook ingestion and payment-proof
review compose the same typed flush-only participant inside their wider owner
transactions. Provider identity, exact intent correlation, amount, currency,
settlement origin, payment memo, audit metadata, and the schema-v1 deposit event
are derived from closed command/source contracts and locked canonical intent
evidence. The legacy intent transaction wrapper, transport-shaped
`PaymentGatewayTransaction` input, caller-selected settlement origin, commit
flag, and caller settlement commits are retired. A forced event-staging failure
proves that payment, credit application, intent projection, audit, and event
evidence roll back together. This slice moves the typed manifest baseline from
51 contracted and 210 legacy services to 52 contracted and 209 legacy services.

The payment-webhook prerequisite slice now gives every settlement collaborator
a named transaction-neutral entry point. Provider-event ingestion composes
flush-only generic payment, consolidated settlement, allocation intent and
confirmation, refund/reversal evidence, payment-status, provider cash, and
allocation-exception participants. Legacy roots retain their commit/replay
wrappers, while participant error paths never roll back a wider transaction.
Verified invoice allocation is isolated through the central owner-command
savepoint API: deterministic failure removes partial allocation writes and
stages one reconciliation exception with the confirmed provider cash, while
direct participant transaction completion remains forbidden. Provider fees
enter the payment/deposit owners as typed evidence and are no longer a webhook-
side mutation. Architecture tests pin these boundaries before
`financial.payment_webhooks` is promoted to its typed coordinator contract; the
manifest baseline therefore remains 52 contracted and 209 legacy services in
this prerequisite slice.

The completed payment-webhook slice contracts `financial.payment_webhooks` as
the typed coordinator for consequences of a claimed signature-verified receipt.
Paystack and Flutterwave adapters now own only signature verification, JSON/HTTP
mapping, durable inbox admission, and submission of the typed receipt identity.
The coordinator locks and normalizes the stored receipt, then composes named
flush-only account-credit, provider-event, payment/allocation, and top-up
participants. Money, allocation, intent, audit/event, and processed-receipt
evidence commit or roll back in one owner transaction. A failed consequence is
followed by a separate `integration.inbox` owner command that records retry or
dead-letter evidence. Direct adapter ORM writes, commits, rollbacks, savepoints,
provider mappings, fee mutation, and synchronous service-restoration/prepaid-
draft fallbacks are retired. Forced processed-receipt failure proves payment,
invoice allocation, provider event, and receipt projection atomicity. This
slice moves the manifest to 53 contracted and 208 legacy services and reduces
the service-layer HTTP-exception baseline from 212 to 211 without growing the
writer, decision-input, or adapter-transaction baselines.

The completed payment-reconciliation slice contracts
`financial.payment_reconciliation` as the application coordinator for stranded
gateway top-ups. The sweep resolves typed stale-window, maximum-age, expiry-
grace, and batch-size policy; selects immutable candidates; and releases its
read transaction before provider verification. Each definitive provider
observation enters an independent manifest-verified consequence transaction
that composes the canonical deposit, provider-event, payment/allocation, and
top-up completion or expiry participants. Provider plus reference reuses the
same event identity as webhook ingestion, so either path resumes rather than
duplicates settlement. The legacy scheduled session wrapper, helper commits and
rollbacks, private provider lookup, guessed invoice allocation, direct payment
decision path, prepaid-draft fallback, and synchronous access restoration are
retired. The provider remains the observation source and
`financial.topup_intents` remains the expiry decision owner, avoiding a
participant-to-coordinator dependency cycle. This slice moves the manifest to
54 contracted and 207 legacy services without growing the debt baselines.

The completed tax-accounting slice contracts `financial.tax_accounting` as the
source-document tax semantics, WHT source-record, lifecycle, timeline, and
read-projection owner. Tax reports and the operator queue now return typed
immutable read models; invalid dates, currencies, filters, and pagination fail
with stable domain errors instead of silently defaulting. Proof-backed WHT
creation is a typed, exact-replay, flush-only participant of the payment-proof
transaction and stages its versioned receivable event once. Staff WHT
transitions enter one manifest-verified owner command, lock the WHT record then
its linked payment, and commit lifecycle state, append-only timeline, ERP-sync
freshness, audit, and a versioned transition event atomically. The admin route
owns only form parsing and safe domain-error presentation; its rollback,
separate audit, public lifecycle initializer, commit flag, primitive report
bags, and duplicate event path are retired. Rollback, replay, evidence,
projection, admin-rendering, PostgreSQL row-lock concurrency, and architecture
tests protect the boundary. On the reconciled v5.1.0 base this moves the
executable manifest from 62 contracted/214 legacy services to 63 contracted/213
legacy services and reduces adapter transaction debt from 373 to 372 operations
across 83 files.

The completed payment-provider-event slice contracts
`financial.payment_provider_events` as the canonical admission and processing
owner for provider observations. Provider configuration remains isolated in
`billing.providers`; the caller-selected trust boolean, helper commit,
source-free event record, transport-shaped mutable input, and return-on-
identity-without-proof paths are retired. The owner persists the explicit
administrative, verified-webhook, or gateway-reconciliation source together
with normalized status, gross, fee, net, currency, provider reference,
processing result, and an exact evidence digest. Administrative commands are
informational only, while verified flush-only participants enforce their named
command scopes. Provider locking serializes concurrent first inserts; exact
replays return once and changed evidence fails closed. Required webhook
currency and invoice-settlement net evidence are never invented, and the
unresolved provider gross/net/fee policy remains explicitly owned by
`financial.payments` and Finance. Atomic audit/event rollback, existing
settlement/refund/reversal behavior, architecture boundaries, and PostgreSQL
concurrency tests protect the cutover. This moves the executable manifest from
63 contracted/213 legacy services to 64 contracted/212 legacy services without
growing the adapter-transaction or service HTTP-coupling debt baselines.

These are burn-down measures, not approved exceptions. Reproducible inventory
and shrink-only enforcement are part of the foundation workstream.

Reproduce the current inventory with:

```bash
python -m scripts.architecture.sot_debt
```

## Objective

Make the checked-in source-of-truth doctrine an executable coding contract.
Every important fact, interpretation, decision, transition, projection, repair,
and external consequence must have one named owner whose boundary is enforced
across web, API, task, event-handler, command, and integration entry points.

This is a controlled ownership refactor. It is not complete when code has only
been moved, renamed, registered, or prevented from creating new debt. Existing
drift and parallel paths must also be repairable and retired.

## Delivery model

The refactor may be developed as small, reviewable domain slices. Each slice
must finish one coherent ownership boundary and pass its focused checks before
being incorporated into this branch.

This branch is the integration boundary for the major change. It must not be
merged to `main`, published as a release, or marked complete until every gate
below is satisfied. Intermediate commits, pushes, pull requests, integration
merges, release labeling, and publication require Michael's explicit request.

## Required workstreams

1. **Repository coding contract**
   - Add repository-local contributor and agent guidance.
   - Establish one transaction, error, typing, command/query, event, migration,
     deletion, and projection/reconciliation standard.
   - Replace checked-in examples that teach conflicting or legacy patterns.
2. **Executable ownership registry**
   - Classify record, resolver, policy, writer, reconciler, projection, and
     transport roles explicitly.
   - Record authoritative inputs, transaction boundaries, event contracts,
     freshness and repair obligations, cutover state, tests, and stewardship.
   - Generate or mechanically validate the relationship map from the registry.
3. **Writer and adapter boundaries**
   - Cover services, web, API, tasks, event handlers, scripts, caches, external
     projections, and raw database paths.
   - Remove direct adapter transactions and parallel decision paths.
   - Retire the undeclared-writer and decision-input debt baselines rather than
     treating them as permanent allowlists.
4. **Domain migration slices**
   - Migrate risk-first, beginning with network, access, provisioning, billing,
     and customer-impact paths.
   - Include locking, idempotency, constraints, drift detection, repair,
     fallback retirement, and architecture tests in each slice.
5. **Contract evolution and traceability**
   - Version event, webhook, and public API contracts.
   - Preserve actor, tenant/scope, command/idempotency, correlation, causation,
     aggregate version, reason, and input provenance where applicable.
6. **Quality and release enforcement**
   - Make architecture, security, secret scanning, import boundaries,
     PostgreSQL integration, migrations, and a ratcheted coverage floor
     blocking release-image dependencies.
   - Keep all exceptions narrow, justified, tested, and shrink-only.

## Definition of complete

The major refactor is complete only when all of the following are true:

- Every persistence writer and business decision path is declared under one
  unambiguous owner; duplicate concern and canonical-writer claims are zero.
- Web, API, Celery, event-handler, CLI, and integration adapters delegate to the
  same command/query owners and do not own business transactions.
- The undeclared-writer and decision-input bypass baselines are empty. Any
  infrastructure exception is represented as an explicit owner, not legacy
  debt.
- Public owner commands have defined transaction, locking, idempotency, error,
  audit, event, and retry semantics.
- Every derived field, cache, summary, mirror, and external projection in scope
  has authoritative inputs, temporal/freshness semantics, a drift signal, and
  an idempotent repair path.
- Migration state is explicit for every moved boundary: old owner, new owner,
  verification or shadow phase where required, cutover evidence, and retired
  fallback.
- Domain services expose domain results/errors; HTTP and task semantics are
  mapped only in adapters. New and migrated owner modules pass strict typing.
- Event and external contracts have an explicit compatibility and versioning
  policy, with replay and consumer tests where applicable.
- Deletion, retention, legal-hold, tombstone, and propagation ownership is
  documented and enforced for affected domains.
- The executable registry, generated/validated relationship map, architecture
  tests, developer guidance, and implementation agree.
- Repository-prescribed formatting, linting, typing, security, unit,
  architecture, PostgreSQL integration, migration, and relevant browser/mobile
  checks are green on the final integrated head.
- The final pull request is labeled `version:major`, documents migration and
  rollback/forward-fix implications, and all required CI checks are actually
  green before Michael authorizes merge.

## Merge prohibition

Until the Definition of complete is satisfied, the branch remains
`active, not merge-ready`. Partial progress must be reported as incomplete and
must not be represented as a finished SOT migration.
