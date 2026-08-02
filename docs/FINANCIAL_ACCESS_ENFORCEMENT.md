# Financial access enforcement

Status: canonical and active

Decision authority: [ADR 0003](adr/0003-permanent-customer-financial-lifecycle.md)

This document is the current contract and operator runbook for postpaid
dunning, prepaid renewal/funding/coverage enforcement, financial restoration,
enforcement locks, RADIUS projection, and their timing. Dated audits and ADRs
are supporting evidence, not runtime instructions.

## Permanent contract

Sub always evaluates the canonical customer-financial lifecycle. Invoice
generation, overdue transition, prepaid renewal, collections, restoration,
notification, and event retry have no runtime enable/disable authority.
Operators may configure safe cadence, notification quiet hours, and the shared
enforcement time-of-day window. They may not select which lifecycle phases
exist.

Every calendar day is eligible. Account-specific facts may produce a typed
no-action outcome for one account while all unaffected accounts continue:

- account billing approval and canonical billing profile;
- reviewed prepaid opening funding and signed quarantine;
- current service coverage and exact contracted renewal terms;
- grace, payment arrangements, payment proofs under review, service extensions,
  outage shields, and dedicated-service policy;
- enforcement-window time and external transport capability.

There is no Splynx, `Subscriber.deposit`, current-catalog-price, invoice-status,
or cached-access fallback. Missing or contradictory evidence is visible and
account-scoped; it is never coerced to zero, paid, funded, or safe-to-suspend.

## Owners and boundaries

| Concern | Owner | Contract |
| --- | --- | --- |
| Postpaid invoices and lifecycle | `financial.invoices` | Owns invoice construction, issue, due, settlement projection, void, and receivable document state. |
| Payments and applications | `financial.payments` | Owns confirmed receipt, allocation, unallocated account credit, refund, and reversal facts. |
| Credit notes | `financial.credit_notes` | Owns customer credit documents and their applications. |
| Append-only customer-position entries | `financial.ledger` | Owns adjustment/reversal evidence; it does not replace invoice or payment owners. |
| Prepaid opening position | `financial.prepaid_funding_reconstruction` | Owns the one-time reviewed baseline and native-after-cutover projection. |
| Customer-facing financial position | `customer.financial_position` | Resolves document/event facts without becoming their writer. |
| Current prepaid coverage | `financial.prepaid_service_coverage` | Classifies exact evidence; writes no money, dates, or access state. |
| Service-extension lifecycle | `financial.service_extensions` | Owns create/apply/cancel, immutable `ServiceExtensionEntry` evidence, and extension-caused billing-anchor changes; requests restoration from the access lifecycle owner. |
| Prepaid renewal charge and execution | `financial.prepaid_service_renewals` | Resolves the exact taxed contract charge and writes the debit, entitlement, anchor, and renewed outcome together. |
| Prepaid threshold | `financial.prepaid_threshold` | Combines due uncovered services with the shared renewal charge and configured reserve. |
| Funding/access eligibility | `financial.access_resolution` | Produces the currency-bound funded/insufficient decision. |
| Prepaid warn/suspend/restore plan | `financial.prepaid_enforcement` | Owns one plan used by dry-run and execution. |
| Postpaid collections policy | `financial.dunning` | Owns overdue AR consequences and financial shields. |
| Financial consequence confirmation | `financial.dunning` access consequence owner | Locks, recomputes, fingerprints, applies, and evidences suspend/restore/throttle/reject consequences. |
| Locks and subscription/account state | `access.subscription_lifecycle` | Sole writer of reason-scoped locks, account status, and child-service access state in one transaction. |
| Network projection | `access.radius_projection` | Owns the exact per-login plan, idempotent external writes, and bidirectional convergence check. |

Routes, jobs, webhooks, event handlers, commands, and notification transports
are adapters. They invoke the owner and map its typed outcome; they do not
reconstruct balances, prices, coverage, grace, shields, or access decisions.

## Financial bases

### Postpaid receivable

Postpaid enforcement uses collectible invoice receivable recomputed from the
invoice total minus active canonical payment and credit-note applications. A
stored `balance_due` is a query accelerator, not independent authority. Draft,
void, inactive, pro-forma, reconciliation-held, wrong-currency, and prepaid
service-consumption documents do not become collectible AR merely because a
status or amount exists.

Payment receipt is not settlement. A payment can remain unallocated account
credit, and a status label cannot replace exact applications. Dunning may
suspend only when the recomputed collectible overdue receivable survives all
account-level shields and grace.

### Prepaid funding

For an account with a reviewed opening position, prepaid available funding is:

```text
reviewed opening position at its cutover timestamp
+ canonical native financial facts whose economic timestamp or Sub created_at
  is later than that timestamp
```

Splynx rows, bank statements, exports, deposit fields, and audit tables may
support one-time evidence review. They are not queried by runtime funding.
Amounts in different currencies are never summed or compared.

An account created after the final cutover starts at zero and accumulates
native facts. A pre-cutover account without a reviewed baseline fails closed.
An old postpaid account changing to prepaid requires a reviewed current
baseline as part of that transition.

## Exact prepaid renewal charge

Renewal and enforcement call the same bounded resolver. For each collectible
monthly prepaid subscription it uses:

1. the positive contracted `Subscription.unit_price` as amount authority;
2. the subscription discount effective at the decision time;
3. service-address, then account, then offer/default tax precedence;
4. the active recurring price row only for currency and cadence metadata; and
5. the canonical tax-application mode.

The result is the exact tax-inclusive charge that renewal would post. For
example, a ₦17,500 exclusive contract at 7.5% VAT requires ₦18,812.50 in both
renewal and enforcement.

The current catalog amount never substitutes for missing contract price. A
missing/nonpositive unit price or missing currency/cadence produces
`renewal_terms_unresolved`. Renewal reports a missing-price outcome and leaves
confirmed money as account credit; enforcement performs no money-based
suspension or restoration for that account.

## Prepaid coverage

`financial.prepaid_service_coverage` accepts these current-period sources:

1. an active `ServiceEntitlement` spanning the decision time and structurally
   linked to the exact renewal debit, paid invoice line, or append-only
   `SubscriptionBillingGrant`; or
2. an applied `ServiceExtensionEntry` spanning the exact
   `[grant_starts_at, grant_ends_at)` interval owned by
   `financial.service_extensions`.

For a service extension, `grant_starts_at` is the later of the existing billing
anchor and application time. Coverage, reconciliation, and enforcement
shielding consume that same immutable interval; `created_at + days` and
`previous_next_billing_at + days` are not parallel coverage clocks. See
`docs/designs/SERVICE_EXTENSION_EFFECTIVE_INTERVALS.md`.

Service-extension lifecycle state, immutable entry evidence, exact entity audit
rows, and durable events are staged atomically. An extension may request
billing-lock restoration, but `access.subscription_lifecycle` remains the sole
restoration-policy and lock writer. Audit request paths and a future
`next_billing_at` without entry evidence are not extension authority. See
`docs/designs/SERVICE_EXTENSION_LIFECYCLE_SOT.md`.

A paid invoice is source evidence, not read-time coverage. The coverage
reconciler must project its exact subscription line and ordered period into an
entitlement. `Subscription.next_billing_at` is also a projection: a future
anchor without exact evidence is `unresolved_projection`, blocks adverse
action, and enters reconciliation.

When an existing prepaid draft is covered by settlement-backed Payments plus a
signed reviewed opening balance, `financial.prepaid_draft_reconciliation` owns
the invoice-first repair. It allocates Payments first, records the remainder as
typed opening-funding consumption (never as a Payment), creates the entitlement,
then projects `next_billing_at` from the entitlement end. Automatic discovery
creates a durable operator exception; it does not silently leave a generic
`draft_invoice_pending` outcome.

Bill Now performs the same service-document guard before creating a recovery
draft. Every unresolved ordinary or recovery invoice with an active positive
line for the exact subscription blocks replacement and routes the operator to
the existing invoice. Financial activity or ambiguous coverage selects manual
review. The guard is subscription-scoped, so another service's invoice does not
become an account-wide block.

That classifier scopes payment-backed and unbacked account credit to native
facts whose economic timestamp or Sub creation time crosses the active reviewed
opening-position boundary. Pre-boundary mirror rows are already represented by
the signed opening amount and are neither reused nor quarantined again.
Post-boundary unbacked credit remains a fail-closed blocker. Accounts without an
active reviewed baseline retain the generic all-history classification.

Generic subscription Restore cannot clear an active prepaid financial lock.
The lifecycle preview reports
`prepaid_financial_reconciliation_required` and the admin UI routes the
operator to the draft-invoice reconciliation queue. Only the financial access
owner may clear the prepaid lock after exact funding and coverage evidence
commits.

Current exact coverage wins over reserve balance. A customer is not suspended
during a funded or explicitly granted service period merely because they do not
hold the next period's reserve. `min_balance` is a top-up target and becomes an
access threshold only when at least one service is due and uncovered.

## Decision ladders

### Prepaid

The planner and executor consume the same decision in this order:

1. Repair obsolete prepaid timers and reason-scoped locks that no longer belong
   to a prepaid or collectible service. This repair consumes no funding.
2. Reject unsafe billing profiles, mixed modes, inactive/canceled accounts, or
   accounts without billing approval with a typed outcome. Billing approval is
   activation admission, not a runtime bypass: revocation disables the account
   and its non-terminal services through `customer.billing_approval` and
   `access.subscription_lifecycle`.
3. Exclude signed-quarantine or missing-baseline accounts from money action.
4. Protect `unresolved_projection` coverage and `renewal_terms_unresolved`
   contract evidence from adverse action. An uncovered service with exact or
   malformed paid-invoice/renewal evidence is an unresolved projection until
   the reviewed coverage reconciler creates or verifies the exact entitlement.
5. Treat current covered/non-billable services as requiring no new funding.
6. For each due uncovered service, sum the shared exact renewal charges. The
   required balance is the greater of that sum and the configured minimum.
7. If fully funded, release eligible prepaid locks and timers. If not funded,
   apply canonical grace, enforcement window, financial/customer-experience
   shields, and dedicated-service policy.
8. Warn, wait, defer, suspend, or report aligned state through the one planner.

A prepaid consequence targets only due uncovered subscriptions. It never locks
covered or unresolved services. Restoration releases a prepaid lock only when
all due service is fundable or the exact locked subscription is currently
covered.

### Postpaid

Postpaid dunning:

1. recomputes collectible overdue receivables from invoice/application facts;
2. applies canonical grace, payment-arrangement/proof/extension/outage shields,
   billing profile, dedicated-service policy, and the shared time window;
3. previews the exact throttle/suspend/reject consequence; and
4. locks and recomputes before confirming it.

Prepaid imported AR never becomes a substitute prepaid enforcement basis.

## Consequence, transaction, and retry contract

Every financial suspend, reject, throttle, or restore records a
`FinancialAccessConsequence` with its locked preview fingerprint, idempotency
key, exact inputs, outcome, and structural links to each lock, credential, and
dunning case affected.

`access.subscription_lifecycle` creates and resolves locks. Locks are
reason-scoped and most-restrictive-wins. Clearing prepaid never clears fraud,
FUP, admin, overdue, customer-hold, or system locks. A stale prepaid lock on a
non-prepaid service is removed without consulting a prepaid balance. A lock on
a terminal service is resolved without activating that service.

`Subscriber.billing_enabled=false` must never coexist indefinitely with active
service. The permanent billing-approval reconciler treats that join as drift.
When every active subscription has an effective complimentary or sponsored
billing treatment it repairs the redundant approval fact to true; otherwise it
disables the account and every non-terminal subscription. Re-approval restores
only when the current administrative disable was created by the billing-
approval owner, so it cannot lift fraud, collections, customer-hold, or
unrelated administrative decisions.

Lifecycle state, lock, audit, and durable event rows commit or roll back
together. The lifecycle owner is the sole writer of the persisted subscriber
status, subscriber portal/account-active state, subscription status, and child
`Subscription.access_state` projection. It derives parent and child state under
the same transaction after flushing lock changes. Delivery begins only after
commit. A rollback therefore cannot send a suspension/restoration event for
state that never existed.

Enforcement handlers attempt the owned local and external consequences. A
failed RADIUS projection, access-state projection, session-cleanup enqueue, or
financial restoration is raised to the durable dispatcher. The handler remains
failed/retryable; it is never logged as successful work. Other independent
event handlers, including receipts and webhooks, remain reachable according to
the declared event execution plan.

The prepaid-renewal event adapter validates that the referenced payment belongs
to the account, succeeded, and has canonical settlement evidence before asking
the renewal owner to act. Missing evidence raises and remains a failed durable
handler attempt. The permanent event redriver retries that exact attempt. A
typed `no_due_service` or consolidated-allocation result is a completed
evaluation rather than an inferred renewal.

## Access tier and RADIUS

Hard reject is the default. Captive/walled-garden access is an explicit
exception for an eligible direct-house residential account with valid portal
network configuration. Business, government, NGO, reseller-owned,
reseller-principal, system, disabled, canceled, and unclassified accounts fail
to hard reject even if a stale opt-in flag exists.

The lifecycle owner persists the requested restriction on each active lock.
`access.walled_garden_policy` derives the most restrictive effective state.
RADIUS population, connectivity reconciliation, session cleanup, portal views,
and audit comparators consume that state; none independently interprets account
or subscription statuses.

Service status and restriction are desired-state inputs. Network and accounting
records are projections or observations:

- `Subscription.access_state` is the local access projection written only by
  `access.subscription_lifecycle`;
- `IPAssignment` is desired address authority and
  `Subscription.ipv4_address`/`ipv6_address` are its served-address projection;
- `last_seen_framed_ipv4`/`last_seen_framed_ipv6` are accounting observations
  and never overwrite the desired address;
- active service retains its credential and address; a reversible restriction
  retains both but projects hard reject or captive access; a terminal service
  removes external authentication and releases its service resources.

The RADIUS writer resolves the desired plan before reading customer
credentials. Hard reject therefore remains buildable when a password is
missing or undecryptable. Active or captive access that cannot be built is
reported as `unbuildable` and its existing external row is preserved; a scoped
repair cannot delete a paying customer's only working row and report success.

## Account and RADIUS convergence

The mandatory access-control loop runs at the configured operational cadence
and cannot be disabled. It performs one sequence:

1. invoke `access.subscription_lifecycle` to repair derived account and child
   access state from canonical service facts;
2. resolve the exact per-login RADIUS plan consumed by the projection writer;
3. compare the normalized radcheck, radreply, and owned radusergroup row set in
   both directions using a secret-safe keyed fingerprint;
4. request one idempotent projection refresh when rows are missing, extra,
   unbuildable, or have exact attribute drift;
5. reconcile live sessions within the configured disconnect-attempt cap; and
6. record an outcome alert until local and external projections converge.

Only one recovery run may execute at a time. Its cap counts attempts, so a
failing transport cannot bypass the safety limit. CoA outcomes preserve the
difference between disconnect confirmed, NAS session-not-found, rejection,
timeout, configuration failure, and transport failure. A radacct row is closed
only when the NAS returns RFC 5176 Error-Cause 503, directly confirming that
the requested session context is absent.

The task is a recovery adapter. Payment, renewal, dunning, administrative, and
service-lifecycle commands still invoke the lifecycle owner in their own
transactions so ordinary access changes do not wait for the periodic pass.
`refresh_radius_from_subs` is an event- and reconciler-requested projection
transport, not a second periodic recovery loop. Scheduler settings, environment
flags, and stale `ScheduledTask` rows cannot register it independently.

## Timing

Enforcement is eligible every calendar day. The only financial enforcement
timing settings are:

- `collections.enforcement_window_start`
- `collections.enforcement_window_end`

The resolver uses `scheduler.timezone`, supports windows that wrap midnight,
and is checked again inside the locked consequence preview/confirmation. A
manual run, retry, or duplicate schedule cannot bypass it. Weekend, holiday,
audit/enforce-mode, prepaid-specific activation, readiness, and health switches
are retired.

Notification quiet hours remain delivery timing policy. Scheduled cadence must
enter the configured window. Permanent customer-financial and access-control
tasks may change cadence or local run time but cannot be disabled, renamed, or
deleted.

## One-time funding reconstruction

The baseline materializer accepts an Ed25519-sealed exact-cohort manifest. The
trusted public key setting must be an OpenBao reference. The signing private key
belongs to the isolated audit environment and must never be copied into Sub,
Git, logs, reports, or durable knowledge.

The manifest binds currency, timestamp, source, account balances, materialized
IDs, quarantined IDs/reasons, semantic/payload/cohort/blocker hashes, signer
fingerprint, approving actor, and a non-secret evidence reference. Missing,
extra, overlapping, duplicate, future-dated, wrong-currency, unsigned, or
changed-cohort rows fail closed. There is no generic blocker override.

Accepted blocker dispositions are narrowly typed:

- `source_evidence_required`
- `canonical_payment_required` with definitive reviewed attribution
- `quarantine`
- `no_paid_through_due_immediately` only for the exact hash-bound, independently
  verified never-paid service reason

Generate/adjudicate in the isolated audit environment, then seal, review,
dry-run, and materialize in one controlled window:

```bash
python scripts/one_off/adjudicate_prepaid_funding_gaps.py \
  --blockers /approved/prepaid-funding-blockers.json \
  --decisions /approved/prepaid-funding-gap-decisions.json \
  --out /approved/prepaid-funding-gap-actions.json

python scripts/one_off/export_prepaid_funding_snapshot.py \
  --snapshot-at REVIEWED_TIMESTAMP \
  --source REVIEWED_SOURCE_LABEL \
  --gap-actions /approved/prepaid-funding-gap-actions.json \
  --out /approved/prepaid-funding-sealed.json \
  --blockers-out /approved/prepaid-funding-blockers.json \
  --allow-quarantined-subset \
  --signing-key-ref bao://secret/audit/prepaid-reconstruction-signer#private_key_pem

python scripts/one_off/materialize_prepaid_funding_reconstruction.py \
  --manifest /approved/prepaid-funding-sealed.json

python scripts/one_off/materialize_prepaid_funding_reconstruction.py \
  --manifest /approved/prepaid-funding-sealed.json \
  --apply \
  --reviewed-sha256 REVIEWED_NORMALIZED_SHA256 \
  --evidence-ref NON_SECRET_FINANCE_REVIEW_REFERENCE \
  --approved-by APPROVING_ACTOR \
  --confirm-final-cutover MATERIALIZE_VERIFIED_PREPAID_FUNDING
```

The materializer recomputes the live cohort. Any drift between seal and apply
requires a fresh export, review, and signature. After materialization, later
correction uses an append-only reviewed supersession. It never restores Splynx
or deposit as runtime authority.

## Coverage and lock reconciliation

`financial.prepaid_service_coverage_reconciliation` previews each subscription
as already covered, exactly repairable, legitimately due/uncovered, or
quarantined. Confirmation requires the exact `as_of`, SHA-256 fingerprint,
idempotency key, operator, and reviewed reason. It locks and recomputes, creates
only the missing entitlement, and never posts money or infers a period from
memo text.

```bash
python scripts/billing/prepaid_coverage_reconcile.py --as-of <ISO-8601>
python scripts/billing/prepaid_coverage_reconcile.py \
  --apply --as-of <same-ISO-8601> --fingerprint <sha256> \
  --idempotency-key <stable-key> --actor <operator> \
  --reason "<reviewed evidence reason>"
```

Preview prepaid-lock cleanup from active lock evidence, not subscriber status,
invoice status, or paid-through date:

```bash
python -m scripts.one_off.unwall_paid_accounts --prepaid-locks-only
python -m scripts.one_off.unwall_paid_accounts \
  --prepaid-locks-only --apply --limit 1
python -m scripts.one_off.unwall_paid_accounts --prepaid-locks-only --apply
```

The runtime sweep also repairs obsolete prepaid locks/timers for non-prepaid and
service-less accounts. It resolves only prepaid state, preserves unrelated
locks, and never creates a funding baseline merely to perform cleanup.

## Archive boundary

`payment_prepaid_applications_archive` is historical provenance, not runtime
coverage or funding authority. Migrations 394, 396, and 397 fail closed on
missing, duplicate, or malformed legacy/archive tables and verify row count,
columns, types, nullability, defaults, keys, constraints, and indexes. The
archive has no application writer. Finance operations owns retention, and its
deletion requires a separate reviewed decision.

## Continuous operations

### IPAM assignment-lifecycle migration

`IPAssignment` remains the desired-address authority; a subscription's IPv4
column and RADIUS rows are projections and cannot manufacture, move, or reclaim
an allocation. The shadowing `network.ip_assignment_lifecycle` owner audits all
active IPv4 assignments and retains the safe missing-`subscription_id`
backfill. It also previews reviewed exact-service create, link, deactivate, and
terminal-release repairs without changing the served column, RADIUS, or
sessions.

Begin with a read-only preview:

```bash
python -m scripts.one_off.repair_ipam_to_served
```

Apply only the reviewed cohort using the exact printed repair fingerprint:

```bash
python -m scripts.one_off.repair_ipam_to_served \
  --apply --limit 25 --fingerprint REVIEWED_REPAIR_SHA256 \
  --idempotency-key STABLE_OPERATION_KEY --actor APPROVING_OPERATOR \
  --reason "Reviewed exact service-ownership backfill evidence"
```

The limit is part of the fingerprinted cohort. Re-run the same limited preview
immediately before apply. Any changed assignment, subscription, or served-IP
compatibility evidence fails closed and requires a new review. This migration
does not itself cut RADIUS or runtime readers over to exact-service ownership.

For an explicitly adjudicated service-level ledger repair, preview first:

```bash
python -m scripts.one_off.repair_service_ipv4_assignment \
  --subscription-id SUBSCRIPTION_UUID \
  --desired-address-id IPV4_ADDRESS_UUID \
  --deactivate-assignment-id STALE_ASSIGNMENT_UUID
```

Apply only the identical current preview with its exact fingerprint,
idempotency key, actor, and reason. Use `--release` instead of
`--desired-address-id` only for an exact canceled or expired service. The
complete command, safety policy, migration boundary, and projection cutover are
defined in `docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md`.

### Daily review

1. Review billing-health, funding, coverage, renewal, dunning, notification,
   durable-event, lock, access-state, and RADIUS projection observations.
2. Investigate typed blockers by account. Route corrections through the owning
   payment, invoice, credit, subscription, reconstruction, coverage, lifecycle,
   or provider service.
3. Run read-only previews before historical repair. Apply only the reviewed,
   fingerprinted cohort through the owner command.
4. Process exact cases in small canary batches. For mixed-source prepaid drafts,
   verify the opening baseline, approval evidence, and prior consumption before
   confirmation.
5. Verify money facts, opening consumption, entitlement, subscription anchor,
   lock, billing event, RADIUS state, receipt, and customer-visible outcome
   after every batch.
6. Keep unresolved evidence quarantined. Never fabricate zero funding, a paid
   period, or a restoration/suspension decision.

### Continuous acceptance signals

- future `next_billing_at` without exact current coverage;
- active prepaid lock on a covered or non-prepaid subscription;
- overlapping/duplicate entitlements;
- renewal debit without exactly one entitlement;
- entitlement/anchor mismatch;
- reusable fully paid prepaid service value;
- due uncovered service with missing contract terms;
- repairable/quarantined coverage evidence by stable reason;
- failed/retrying financial-access event handlers;
- local access state or exact RADIUS row projection not converging to the
  lifecycle owner;
- active/captive RADIUS logins whose credentials cannot be built;
- recovery-loop overlap, exhausted disconnect-attempt cap, or nonterminal CoA
  outcomes;
- accounting observations that differ from the desired assigned address.

### Release and incident handling

Before deployment, run the prescribed format, lint, type, architecture,
billing/payment/renewal/collections/access/event, integration, migration-head,
and image health checks. Review every data-bearing migration precondition.

For a faulty release, roll back the deployment or ship a focused forward fix.
For incorrect account facts, correct only the evidenced cohort through its
named owner. Do not recreate retired lifecycle switches, use direct SQL to edit
money/locks/entitlements, or restore a legacy fallback as containment.

Retain non-secret preview fingerprints, manifest hashes, approvals, idempotency
keys, evidence references, repair-run IDs, and post-state verification. Never
store customer credentials, provider secrets, bank narration, raw identity
exports, or secret values in these records.

## Code and verification index

- `app/services/customer_financial_ledger.py`
- `app/services/prepaid_funding_reconstruction.py`
- `app/services/prepaid_service_coverage.py`
- `app/services/prepaid_service_renewals.py`
- `app/services/prepaid_threshold.py`
- `app/services/access_resolution.py`
- `app/services/prepaid_enforcement_planner.py`
- `app/services/collections/prepaid_balance_sweep.py`
- `app/services/collections/_core.py`
- `app/services/account_lifecycle.py`
- `app/services/account_status_reconcile.py`
- `app/services/radius_access_state.py`
- `app/services/radius_population.py`
- `app/services/radius_projection_planner.py`
- `app/services/radius.py`
- `app/services/enforcement.py`
- `app/tasks/radius.py`
- `app/services/enforcement_window.py`
- `app/services/events/dispatcher.py`
- `app/services/events/handlers/enforcement.py`
- `tests/test_prepaid_funding_reconstruction.py`
- `tests/test_prepaid_service_coverage.py`
- `tests/test_prepaid_coverage_reconciliation.py`
- `tests/test_prepaid_service_renewals.py`
- `tests/test_prepaid_threshold_resolver.py`
- `tests/test_prepaid_enforcement_planner.py`
- `tests/test_prepaid_balance_sweep.py`
- `tests/test_financial_access_restore.py`
- `tests/test_account_lifecycle.py`
- `tests/test_events_enforcement_services.py`
- `tests/test_radius_shadow_handler_integration.py`
