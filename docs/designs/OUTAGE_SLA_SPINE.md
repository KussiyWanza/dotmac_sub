# Outage and SLA Spine

Status: adopted design baseline (programme PR 0). Policy defaults approved by
Michael on 2026-08-02; only automatic financial posting remains gated on
ISP-specific legal and finance sign-off. Presentation rules follow
`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`; the UI surfaces consuming this
spine are contracted in `docs/designs/CUSTOMER_NETWORK_PATH.md` and
`docs/designs/NETWORK_EXPLORER.md`.

## Decision

The spine gives outage operations and per-customer SLA one named owner per
concern, separating observation, decision, and consequence:

| Concern | Owner |
|---|---|
| Incident lifecycle and immutable scope revisions | `network.outage_lifecycle` (extended) |
| Current topological audience (exposure) | `network.outage_impact` (unchanged) |
| Per-subscription service-impact evidence | `network.service_impact` (new resolver) |
| Immutable customer downtime intervals | `network.customer_outage_accrual` (new) |
| Effective-dated SLA policy and scores | `customer.service_level` (new) |
| Planned maintenance lifecycle | `network.maintenance_lifecycle` (new) |
| Incident ticket and watcher composition | `support.ticket_lifecycle` links (extended) |
| Outage communication decisions | `network.outage_communications` (new) |
| Outage communication delivery | existing communication policy/intents/receipts owners |
| Compensation remedy (preview/confirm) | separate financial command owner, posting gated |

Topological audience means potentially affected — never automatically
unavailable. The shared vocabulary lives in
`app/services/service_impact_contracts.py` and is the only impact vocabulary
any surface renders.

## 1. Confirmed unavailability (approved evidence rules)

`confirmed_unavailable` requires all three: the subscription's effective path
traverses the failed boundary; fresh authoritative evidence shows a
provider-controlled failure; and no proven alternate path serves it.

- A shared AP / PON / FDH-uplink / router / backhaul failure confirms
  unavailability for its exact dependent audience.
- A lone offline CPE/ONU is insufficient (customer power or disconnect).
  Individual last-mile confirmation needs two independent fresh observations
  or an operator-reviewed provider-fault ticket/work order.
- Customer complaints open investigation; they never accrue downtime alone.
- Stale or missing telemetry produces `unknown` — never unavailable, never
  available.
- Degradation is a separate state and counts only where the applicable SLA
  defines a measurable threshold.
- Operator overrides carry typed evidence, reason, effective timestamp, and
  audit history.

## 2. Accrual clocks (approved)

- `started_at` = earliest continuous qualifying failure observation once the
  outage is subsequently confirmed; fall back to `confirmed_at` when that
  timestamp is untrustworthy. Operator declarations backdate only to cited
  evidence.
- `recovered_at` = first healthy observation; finalization waits for a
  recovery hold of at least two successful observations spanning five
  minutes. Failure during the hold keeps one continuous interval.
- `resolved_at` is workflow closure and never determines customer downtime.
- Subscription activation, suspension, migration, and termination clamp each
  customer's interval.
- Instants are stored UTC; calendar periods use Africa/Lagos.
- Corrections are append-only adjustments; history is never rewritten.

## 3. Scope history: reroot, merge, split (approved)

- Incident and impact-episode identifiers are immutable. A reroot appends a
  scope/root revision: monotonic sequence, old and new scope, effective_at,
  reason, evidence, audience delta, and an immutable membership token.
- Merge preserves source incidents, links them to a canonical incident or
  correlation group, and unions overlapping customer intervals — never copies
  or sums them.
- Split creates children prospectively from the split timestamp; pre-split
  accrual stays with the parent.
- Duplicates are linked and suppressed from independent accrual and
  notification.
- Communications fire on a material customer impact-state change, not merely
  because an incident was rerooted.
- Manual reroot/merge/split are preview→confirm commands with stale-token
  conflict protection.

## 3a. Customer communications (implemented)

`network.outage_communications` decides whether a customer is owed a message,
which one, and when. It owns no audience, no impact word, no downtime figure
and no delivery — those stay with `network.service_impact`,
`network.outage_lifecycle`, `network.customer_outage_accrual` and
`communication_intents` respectively.

- Three stages: `opened`, `update` (prolonged outage) and `restored`.
- Only `confirmed_unavailable` opens a conversation. `potentially_affected`,
  `unknown`, `degraded` and a `suspected` incident say nothing — exposure is
  never a message.
- **The restoration cohort is the set of customers actually told**, read from
  `outage_customer_notices` rows carrying a communication-intent id. Never the
  current audience: a mid-incident joiner was promised nothing, and a customer
  who has since left the audience is still owed the all-clear. A suppressed or
  unreachable recipient is not in the cohort.
- One message per distinct customer, not per subscription. Grouping happens
  before policy evaluation and the body names the affected services.
- clearing → reopened is one ledger interval but two conversations. Notices
  carry a per-customer sequence, so a second opening message is possible and
  the first can never be re-sent.
- A discarded incident still closes the conversation. Announcing a fault and
  then going silent reads as an unfixed fault.
- Partial restoration closes only the customers observed back.
- A restoration message quotes the ledger's exact measured downtime; when no
  exact interval exists it states no duration rather than inventing one.
- Every decision writes an append-only `outage_customer_notices` row —
  including dry-run plans and blocked recipients, under separate dedupe-key
  namespaces so neither can mute a later genuine message. The unique dedupe key
  is the concurrency guard.
- Gates are database-authoritative (`outage_customer_comms_*`): enabled,
  dry-run (default on), settling window, minimum affected count, update
  interval, per-run recipient cap, and a per-customer cross-incident cooldown
  that stands in for the merge/split commands that do not exist yet.

**Cutover.** This owner supersedes the classifier-bound
`outage_notifications` / `outage_auto_notify` send paths (ADR 0004). Arming
`outage_customer_comms_enabled` makes both refuse with
`superseded_by_outage_communications` — automatic *and* operator — so two
customer outage senders are never live at once. The legacy paths are removed
once the new owner has run armed through a full incident cycle.

## 4. SLA policy and scoring (approved)

Presentation boundary (Michael, 2026-08-06): SLA policy, scores, verdicts,
evidence, breach state, and any later compensation eligibility remain available
only to authorized staff/admin surfaces. They are not exposed through the
customer portal or customer-facing API unless a later approved change amends
this design. This restriction changes presentation, not the scoring authority.

- Never invent a contractual SLA. With no effective policy, surfaces show
  measured availability plus "No contractual SLA" — no 99.5% default.
  Internal operational targets stay visibly separate from contract.
- Policies are immutable effective-dated versions. Precedence:
  subscription-specific contract → customer/account contract → subscribed
  offer version → SLA-enabled commercial plan-family default → internal
  measurement policy. A mid-period change splits the calculation by policy
  version. The plan-family subset is a closed typed protocol; catalog
  classification alone never creates contractual terms.
- Default reporting period: calendar month in Africa/Lagos.
- Eligibility is the intersection of proven-active lifecycle time and exact
  service-entitlement time. Prepaid entitlement comes from funded
  `ServiceEntitlement` or applied service-extension grant intervals. Postpaid
  entitlement comes only from authoritative accepted billing-contract
  versions; the current shadow contract rows are explicit incomplete evidence,
  never a substitute for entitlement.
- Scoreable time is eligible time less reviewed exclusions. Availability is
  `(scoreable seconds − qualifying unavailable seconds) / scoreable seconds`.
  Excluded time is never counted as uptime.
- `access.subscription_lifecycle_evidence` is the authority for the lifecycle
  half of eligibility. The lifecycle status owner appends evidence in the same
  transaction as each state change; generic CRUD and asynchronous event
  handlers cannot create contractual history.
- Admitted lifecycle evidence carries separate `effective_at` and
  `recorded_at` instants, a closed source/grade vocabulary, a stable source
  identity, and a material fingerprint. Idempotent replay returns the same row;
  reusing an identity for different evidence fails closed.
- Rows created before this admission contract remain immutable but unsupported.
  Migration cutover, subscription creation, and reviewed recovery may append a
  prospective state baseline. A baseline proves state only from its effective
  instant forward; it never upgrades or reconstructs earlier history.
- Lifecycle evidence retains its subscription identity. The subscription
  foreign key is `RESTRICT`, and catalog deletion fails closed once any
  lifecycle evidence exists; operators use a lifecycle transition instead of
  erasing the service and the contractual periods that depend on it.
- Lifecycle completeness is evaluated for the exact scoring period. The left
  edge needs a trusted state anchor, lineage must remain continuous, and an
  unsupported observation breaks coverage until a later trusted transition or
  baseline. An earlier unsupported row does not poison a fully supported later
  period.
- Exact-subscription RADIUS accounting proves positive monitoring only from
  session start through session stop or the last imported observation. Open
  sessions are never extended to evaluation time and subscriber-level unbound
  sessions are never projected retrospectively onto a service.
- `downtime = confirmed unavailable ∩ eligibility`; `excluded = reviewed or
  non-exact outage evidence ∩ eligibility − downtime`; `unknown = eligibility
  − (positive monitoring ∪ downtime ∪ excluded)`. Unknown time is never silent
  uptime.
- An incomplete result exposes lower and upper availability bounds and no
  single measured percentage. It may prove `breach` only when the best-case
  upper bound is below the effective target; it can never produce `passing` or
  `at_risk`.
- Third-party/upstream failure is not automatically excluded where Dotmac
  sold end-to-end service; force majeure is reviewed case-by-case.
- Every calculation is segmented at policy and precedence changes. Recording a
  result appends one `sla_period_score_revisions` row plus exact eligibility and
  positive-monitoring snapshots. Same command identity plus same digest
  replays; changed evidence appends a linked revision under a new identity.
  All three tables are append-only and retain policy, outage, lifecycle,
  entitlement and monitoring lineage. Composite foreign keys bind every child
  snapshot and every supersession link to the same subscription and reporting
  period; a valid UUID from another customer or period cannot cross-link the
  evidence chain. The scorer remains shadow/internal until the admin-only
  discrepancy review and atomic admin-display cutover.
- The admin display selector is database-authoritative and fail-closed. During
  discrepancy review its registered vocabulary contains only
  `legacy_availability`; configuration alone cannot arm
  `customer.service_level`. The later activation change must bind the candidate
  to accepted review evidence and remove the legacy operational display in the
  same cutover. A restricted review page may compare clearly labelled legacy
  and candidate evidence, but no ordinary admin surface presents both as
  competing authorities.

## 5. Planned maintenance (approved)

`network.maintenance_lifecycle` owns draft → approved → announced →
in_progress → completed, plus canceled and overrun.

- At least seven calendar days' customer notice before customer-impacting
  planned work (aligned with the NCC major-outage directive of 25 May 2025,
  which applies to ISPs and last-mile operators).
- The record carries exact scope, audience token, window, reason, owner,
  expected impact, customer-safe message, and backout plan.
- The audience is re-resolved before work begins; material scope drift needs
  approval and renewed notice. Monitoring continues during maintenance.
- Only the approved, properly notified window is SLA-excludable. Unannounced
  work, newly affected customers, and overruns count as unplanned downtime.
  An unresolved interruption at the scheduled end becomes or joins an outage.
- Emergency maintenance is unplanned by default.

## 6. Compensation (approved; posting gated)

- Downtime measurement, SLA determination, and financial remedy are separate
  owners. Exact qualifying downtime is always calculated.
- Compensation is created only when an effective contract or regulatory
  policy requires it. An NCC-classified major outage exceeding 24 hours
  creates a mandatory compensation obligation/review.
- The remedy is calculated and previewed automatically but requires approval
  before posting financial value until the exact ISP formula is confirmed.
- Contractual defaults where expressly adopted: postpaid — proportional
  credit against the base recurring charge; prepaid/time-based — validity
  extension equal to qualifying downtime. Multipliers, minimums, and caps
  come from the effective policy, not the flat `credit_percent`.
- One remedy per affected subscription with a consolidated customer
  notification. Posting is idempotent against an immutable calculation
  snapshot; corrections are additional or reversing adjustments.

## Ownership inventory: old → new

- Exposure (blast radius): stays `network.outage_impact`; every current UI
  figure remains labelled potentially affected until the resolver exists.
- Incident lifecycle: stays `network.outage_lifecycle`; gains immutable scope
  revisions (the mutable root remains the latest projection).
- `SlaProfile.credit_percent` and read-time `topology.customer_availability`:
  become display-only evidence; `customer.service_level` supersedes them
  after a shadow-comparison phase with a discrepancy review, an atomic
  admin-display cutover, and retirement of the old derivation. Two operational
  admin scores must never coexist. Customer exposure is out of scope.
- Customer outage sending: moves from `network.outage_notifications` (and its
  `network.outage_auto_notify` trigger) to `network.outage_communications`.
  The old path never had a restoration message, resolved its audience from
  `connection_status.assess` rather than the impact resolver, and messaged per
  subscription instead of per customer. It stays callable until the new owner
  is armed, then refuses; it is removed after one full armed incident cycle.
- `OutageIncident.crm_ticket_id` placeholder: superseded by typed
  incident-ticket links (one canonical infrastructure ticket, many complaint
  tickets). Network recovery never auto-closes tickets or work orders.
- `operations.sla_escalation` (internal response escalation) and
  infrastructure `AvailabilitySnapshot` (element health) are explicitly not
  overloaded.

## Delivery and cutover

Slices follow the amended programme sequence (scope revisions → ticket and
watcher composition → communications → impact resolver and ledger → SLA
scoring → maintenance → UI integration), each with registry `ServiceContract`
entries, migrations, replay/concurrency tests, and architecture guards.
Historical backfill only materialises intervals supported by exact evidence;
everything else is labelled estimated or unavailable. Customer-visible SLA is
not part of this programme phase. Admin cutover is gated until the applicable
subscription cohort has sufficient evidence coverage and every unexplained
discrepancy is reviewed.

### Admin SLA shadow-review page contract

- Screen: restricted customer/subscription SLA shadow review; detail/evidence
  page for customer-operations staff holding `customer:read`.
- Job: compare the latest immutable candidate revision with legacy availability
  over the same closed Africa/Lagos calendar month, without treating either
  comparison column as a cutover approval.
- Authority: `customer.service_level` owns period validation, candidate
  selection, discrepancy classification, blockers, and display-authority
  decision. `topology.customer_availability` supplies labelled legacy evidence;
  `control.settings_spec` supplies the inert selector.
- First viewport: admin-only warning, exact customer/subscription/period,
  effective display authority, candidate completeness, legacy coverage,
  discrepancy class, delta when comparable, and cutover blockers.
- Actions: period selection and evidence drill-down only. There is no approve,
  arm, compensate, export, send, or customer-publish action in this slice.
- States: missing candidate, incomplete candidate, unavailable candidate,
  unavailable legacy evidence, exact match, and unreviewed difference are
  distinct. Legacy evidence is comparable only when every resolved path
  element has a trustworthy daily snapshot for every day in the period;
  missing coverage is never uptime. No tolerance or cause is guessed.
- Exposure: the route and template exist only under the admin application and
  require `customer:read`; architecture tests forbid customer portal/API
  imports or routes.

## Verification

- `tests/test_service_impact_contracts.py` pins the shared vocabulary,
  invariants, and approved constants (recovery hold, notice period,
  timezone).
- Each implementation slice adds owner behaviour, replay, idempotency, and
  boundary tests before any consequence goes live.
