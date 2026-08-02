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
| Outage communications | existing communication policy/intents/receipts owners |
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

## 4. SLA policy and scoring (approved)

- Never invent a contractual SLA. With no effective policy, surfaces show
  measured availability plus "No contractual SLA" — no 99.5% default.
  Internal operational targets stay visibly separate from contract.
- Policies are immutable effective-dated versions. Precedence:
  subscription-specific contract → customer/account contract → subscribed
  offer version → internal measurement policy. A mid-period change splits the
  calculation by policy version.
- Default reporting period: calendar month in Africa/Lagos.
- Availability = (eligible seconds − qualifying unavailable seconds) /
  eligible seconds; only active, service-entitled time enters the
  denominator.
- `unknown` monitoring periods make the result incomplete/provisional — never
  silent uptime.
- Third-party/upstream failure is not automatically excluded where Dotmac
  sold end-to-end service; force majeure is reviewed case-by-case.
- Every score stores the policy version, evidence coverage, and interval
  lineage that produced it.

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
  cutover, and retirement of the old derivation. Two displayed scores must
  never coexist.
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
gated until the applicable subscription has sufficient evidence coverage.

## Verification

- `tests/test_service_impact_contracts.py` pins the shared vocabulary,
  invariants, and approved constants (recovery hold, notice period,
  timezone).
- Each implementation slice adds owner behaviour, replay, idempotency, and
  boundary tests before any consequence goes live.
