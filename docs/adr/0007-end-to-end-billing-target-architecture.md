# ADR 0007: End-to-end billing target architecture

Status: accepted

Date: 2026-07-27

Accepted: 2026-07-27 by Michael

Renumbered from a working draft numbered 0004. That number was already taken by
`0004-automated-outage-notification-dispatch.md`, as were 0005 and 0006.

Decision owner: Michael / Dotmac architecture

Affected systems and domains: sales, catalog and subscription contracts,
billing cadence and rating, invoices, payments, customer financial position,
prepaid coverage, collections, access lifecycle, customer statements, Dotmac
ERP integration, events, durable timers, and financial operations

## Acceptance status

This ADR is accepted as the target architecture. Acceptance authorizes the
phased implementation below and makes the target normative for new work; it does
not by itself move any authority.

Current checked-in owner contracts remain authoritative for each concern until
that concern's migration phase passes its explicit cutover gate. A phase that is
implemented but has not passed its gate is shadow evidence, not authority. The
per-concern migration state is observable in
`app/services/sot_registry/registry.py` through each owner's `MigrationContract`.

## Architecture goal

Every accepted commercial commitment, and every later billable service change,
becomes exactly one versioned billing contract and a structurally linked set of
obligations. Each obligation is deterministically rated, documented, settled or
explicitly resolved, posted to the operational customer subledger, reflected in
typed per-currency financial position, and connected to the required service,
collections, access, customer-statement, and ERP consequences through durable
owner outputs.

The same contract, period, rating, posting, and delivery primitives support
composable cadence and both prepaid and postpaid collection timing. They do not
collapse prepaid and postpaid into one accounting state machine.

The end-to-end path contains no authoritative metadata join, mutable balance
counter, parallel money formula, business-wide enforcement sweep, cross-owner
drift-repair service, or adapter-owned financial decision.

## Context

The current system contains strong domain owners but no single end-to-end
billing contract:

- `SalesOrder`, installation invoice, subscription invoice, and originating
  payment are not joined through one structural obligation identity. Some
  Sale-to-Money joins still depend on metadata or external string identifiers.
- Billing mode is persisted on account, subscription, and catalog records.
  `financial.billing_profile` detects disagreement at runtime instead of one
  contract owning the effective term.
- Postpaid period generation supports daily, weekly, monthly, quarterly, and
  annual cycles, while prepaid renewal remains materially monthly-specific.
- `financial.ledger` owns individual ledger-entry and reversal lifecycle, while
  customer financial position is assembled from payment, allocation, invoice,
  prepaid-consumption, write-off, credit-note, ledger, and reviewed-opening
  records. Account credit has a separate ledger-entry formula.
- Postpaid dunning and prepaid enforcement have different account scans,
  timers, notices, commits, and error handling even though both eventually ask
  the shared access-lifecycle owner to act.
- Sales, service delivery, payments, access, and ERP are independent state
  machines. Their transitions cannot be made safe by one distributed database
  transaction or by a later service guessing which owner should be repaired.
- Dotmac ERP owns the general ledger, account mapping, balanced accounting
  journals, tax returns, and financial statements. Sub must not become a shadow
  ERP ledger.

The target must make an incomplete handoff explicit at the time it occurs.
Detecting a disagreement later is not a substitute for guaranteeing that every
committed owner output is durably delivered to its declared consumers.

## Decision

### 1. End-to-end topology

The target is a directed graph of owner state machines:

```text
accepted commercial order or authorized service change
  -> versioned billing contract
  -> scheduled/due billing obligation
  -> deterministic rating and tax treatment
  -> prepaid resolution or postpaid receivable/document
  -> payment, credit, grant, waiver, write-off, cancellation, or reversal
  -> operational customer-subledger posting
  -> typed customer financial position
  -> service-period coverage and/or collections consequence
  -> access-lifecycle transition
  -> customer statement/reporting and Dotmac ERP projection
```

This is not one mega state machine. Every node owns its records, transition,
transaction, correction protocol, and versioned output. Some outputs fan out in
parallel. A failed consumer never rolls back the predecessor's committed fact.

### 2. Guaranteed owner-output protocol

Every cross-owner handoff follows the same protocol:

1. The producing owner enters `execute_owner_command` on a transaction-free
   session.
2. The authoritative transition, audit evidence, and versioned outbox event
   commit atomically.
3. `events.dispatcher` delivers the event at least once until one consumer
   attempt commits success or reaches an explicitly reviewed terminal failure.
4. The consumer uses a unique `(consumer, event_id)` receipt plus its business
   idempotency key.
5. The consumer commits its authoritative transition, receipt outcome, audit,
   and next outbox event atomically.
6. A retryable failure remains durable and visible. It is never converted into
   a successful log entry or silently abandoned.
7. A wrong fact is corrected only by its owner through an immutable correction,
   reversal, cancellation, or supersession transition. That transition emits
   the output that drives downstream compensation.

The system therefore has completed, pending, retrying, or explicitly failed
work. It does not have silent cross-owner drift that a generic reconciler later
interprets.

Monitoring owns transport and workflow health only: stuck pending outputs,
exhausted attempts, overdue timers, and missing terminal acknowledgements.
Monitoring cannot rewrite a business owner.

### 3. Authority boundary and proposed owners

The names below are target service identities. New services require complete
typed `ServiceContract` declarations and cannot enter the legacy manifest
baseline.

| Concern | Target owner | Authoritative result |
| --- | --- | --- |
| Commercial order, order line, negotiated terms, order waiver | `sales.orders` | Accepted or corrected commercial commitment |
| Subscription billing terms and effective versions | `billing.contracts` | Immutable `BillingContractVersion` and lines |
| Calendar periods and due triggers | `billing.obligations` | Unique obligation for an exact source and period |
| Rate calculation and proration | `billing.rating` | Typed rated-obligation result; read-only policy/resolver |
| Tax-rate records and tax treatment vocabulary | `financial.tax_configuration` | Effective tax inputs consumed by rating/documents |
| Customer invoice lifecycle | `financial.invoices` | Invoice and structurally linked invoice lines |
| Customer credit-note lifecycle | `financial.credit_notes` | Credit document and exact applications |
| Cash settlement, allocation, refund, and reversal | `financial.payments` | Payment facts and exact obligation/document applications |
| Operational customer-subledger postings | `financial.customer_subledger` | Immutable posting groups, typed effects, and reversals |
| Customer financial position | `customer.financial_position` | Read-only per-currency position derived only from the subledger |
| Prepaid funded/granted service period | `billing.service_entitlements` | Exact active, replaced, or reversed entitlement interval |
| Postpaid delinquency decision | `collections.postpaid_policy` | Typed overdue-receivable decision |
| Prepaid underfunding decision | `collections.prepaid_policy` | Typed uncovered/underfunded-service decision |
| Collections case, timers, notices, and consequence request | `collections.lifecycle` | One reason-scoped collections workflow |
| Subscription/account access state and enforcement locks | `access.subscription_lifecycle` | The only service-state/access mutation |
| Durable owner-output delivery | `events.dispatcher` | Receipt, attempt, retry, and terminal-delivery evidence |
| Durable time trigger | `runtime.durable_timers` | Exact owner, entity, due time, generation, and trigger event |
| Customer billing accounting export | `integration.dotmac_erp_billing_adapter` | Idempotent transport/delivery evidence only |
| General ledger and statutory accounting | `external:dotmac_erp` | ERP-owned mappings, journals, returns, and statements |

Routes, APIs, webhooks, Celery tasks, CLIs, templates, provider connectors, and
ERP clients remain adapters. They create sessions, authenticate inputs, and map
outputs; they own no billing transition.

### 4. Required domain records

Exact table names may change during implementation, but the following typed
records and identities are required.

#### Billing contract and version

A billing contract represents accepted customer-specific billing terms. It is
not the mutable current catalog offer.

Each version records:

- account and subscription identity;
- source kind and structural source identity, such as SalesOrderLine,
  authorized plan change, renewal, or staff-approved correction;
- effective `[starts_at, ends_at)` interval;
- contracted price and currency;
- rate basis and quantity;
- service interval and invoice aggregation interval;
- prepaid/postpaid collection timing;
- calendar anchor, timezone, and alignment rule;
- proration rule;
- payment terms;
- discount and tax-treatment inputs;
- version, supersession, actor, reason, and idempotency evidence.

At most one version may be effective for one contract line at an instant.
Historical terms are never rewritten when catalog prices or policies change.

#### Composable cadence

Cadence is a value object, not a growing list of special-case cycles:

```text
BillingCadence
  rate_basis
  rate_unit
  rate_quantity
  service_interval { unit, count }
  invoice_interval { unit, count }
  collection_timing { advance, arrears }
  anchor
  timezone
  alignment
  proration_policy
```

Calendar and fixed-duration meanings are distinct:

- quarterly is three calendar months, not ninety days;
- annual is twelve calendar months, not 365 days;
- a monthly anniversary on the 29th, 30th, or 31st uses one declared
  end-of-month rule;
- every service and invoice interval uses `[starts_at, ends_at)`;
- proration declares actual calendar days, actual elapsed time, full period, or
  no proration instead of choosing implicitly;
- rate unit and invoice interval may differ, allowing a daily rate to be
  aggregated into a monthly invoice.

Examples:

| Contract | Rate | Service/invoice period | Timing |
| --- | --- | --- | --- |
| Daily prepaid access | fixed per day | one day | advance |
| Fourteen-day prepaid access | fixed per period | fourteen days | advance |
| Daily-rated business service | per active day | one calendar month | arrears |
| Monthly residential service | fixed per month | one calendar month | advance or arrears |
| Quarterly service | fixed per three months | three calendar months | advance or arrears |
| Annual contract billed quarterly | annual/contracted rate allocation | service year; invoice every three months | arrears or advance by term |

#### Billing obligation

An obligation is the finite billable unit. Its natural identity includes:

```text
contract line
  + contract version
  + charge component
  + source fact/version
  + period start
  + period end
  + currency
```

Database uniqueness and owner locking prevent duplicate obligations under
replay or concurrency.

An obligation records rated net, tax, and gross values; currency; accounting
treatment; earning/service interval; source identity; and state. It is not an
invoice, payment, or entitlement.

Target obligation states are:

```text
scheduled -> open -> partially_resolved -> resolved
                    \-> canceled
                    \-> written_off
resolved/canceled/written_off -> corrected or reversed by linked evidence
```

Every terminal resolution is explicit: settlement, credit, grant, waiver,
write-off, pre-earning cancellation, or reversal. Status is never inferred from
an invoice label or a payment origin string.

Recurring obligations after activation remain linked to the billing contract
and subscription. They do not inflate the finite funding result of the original
SalesOrder.

#### Structural applications

Partial settlement and document aggregation require structural association
records:

- obligation to invoice-line association;
- payment to obligation/document application;
- credit-note to obligation/document application;
- prepaid funding reservation and consumption;
- grant/waiver/write-off resolution;
- reversal/correction to the exact original application or resolution.

A payment's originating order is provenance. Only exact applications determine
which obligation was settled.

#### Operational customer subledger

`financial.customer_subledger` replaces the current split between individual
ledger rows and multi-source balance formulas.

A `CustomerPostingGroup` records:

- one business-owner command and source identity;
- account, currency, occurred-at and recorded-at timestamps;
- command, correlation, causation, and idempotency identifiers;
- zero or more typed `CustomerPositionEffect` records;
- exact obligation, document, payment, credit, entitlement, and correction
  links where applicable;
- reversal/supersession identity;
- audit provenance.

Position effects use typed economic meanings such as receivable issue,
receivable settlement, customer credit creation/consumption, prepaid
reservation/consumption, write-off, refund, and adjustment. They are not ERP
chart-of-account debits and credits.

The subledger enforces:

- positive amount and explicit currency;
- exact account/link compatibility;
- one posting group per idempotent business result;
- one active reversal chain;
- application and consumption cannot exceed their structural source;
- transfer/conservation invariants for allocations;
- no direct update or deletion of posted economic history;
- no independent participant commit.

The public business owner decides why money moves. It invokes the subledger as a
required flush-only participant and commits the document/resolution, posting,
audit, and event atomically.

#### Typed customer financial position

Customer financial position is calculated only from the operational customer
subledger, never by recombining documents with selected ledger rows.

It exposes, per currency:

- collectible receivable;
- confirmed unapplied customer credit;
- prepaid funding reserved for open obligations;
- prepaid funding consumed by service;
- refundable credit;
- write-off and adjustment evidence where needed for reporting.

There is no cross-currency total and no generic `account.balance`.

Critical decisions either aggregate immutable postings directly or consume a
typed position materialization written by the subledger owner in the same
transaction. An asynchronous UI cache is never an enforcement input.

Reviewed opening positions become explicit immutable opening posting groups
during migration. They do not remain a permanent parallel calculation branch.
Initial authority activation requires complete-cohort evidence. Once authority
is active, a separately approved single-account completion may capture one
newly eligible native-after-handoff account against the immutable original
cutoff; it revalidates only that explicit account under lock and cannot waive or
alter unrelated source debt.

### 5. Prepaid and postpaid semantics

Billing mode is collection timing on the effective contract. It is independent
of cadence.

#### Prepaid

```text
obligation opens
  -> existing confirmed credit is reserved/applied
  -> obligation resolves
  -> one prepaid consumption posting commits
  -> one exact service entitlement commits
  -> entitlement output requests access evaluation
  -> next obligation timer is scheduled
```

If funding is insufficient, no receivable is created merely to support
enforcement. The obligation emits `billing.prepaid_underfunded`, which starts or
advances the collections workflow. A later settlement output applies funding
and continues the same obligation.

Prepaid invoices, when legally or operationally required, use an explicit
`prepaid_consumption` accounting treatment. They are never excluded from AR
through a negative metadata classifier.

#### Postpaid

```text
obligation opens/earns
  -> collectible receivable posting and invoice commit
  -> exact due timer is scheduled
  -> payment/credit application resolves the obligation
  -> settlement output closes collections state and requests access evaluation
```

Provisioning and the active contract permit postpaid service before cash
settlement. Non-payment later creates a reason-scoped collections consequence;
payment is not a prerequisite entitlement.

#### Non-cash treatment

Complimentary, sponsored, granted, waived, and written-off outcomes use typed
resolution records. They do not set billing approval false, fake a payment, or
silently remove an obligation from cohort totals.

### 6. Owner-output event graph

The initial versioned protocol includes at least these outputs:

| Producer output | Required consumer consequence |
| --- | --- |
| `sales.order.accepted` | `billing.contracts` creates the exact contract/version |
| `sales.order.corrected` | `billing.contracts` supersedes affected future terms |
| `subscription.billing_terms_changed` | `billing.contracts` creates a future-effective version |
| `billing.contract.activated` | `billing.obligations` creates/schedules its first exact obligation |
| `billing.obligation_timer_due` | `billing.obligations` opens the named obligation only |
| `billing.obligation.opened` | prepaid funding application or postpaid invoice/receivable owner acts |
| `financial.invoice.issued` | collections due timer and ERP document projection are created |
| `financial.payment.settled` | exact allocation/application owner acts; confirmed cash is retained |
| `billing.obligation.resolved` | prepaid entitlement, SalesOrder funding gate where finite, and downstream reporting act |
| `billing.service_entitlement_granted` | access evaluation and next-period scheduling act |
| `billing.prepaid_underfunded` | `collections.lifecycle` starts/advances the exact reason-scoped case |
| `financial.invoice.overdue` | postpaid policy and `collections.lifecycle` act |
| `collections.consequence_requested` | `access.subscription_lifecycle` alone changes access |
| `financial.invoice.settled` or `billing.prepaid_funded` | collections close/restore evaluation acts |
| `financial.customer_posting.committed` | statements/reporting and ERP financial projection consume the committed fact |
| owner-specific `*.corrected`, `*.reversed`, or `*.superseded` | named consumers apply idempotent compensating transitions |

Every event schema declares:

- stable event and schema version;
- producer owner and authoritative source identity;
- exact consumer capability;
- command, correlation and causation identities;
- business idempotency key;
- occurred-at and recorded-at times;
- additive compatibility rule;
- retry and terminal-failure policy.

Consumers never re-decide the producer's fact. They validate identity and use it
as one input to their own transition.

### 7. Durable timers replace business sweeps

Every time-based transition is represented by a durable timer:

```text
owner
  + entity
  + purpose
  + generation
  + due_at
  + expected source version
  + output event type
```

The owning transition creates, replaces, or cancels its timer. A unique current
generation makes stale deliveries harmless.

`runtime.durable_timers` stages that timer mutation as a required flush-only
participant in the business owner's transaction. It never commits on its own,
and an owner transition that requires a future action cannot commit without its
timer.

Examples:

- contract activation schedules the first obligation;
- entitlement creation schedules the next prepaid obligation;
- opening a postpaid obligation schedules the next contract period independently
  of whether the current receivable is later paid;
- invoice issuance schedules due/overdue collections review;
- payment arrangement schedules installments;
- grace decisions schedule the exact next collections action;
- a policy change emits an exact versioned output that makes
  `collections.lifecycle` replace affected open-case timers;
- contract correction, cancellation, settlement, shield, or extension replaces
  or cancels affected timers through its owner output.

`runtime.durable_timers` may scan the indexed timer table for `due_at <= now`,
but it performs no customer, invoice, funding, or access decision. It only
emits the declared trigger and records delivery.

The target retires the business-wide prepaid balance sweep, postpaid dunning
sweep, and other account scans that reconstruct which transition should have
been scheduled.

### 8. Collections and access

Prepaid and postpaid retain distinct policy inputs:

- postpaid evaluates an exact overdue collectible receivable;
- prepaid evaluates an exact uncovered obligation and available typed funding.

Both planners return a typed proposal to `collections.lifecycle`. That owner
alone owns:

- one case per account/subscription/reason as policy requires;
- warning and escalation states;
- exact durable timers;
- customer-notice request;
- shields, arrangements, extensions, and grace application;
- consequence-request idempotency;
- close/reopen evidence.

`collections.lifecycle` does not mutate subscription or RADIUS state. It emits a
reason-scoped consequence request to `access.subscription_lifecycle`.

The access owner locks and revalidates its own state, applies or removes only
the matching enforcement restriction, and emits the resulting access output.
Financial recovery cannot remove fraud, outage, customer-hold, administrative,
or other unrelated restrictions.

### 9. Sales, fulfillment, and service activation

Sales owns commercial acceptance, not payment truth.

The accepted SalesOrderLine creates the finite contract/obligation chain.
Finance outputs exact obligation resolution. `sales.orders` may transition its
funding gate only by consuming the resolution output for the complete finite
order-obligation set.

`SalesOrder.amount_paid` or similar counters are not authority. The target
stores exact obligation identities and funding-gate transition evidence.

Full installation/order funding may permit the existing fulfillment owner to
create pending service artifacts. Billing still cannot activate service.
Provisioning success plus the applicable prepaid entitlement or postpaid
contract eligibility requests the access owner to activate the Subscription.

Future recurring obligations belong to the subscription contract and do not
reopen or inflate the original order's finite funding gate.

### 10. Payment-provider boundary

Provider webhooks and reconciliation observations enter through the existing
verified inbox/provider-event owners.

A confirmed settlement always commits:

- provider receipt/event evidence;
- Payment settlement state;
- the cash/customer-credit posting;
- the payment output event.

Allocation, entitlement, receipt delivery, access, or ERP failure cannot roll
back confirmed cash. Those consumers retain durable pending/retry state.

Pending checkout, failed payment, uploaded proof, or provider intent creates no
cash and no access consequence.

### 11. ERP boundary

Sub owns ISP operational source facts:

- billing contract and obligation identity;
- invoice and credit-note documents and tax treatment;
- payment, refund, reversal, and WHT evidence;
- operational customer-subledger postings;
- service entitlement and collections/access correlation.

Dotmac ERP exclusively owns:

- chart of accounts and TaxCode mappings;
- balanced general-ledger journals;
- accounting periods and controls;
- tax transactions and returns;
- statutory and management financial statements.

`integration.dotmac_erp_billing_adapter` maps committed Sub owner outputs into a
versioned ERP transport contract. Delivery and ERP acknowledgement are durable
and idempotent. Missing or ambiguous mappings fail closed in ERP. ERP downtime
does not roll back Sub cash, documents, entitlement, or customer access.

Sub does not create a shadow GL or infer accounting success from transport
receipt alone.

### 12. Corrections

The owner of an incorrect fact owns the correction:

- Sales corrects or cancels the commercial commitment.
- Billing supersedes a contract version or corrects/reverses an obligation.
- Invoice/credit-note owners issue document corrections.
- Payment owns refunds and reversals.
- The customer subledger posts linked reversals; it never edits history.
- Entitlement owns shortening, replacement, or revocation from exact corrected
  evidence.
- Collections corrects its case/timer state.
- Access corrects only its reason-scoped restriction.

Each correction output drives declared compensating consumers. A generic
cross-owner reconciler cannot compare states and select a winner.

Deterministic non-authoritative UI caches may be discarded and rebuilt. That is
cache maintenance, not business-state repair.

## Invariants

1. Every active subscription has exactly one effective billing contract version
   for an instant, with explicit mode, cadence, price, currency, tax inputs,
   alignment, and proration.
2. Every obligation has one structural source and one unique natural identity.
3. Order funding considers only its finite structurally linked obligations.
   Future recurring obligations cannot alter the historical order result.
4. Metadata and external identifiers are provenance, never authoritative joins.
5. Rate unit, service interval, invoice interval, and collection timing are
   independent typed facts.
6. Calendar-month periods are not converted to fixed day or second counts.
7. Prepaid obligations require exact funding or a typed non-cash resolution
   before entitlement; they do not create AR for enforcement convenience.
8. Postpaid obligations create receivables and may permit provisioned service
   before settlement according to contract and access policy.
9. Every document line, application, posting, entitlement, and correction links
   structurally to its obligation/source.
10. Every money-affecting owner command stages exactly one idempotent posting
    group as a required flush-only participant.
11. The operational customer subledger is append-only and is not an ERP GL.
12. Customer position is derived from one subledger and is separated by
    currency and semantic lane.
13. There is no mutable generic account balance and no nominal cross-currency
    comparison.
14. Confirmed cash survives allocation, notification, access, and ERP failure.
15. Every authoritative transition and output event commit atomically.
16. Every consumer effect is idempotent and commits its receipt and next output
    atomically.
17. Every incomplete handoff is durably pending, retrying, or explicitly
    terminal-failed. Logs are not completion evidence.
18. Time-based business transitions have exact durable timers; a scanner cannot
    reconstruct or decide them.
19. Only `access.subscription_lifecycle` writes subscription/account access
    state and reason-scoped enforcement restrictions.
20. A correction originates only from the owner of the wrong fact and produces
    downstream compensation events.
21. External collaboration and ERP remain transports/consumers unless an
    accepted contract explicitly assigns them authority.
22. Schedulers, routes, tasks, webhooks, CLIs, templates, and integrations are
    adapters around registered owners.
23. Event dispatch and due-timer dispatch are permanent lifecycle
    infrastructure. Their polling cadence may be configured, but their
    authority path cannot be disabled by a feature or readiness toggle.

## Consequences

### Positive

- Daily, weekly, monthly, multi-month, annual, and mixed rate/invoice cadence
  use one period engine for prepaid and postpaid.
- Sale-to-Money gains a structural contract and obligation identity.
- Money position becomes one indexed posting projection rather than several
  exclusion-heavy formulas.
- Confirmed cash, obligation resolution, service entitlement, and access
  consequences have exact causal evidence.
- Per-entity timers replace repeated account/invoice scans and reduce N+1
  resolution work.
- Failures are visible when they occur and retain their trigger for retry.
- Corrections follow owner state machines rather than generic data repair.
- ERP can be unavailable without becoming operational billing authority.

### Costs

- Contract, obligation, application, posting-group, receipt, and timer schemas
  add explicit records.
- Every owner transition must publish and version its output contract.
- Historical data requires reviewed structural backfill and exception
  classification.
- Operations must manage retry/dead-letter queues and owner correction
  workflows.
- Current callers, reports, tasks, and legacy owner contracts require phased
  migration.

### Rejected alternatives

- One distributed billing state machine: it would absorb Sales, payments,
  provisioning, access, and ERP authority and require unsafe cross-domain
  transactions.
- Separate prepaid and postpaid engines: it duplicates period, posting,
  customer-position, timer, notice, and access-consequence machinery.
- Periodic cross-owner drift detection: it discovers failed handoffs late and
  gives a non-owner permission to decide which state is wrong.
- Business-wide dunning/prepaid sweeps: they repeatedly reconstruct work that
  should have been scheduled by the owning transition.
- Mutable account balance: it erases receivable, credit, funding, and currency
  meaning.
- Document-only balance reconstruction: it requires permanent exclusions and
  cannot guarantee one representation of each economic effect.
- Sub double-entry/GL implementation: it duplicates Dotmac ERP authority.
- Big-bang replacement: money authority cannot safely move without shadow
  evidence and finance approval.

## Migration and cutover

Migration is expand, backfill, verify, cut over, and contract. Every phase is a
coherent reviewable slice with its own forward-fix plan.

### Phase 0: accept the target and ratchet new work

- Old owner and paths: current `financial.*` owners, metadata joins, duplicated
  mode fields, per-entry ledger, multi-source balance projection, and sweeps.
- New owner and paths: the owners and protocols in this ADR.
- Work: accept this ADR; define complete typed contracts and event schemas for
  the first slice; add architecture guards preventing new metadata-authority,
  mutable-balance, uncontracted financial owner, or sweep paths.
- Gate: Michael accepts the ADR and the registry/map accurately declare the
  target migration without claiming cutover.

### Phase 1: structural Sale/Service-to-Billing contract

- Expand: add contract, version, line, source, and obligation identities without
  changing current reads.
- Backfill: link active subscriptions and finite SalesOrder lines through
  reviewed source evidence. Ambiguous metadata remains a typed exception and is
  never guessed.
- Shadow: create proposed contracts/obligations beside current invoice and
  renewal behavior; write no duplicate financial effect.
- Gate:
  - every new accepted order/service change creates one structural contract;
  - every active subscription has one proposed effective version;
  - no mixed mode/cadence/price ambiguity in the cutover cohort;
  - unexpected-unlinked and ambiguous cohorts are zero;
  - historical exclusions are exhaustive, typed, and approved.
- Retirement after cutover: metadata joins and duplicate account/catalog
  effective billing-mode reads.

### Phase 2: composable obligation and rating engine

- Expand: implement calendar interval, rating, tax-input, proration, and
  obligation state contracts.
- Shadow: compare obligations and rated totals against current postpaid
  invoice-generation and prepaid-renewal previews for the complete active
  cohort.
- Current implementation state: shadow obligation creation consumes only
  contract/version/line/period identity and resolves net, tax, gross, and
  currency through `billing.rating`; producer-supplied money is not accepted.
  Durable Phase 2 runs lock and classify the complete active cohort, record
  current-owner and target totals per currency, and preserve expected new
  cadence, unresolved, ambiguous, unlinked, duplicate, gap, overlap, and
  variance evidence. The current postpaid owner now exposes the exact
  base-plus-recurring-add-on components used by invoice execution, including
  tax, proration, route quantity and structural `SubscriptionAddOn` identity.
  Unsafe current behavior such as multiple active prices, mixed currency, or a
  route-capped quantity remains a typed blocker instead of being treated as
  parity. The current prepaid owner explicitly reports recurring add-ons that
  its base-only renewal excludes. The verifier cannot repair any owner or move
  authority.
- Rating-provenance implementation: every newly scheduled shadow obligation
  stores its versioned policy, exact coverage, contracted price/quantity,
  rate unit/quantity, timezone, proration policy/factor, exact tax-rate
  identity/value, and a content fingerprint atomically with the rated result.
  Replay reproduces the result from that snapshot without reading mutable
  current tax configuration; different coverage for the same natural identity
  fails closed. Existing obligations remain explicitly provenance-incomplete
  instead of receiving inferred historical inputs, and therefore block the
  Phase 2 cohort until they are replaced or reviewed through an approved
  owner-backed migration. Future rating policies must add a replay
  implementation; they must not change the meaning of `billing-rating-v1`.
- Structural recurring-add-on capture is now an owner-output chain, not a
  cross-owner comparison or repair loop. The temporary
  `billing.addon_contract_backfill` migration owner locks the current shadow
  contract, rebuilds an exact future-period snapshot of
  `SubscriptionAddOn.id`, `AddOnPrice.id`, quantity, price, currency and source
  interval, confirms a fingerprint, and atomically stages
  `billing.addon_contract_backfill.captured`. `billing.contracts` receipts that
  output into the same non-effective boundary draft and exact durable timer
  used by live changes, preserves the base-line lineage, and gives every
  recurring add-on `component_key == str(SubscriptionAddOn.id)`. Only the fired
  timer generation supersedes the current shadow version and emits the
  identity-only obligation handoff; the normal obligation and terminal-evidence
  owners then advance the chain. Ambiguous prices, mixed currency,
  partial-period terms, stale versions, invalid quantities, and missing
  structural sale anchors fail closed before a version is written.
- The existing `billing_target_shadow` operator adapter exposes preview and
  capture as separate commands. Capture requires the reviewed SHA-256
  fingerprint and a stable idempotency key; the adapter cannot bypass either
  owner or promote authority.
- This capture remains migration-only. It does not charge, repair another
  owner, cut over prepaid/postpaid reads, or prove a real cohort complete.
  Cancellation, admin, route, sales, and remediation writers still need to
  emit owner-backed billing-terms outputs atomically with their transitions;
  only then can the temporary backfill producer be retired.
- Live customer recurring add-on purchase now completes the owner-output
  shadow chain without resetting the base subscription cadence. The typed
  `financial.addon_purchases` command owns the entitlement, exact adjustment
  link, idempotency, audit, and
  `billing.contract_terms.recurring_addon_added` output in one transaction.
  `billing.contracts` receipts the immutable accepted term, derives the next
  service-period boundary from its current version, adds the term to one
  non-effective draft, and atomically creates or replaces that contract's
  `pending_terms_effective` durable timer. Further purchases before the same
  boundary add lines to the same draft; they do not advance or reset the
  cadence anchor. If a delayed live output arrives after an exact backfilled
  term has already become effective, the owner receipts that satisfied
  identity and creates neither another draft nor another timer.
- When the exact timer generation fires, `billing.contracts` receipts it,
  validates the expected draft version, closes the prior half-open interval,
  promotes the draft to effective shadow terms, and emits the standard
  identity-only obligation handoff. `billing.obligations` and
  `billing.shadow_verification` receipt the remaining outputs. A cadence that
  differs from the base contract, mixed currency, stale draft, stale timer,
  missing structural sale anchor, or malformed term fails closed. No authority
  or money reader moved.
- Cancellation deliberately remains open. It must carry causal identity back
  to the purchase output so an out-of-order cancellation cannot be receipted
  before the term it removes; adding an unordered negative event would recreate
  the very drift this architecture is intended to prevent.
- Gate:
  - exact period and amount parity for existing supported contracts;
  - exact one-to-one `SubscriptionAddOn.id` to add-on contract-line identity
    and complete-cycle component parity for postpaid;
  - zero prepaid recurring-add-on exclusions until the prepaid owner consumes
    complete rated obligations under a separately approved money cutover;
  - approved expected differences for newly supported cadence;
  - complete fingerprint-valid rating provenance for every included
    obligation;
  - zero duplicate/gapped/overlapping obligations outside typed policy;
  - concurrency and replay uniqueness on PostgreSQL;
  - leap-year, end-of-month, timezone, late/lapsed, and proration matrix green.
- Cutover: invoice and prepaid flows consume obligations, not independent period
  calculations.
- Retirement: current `_period_end`/monthly-only renewal decision forks and
  JSON/metadata invoice accounting classification.

### Phase 3: operational customer subledger

- Expand: add posting groups/effects and structural document/application links.
  Existing money owners stage shadow posting groups in their current atomic
  transactions.
- Backfill:
  - derive every migrated prepaid opening from the complete frozen Splynx
    transaction set, where credits minus debits is the target and a complete
    empty set is zero;
  - advance that source position with canonical Sub-native facts after the
    fixed handoff, then convert only the residual not already represented in
    the subledger into an opening posting group;
  - map current invoices, settlements, applications, credit notes, write-offs,
    adjustments, prepaid consumption, refunds, and reversals;
  - fail the whole opening batch on missing customer coverage, duplicate or
    mismatched identity, malformed history, or an unreconciled transaction net.
- Shadow: compare current customer financial position and new subledger position
  per account/currency and semantic lane.
- Gate:
  - 100% new money-changing paths produce one posting group;
  - unresolved, ambiguous, duplicate, and unexpected-unlinked rows are zero;
  - per-currency/lane differences are zero for the approved observation window;
  - reversal, partial allocation, refund, and concurrent consumption tests pass;
  - finance signs the cohort and cutover evidence.
- Cutover: authoritative financial-position reads use only the customer
  subledger.
- Retirement: document-union balance formulas, account-credit special formula,
  permanent opening-baseline branch, `balance_after` authority, and legacy
  `financial.ledger` writer paths.

Current prepaid cutover implementation:

- Forward money owners stage typed, idempotent posting groups in the same owner
  transaction. Prepaid renewal execution is now a public owner command; the
  hourly runner and funding-change event consumer enter that boundary on clean
  sessions, and exact command replay cannot manufacture a posting for a
  pre-shadow renewal.
- `billing.opening_balance_history` is the cutover-only resolver over the frozen,
  isolated final Splynx audit restore. For every migrated account it requires
  one matching source row and proves the active transaction net equals the
  final source position. A complete empty transaction set is exactly zero. A
  native account created after the handoff has an explicit zero history
  component and advances only from canonical native facts. The complete result
  is fingerprinted; it has no per-account unknown, guessed-zero, or quarantine
  outcome.
- The signed reconstruction artifact covers the exact current prepaid funding
  cohort. Missing, duplicate, mismatched, malformed, or unreconciled evidence
  aborts the entire artifact. The materializer rejects signed partial-subset
  manifests. After materialization, Splynx remains retired as authority and the
  evidence reader is not a runtime balance path.
- `billing.shadow_verification` records an exhaustive, fingerprinted opening
  proposal for the prepaid funding cohort. A completion run fingerprints and
  preserves existing immutable openings and proposes only accounts still
  missing an opening. Each new residual is the verified history-derived target
  minus the shadow value already represented at the cutoff. Operator and
  finance approvals are separate immutable facts.
- `financial.customer_subledger_opening_positions` is the sole migration writer.
  It converts only the approved residuals into immutable opening evidence and
  posting groups. Zero residuals still receive an explicit zero-effect group;
  no account is omitted merely because its opening is zero.
- A second durable verifier compares every eligible account and currency after
  capture, records all semantic lanes, and classifies every observation-window
  money fact as missing, exact, or duplicate posting coverage. Cutover is
  impossible while any blocker is non-zero.
- Payment parity uses the exact `PaymentSettlement.amount` as customer value
  whenever structural settlement evidence exists. `Payment.amount` remains the
  gross gateway charge, and a provider fee never becomes prepaid funding.
  Historical payments without settlement evidence retain a bounded
  gross-minus-refund fallback until reviewed reconciliation; the resolver never
  guesses net value from a fee field alone.
- Once customer-subledger authority is active, the immutable, finance-approved
  `CustomerSubledgerOpeningPosition.legacy_position` and `occurred_at` become
  the legacy verifier's temporal baseline. Facts at or before that opening keep
  the exact financial meaning approved during cutover; only facts crossing the
  opening instant adopt current native-event semantics. This verifier rule is
  read-only: it never rewrites an opening, changes authority, or manufactures a
  posting.
- The one irreversible authority record is bound to its approved parity
  fingerprint. Default position reads include historical shadow groups and new
  authoritative groups. Because production authority is already active, the
  complete-history completion does not reactivate or rewrite it: it appends the
  missing reviewed residual openings and then proves full-cohort parity. There
  is no permanent excluded cohort. Accounts created after authority activation
  start at authoritative zero and accumulate native postings without a
  migration opening. A later source error is corrected forward
  through typed, reviewed reversal/adjustment owners; immutable openings and
  historical postings are never edited.
- This cutover is limited to the operational prepaid position cohort. The
  legacy formulas and sweeps named in later phases are not retired merely by
  creating the Phase 3 authority record.

### Phase 4: durable owner-output chain

- Expand: add versioned event contracts, consumer receipts, attempt states,
  causal identities, correction events, and dead-letter review.
- Shadow: current adapters run while each committed owner result proves that its
  target event/receipt chain reaches the expected terminal consumer.
- Gate:
  - state transition cannot commit without its required output;
  - consumer cannot acknowledge before committing its effect;
  - replay produces one business effect;
  - injected consumer/transport failure remains retryable and later completes;
  - every event has a named terminal consumer outcome;
  - wrong-owner correction tests prove downstream compensation.
- Cutover: event outputs, not adapter composition, trigger cross-owner work.
- Retirement: direct cross-owner calls that independently commit, logged-only
  failure, and generic cross-owner repair.

### Phase 5: durable timers and unified collections

- Expand: create owner-bound timer records, generation replacement, mode policy
  planners, and one collections lifecycle.
- Shadow: scheduled timers produce the same or explicitly approved outcomes as
  current postpaid dunning and prepaid enforcement for the full candidate
  cohort, without applying duplicate consequences.
- Gate:
  - every open invoice, prepaid period, grace deadline, arrangement installment,
    and escalation has exactly one current timer or a typed no-timer reason;
  - stale timer delivery is idempotently rejected;
  - settlement/correction/cancellation replaces or cancels exact timers;
  - mode planners and consequence owner pass the full policy/shield matrix;
  - access has one writer and one reason-scoped consequence path;
  - timer backlog and delivery SLOs are operationally accepted.
- Cutover: enable timer-triggered collections outputs.
- Retirement: `dunning_runner`, `prepaid_balance_sweep`, duplicate notice/timer
  fields, account-wide enforcement loops, and parallel access actions.

### Phase 6: Sales fulfillment and access chain

- Expand: finite order-funding gate consumes exact obligation-resolution
  outputs; provisioning and entitlement/access outputs carry the same contract
  correlation.
- Shadow: compare current SalesOrder funding/fulfillment projections and access
  consequences without changing legacy columns.
- Gate:
  - partial funding never releases service;
  - full finite funding advances the order exactly once;
  - future recurring obligations cannot affect the original order;
  - prepaid requires entitlement; postpaid follows contract/provisioning policy;
  - financial recovery cannot clear unrelated access restrictions;
  - all missing downstream work is represented by pending/failed delivery, not
    an unexplained state.
- Retirement: `SalesOrder.amount_paid` authority, metadata payment-origin joins,
  and billing-owned activation.

### Phase 7: ERP projection and schema contract

- Expand: versioned ERP billing payloads consume committed document/payment/
  posting outputs and retain durable acknowledgements.
- Gate:
  - invoice, credit-note, payment, refund/reversal, tax/WHT, and correction
    payloads have stable idempotency and replay;
  - ERP outage/failure cannot roll back Sub source facts;
  - missing/ambiguous ERP mappings fail closed in ERP;
  - source and ERP identities are structurally recorded;
  - finance approves accounting parity.
- Contract:
  - remove fallback reads/writers and obsolete columns;
  - shrink the legacy manifest baseline;
  - split pipeline, record, projection, collections, integration, and control
    concerns out of the `financial.*` grab bag where the accepted owner map
    requires it;
  - update the relationship map, runbooks, operator surfaces, and architecture
    tests together.

## Cutover evidence standard

Every gate uses a durable cohort/run record containing:

- schema and policy version;
- run/cutoff timestamp and observation window;
- exhaustive cohort classification;
- source and result fingerprints;
- counts and money totals per currency;
- unresolved, ambiguous, unexpected-unlinked, duplicate, and shadow-variance
  categories;
- event/timer delivery outcomes where applicable;
- operator and finance approvals;
- exact code/schema versions.

WARNING logs are alerts, not cutover evidence.

After an authority cutover, rollback cannot restore metadata joins, mutable
balances, legacy fallbacks, or sweep decision paths. Recovery is a forward fix
or an owner correction event.

## Verification

### Behavior matrix

- prepaid and postpaid for day, week, month, multi-month, and year intervals;
- rate unit different from invoice interval;
- month-end, leap-year, timezone, and alignment boundaries;
- activation, lapsed service, cancellation, plan change, and proration;
- installation, recurring, add-on, usage, discount, tax, grant, and waiver;
- partial payment, overpayment, account credit, multi-invoice allocation;
- credit note, write-off, refund, reversal, and correction;
- mixed currency refusal and per-currency reporting;
- provisioned prepaid without funding and funded prepaid without provisioning;
- postpaid overdue, arrangement, grace, shield, suspension, settlement, and
  restoration;
- ERP outage, rejection, replay, and correction delivery.

### Transaction and concurrency

- owner-command admission on a clean session;
- required subledger participant remains flush-only;
- state, posting, audit, and event roll back together;
- producer event and state commit atomically;
- consumer receipt, effect, and next output commit atomically;
- concurrent obligation generation, allocation, funding consumption, reversal,
  timer fire, and consequence application produce one effect;
- provider settlement persists when downstream allocation or access fails.

### Architecture

- every target owner has a complete typed `ServiceContract`;
- no target service enters the legacy baseline;
- adapters cannot write contract, obligation, posting, collections, or access
  state;
- metadata/JSON cannot decide financial treatment or ownership;
- only the customer-subledger module writes posting records/materialized
  position;
- only access lifecycle writes access restrictions;
- no business-wide dunning/prepaid sweep remains;
- no cross-owner drift-repair service exists;
- no Sub chart-of-account, TaxCode mapping, or GL journal is introduced.

### Migration and operations

- PostgreSQL backfill, uniqueness, locking, and constraint tests;
- complete cohort parity by account, obligation, semantic lane, and currency;
- durable event/timer failure-injection tests;
- queue/dead-letter/timer-backlog dashboards and alerts;
- bounded set-based due-timer and financial-position query budgets;
- operator runbooks for owner correction, terminal delivery failure, finance
  exception, and ERP replay;
- full repository-prescribed validation before publication.

## Rollback or forward-fix

Before a phase cuts over, shadow records and delivery bindings may be disabled
without changing authority. They remain reviewable migration evidence.

After a phase cuts over:

- do not restore a retired writer, metadata join, mutable balance, or business
  sweep;
- correct an owner fact through its versioned correction protocol;
- replay a pending/failed output through the dispatcher;
- replace/cancel a timer by issuing the owning transition;
- rebuild only non-authoritative caches;
- quarantine ambiguous money and require finance approval;
- deploy a forward schema/code fix when an invariant is defective.

Historical postings, settlements, obligations, corrections, and delivery
evidence are append-only and are not removed by rollback.

## Review and retirement

- Accepted 2026-07-27 by Michael, reviewed for consistency against ADRs
  0001-0003, `docs/SOT_RELATIONSHIP_MAP.md`,
  `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`, and
  `docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`.
- First implementation slice: Phase 1 structural billing contract and
  obligation identity in shadow mode. The receipted
  `sales.fulfillment.funding_applied -> billing.contracts.shadow_recorded ->
  billing.obligations.shadow_scheduled` chain now records proposed terms and
  first-period obligations for newly funded structurally linked services, then
  commits terminal delivery evidence through `billing.shadow_verification`.
  It has no financial or access effect.
- Phase 1 verification runs are durable complete-cohort records with source and
  result fingerprints, exhaustive blocker classifications, per-currency
  totals, owner-output outcomes, and exact code/schema identity. Operator and
  finance approvals are separate and fail closed while any blocker is non-zero;
  recording approvals still does not move authority.
- Phase 2 rating/obligation verification is also durable migration evidence.
  The contract output schema now carries identity only; `billing.obligations`
  invokes `billing.rating` for every shadow amount. Version 1 contract outputs
  remain consumable during shadow rollout, but their amount fields are ignored.
  New obligations persist fingerprinted `billing-rating-v1` replay inputs and
  reproduce their stored result without resolving current tax state. Legacy
  provenance-incomplete obligations are an explicit unresolved cohort, never
  silently backfilled.
  Complete-cohort runs use typed previews from the current postpaid and prepaid
  owners. Postpaid evidence is componentized across base service and recurring
  add-ons, and every included add-on must match the target contract through its
  structural `SubscriptionAddOn.id`. Prepaid evidence exposes its current
  base-only add-on exclusion and therefore stays blocked rather than claiming a
  false complete-cycle match. Runs require exact parity for supported cadence,
  keep newly supported cadence in an explicit expected-difference cohort, and
  block approval on any unresolved, ambiguous, unlinked, duplicate, gap,
  overlap, or variance count. No real cohort run or operator/finance approval
  is implied by this code.
- A temporary, fingerprint-confirmed recurring-add-on backfill producer now
  drives `billing.contracts` through a receipted owner output. The resulting
  contract version and complete base-plus-add-on obligation output commit
  atomically inside their respective owners. This prevents the migration tool
  from becoming a second contract-line writer and preserves Michael's rule
  that one owner output triggers the next owner. No live add-on writer or money
  authority moved in this slice.
- Each subsequent phase is reviewed at its own cutover gate. A gate that
  requires cohort parity or finance approval cannot be satisfied by code review
  alone; it needs a durable run record meeting the cutover evidence standard
  below.
- Retirement condition: all phases have cut over, legacy paths and manifest
  entries are removed, finance accepts the final evidence, and the target
  architecture is represented in the executable SOT manifest.
- Supersedes if accepted: conflicting portions of current Sale-to-Money joins,
  duplicated billing-mode/cadence authority, multi-source customer-position
  computation, separate enforcement sweeps, and generic cross-owner
  reconciliation expectations.
