# Operations Measurement Strategy

This note defines where Dotmac should use SLA, metrics, and performance
measurement inside the app. The goal is to measure customer experience first,
infrastructure second, and internal mechanics third.

## Current State (updated 2026-07-10)

The Zabbix runtime cutover shipped and is live on prod (PRs #1058, #1069,
#1072): the native infrastructure poller owns device reachability
(ping + SNMP, 300-device capped batches, advisory-locked single-flight),
`live_status` derives from native poll columns for every downstream consumer,
admin interface bandwidth reads VictoriaMetrics (`rate()` over pushed IF-MIB
counters), and expiry reminders are outage-aware via
`customer_service_state`. Zabbix containers are stopped (compose profile
`zabbix`; rollback documented in the cutover memory/PRs).

Division of responsibility, as deployed:

- **Python** answers *what is broken, who is affected, what should we do* —
  lightweight polling plus business interpretation. We deliberately do not
  rebuild Zabbix (no generic item engine, no trigger DSL).
- **Postgres** holds current state, transitions, and customer impact
  (`network_devices` poll columns, `outage_incidents`, `admin_alerts`,
  suppression decisions).
- **VictoriaMetrics** holds graph-heavy time series (bandwidth aggregates,
  `core_interface_{in,out}_octets_total`).

The poller has a dead-man switch: it records a heartbeat and skip streak, and
`admin_alerts` raises (and auto-resolves) findings for a stalled run
(> 3× beat interval), a stuck single-flight lock (repeated
`already_running`), failing VictoriaMetrics counter writes, and stale ping
rows. Zabbix's most important job was noticing silence; this replaces that
for its replacement.

Known coverage gap: ~496 ping-enabled hostname-only devices read
`live_status = unknown` (better than false down, but it limits outage
confidence). Inventory cleanup workstream: backfill mgmt IPs (UISP-linked
first), and audit devices that are hostname-only / no mgmt IP / ping
disabled / SNMP disabled / not mapped to any customer path.

## Measurement Classes

Use three measurement classes consistently:

- SLA: customer-visible commitments such as uptime, repair time, support
  resolution, payment posting correctness, and provisioning completion.
- Metrics: continuous measurements such as latency, counters, error rates,
  freshness, queue depth, payment volume, and topology coverage.
- Performance: operational quality signals such as slow pages, slow jobs,
  degraded devices, failed pollers, and high-impact outage areas.

Do not create separate SLA systems for each module. Feed everything into a few
shared measurement concepts:

- availability
- freshness
- latency
- error_rate
- coverage
- backlog
- drift
- customer_impact

## Existing Primitives

The app already has the core building blocks:

- App request metrics in `app/observability.py`: request count, latency, and
  5xx errors.
- Prometheus collectors in `app/metrics.py`: HTTP, job, billing, Redis,
  bandwidth poller, connectivity, and audit metrics.
- Infrastructure SLA bridge in `app/services/topology/availability_log.py`:
  device `live_status` transitions become uptime downtime intervals.
- Daily availability snapshots in
  `app/services/infrastructure_availability_snapshot.py`: per-device/site/PON
  availability trends.
- Network performance dashboard in `app/services/web_network_performance.py`:
  worst BTS/OLT/AP/PON by uptime, downtime, impact, incidents, and MTTR.
- Support ticket SLA clocks in `app/services/sla_assignment.py`.
- Ticket SLA reports in `app/services/ticket_sla_reports.py`.
- Billing health in `app/services/billing_health.py`: invoice scan coverage,
  payment collapse, stale runners, paid invoices with balances, and enforcement
  drift.
- Topology coverage metrics in `app/services/topology/coverage_metrics.py`.
- Dependency health checks in `app/services/infrastructure_health.py`.
- Root database transaction duration in `app/services/session_hooks.py`, with
  bounded Prometheus histogram/counter series, correlated structured slow-span
  logs, and reviewed warning/critical rules in
  `deploy/observability/database_transactions.rules.yml`.

## Strategic Opportunities

### Customer Service SLA

Measure the full customer experience, not only device uptime.

Add:

- `customer_service_available_ratio`
- `customer_service_degraded_count`
- `customers_under_active_outage`
- `customers_with_open_infra_ticket`
- `customers_suppressed_from_billing_notice`

Sources:

- `customer_service_state`
- topology incidents
- support tickets

### Network SLA

Extend the existing uptime interval model with customer-impact dimensions:

- BTS uptime
- OLT uptime
- AP uptime
- PON availability
- affected subscriber minutes
- MTTR
- incident count
- repeat outage count

The most useful KPI is:

```text
affected_subscriber_minutes = downtime_minutes * affected_subscribers
```

### Billing SLA

Billing needs an internal SLA because billing mistakes directly affect customer
trust.

Add:

- invoice cycle completed within expected window
- payment posting latency
- webhook-to-ledger posting latency
- successful payment but unpaid invoice drift
- prepaid balance enforcement latency
- dunning action correctness

Key metric:

```text
money paid by customer appears correctly on ledger within X minutes
```

### Provisioning SLA

Measure from order/payment/admin action to usable service.

Add:

- subscription activation duration
- ONT provision duration
- Router/RADIUS provisioning duration
- failed provisioning attempts
- pending provisioning age
- active subscription without working RADIUS/ONT state

### RADIUS / Access SLA

This should be added soon after native infrastructure monitoring.

Add:

- RADIUS auth RTT
- accounting update freshness
- pending/resend/timeout counters
- active sessions by NAS
- stale session count
- paid active customer with no active session
- suspended customer with active unrestricted session

### App Performance

HTTP request metrics already exist, but route-level SLOs should be defined for
customer and operator workflows.

Measure p95/p99 latency for:

- customer portal dashboard load
- payment callback/webhook processing
- admin customer detail page
- support ticket creation
- invoice PDF generation
- provisioning action endpoints

### Worker / Job Performance

Standardize job result freshness across critical tasks.

Add for critical tasks:

- last success age
- last failure age
- duration p95
- rows processed
- rows failed
- skipped count
- lock contention count

Billing and topology already use parts of this pattern. Make it reusable.

### Topology Coverage

Coverage tells us whether outage detection can be trusted.

Track:

- active subscriptions with complete network path
- missing ONT link
- missing node
- missing BTS/site
- stale topology source
- devices pollable but not customer-impact mapped

### Field / Support Performance

Support SLA exists, but field execution should be measured more clearly.

Add:

- ticket first-response time
- ticket resolution time
- reopen rate
- repeat ticket rate per customer/site
- tickets suppressed due to known outage
- SLA breach rate by team/person/region
- outage ticket deflection rate

### Infrastructure Dependency SLO

`runtime.infrastructure_health` owns the dependency probes and their status
interpretation. The enabled `check_stale_infrastructure` scheduled task runs the
bounded probe cohort on its configured cadence (300 seconds by default) and
publishes one shared latest-state snapshot. Admin dashboard requests read only
that snapshot; they never run dependency probes. The projection records its
observation time, is fresh for ten minutes, renders older last-known values as
stale, and renders a missing or malformed snapshot as unavailable. Re-running
the scheduled task is the idempotent rebuild path; snapshot age, publication
failure, malformed schema, and the task heartbeat are its drift signals.

Redis transports this bounded projection but is not authoritative for the
underlying dependency state. The full system-health diagnostic page may retain
explicit on-demand probes for deliberate operator investigation; high-frequency
dashboard polling may not.

Export key outputs as metrics:

- `dependency_up{name}`
- `dependency_response_ms{name}`
- `celery_queue_depth{queue}`
- `celery_missing_queue_consumer{queue}`
- `postgres_connection_utilization_pct`
- `redis_circuit_open`
- `victoriametrics_write_failures_total`

## Recommended Priority

1. Push `device_ping_latency_ms` and `device_ping_loss` to VictoriaMetrics from
   the native poller.
2. Add RADIUS health metrics and freshness.
3. Add customer-impact SLA: affected subscriber minutes, customers under outage,
   suppressed billing notices.
4. Standardize critical job heartbeats across billing, monitoring, provisioning,
   RADIUS, and CRM sync.
5. Add provisioning SLA from subscription activation to confirmed service.
6. Export infrastructure health page results as Prometheus gauges.
7. Build one Operations Scorecard page using these measures.

## North Star

The app should answer these questions without manual investigation:

- Are customers online?
- Are paying customers correctly provisioned?
- Are payments posted quickly and accurately?
- Are outages detected and grouped by impact?
- Are teams resolving tickets within SLA?
- Are background jobs healthy and fresh?
- Are graphs and metrics trustworthy?

This is the measurement strategy: customer experience first, infrastructure
second, internal mechanics third.
