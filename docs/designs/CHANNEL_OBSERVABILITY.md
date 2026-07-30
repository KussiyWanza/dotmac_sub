# Channel & Webhook Observability

**Owner:** `observability.channel_health_contracts` (policy),
`communications.team_inbox` (facts), and Alertmanager on `dotmac-observe`
(consequences)
**Status:** Implemented in Sub; production rule/scrape integration uses the
existing Dotmac Observability control plane
**Motivates:** the 2026-07-18 crm.dotmac.io inbox flood

## The incident this exists to catch

A full-stack restart flushed two backlogs through two ingestion paths at once,
and nothing alerted. The worker had been silently down for hours; the only
symptom was the delayed flood. **The dangerous failure was the silence, not the
flood** — so the first-class signal here is *a sensitive channel going quiet*,
and every failure mode below is a real one this class of incident exhibits.

## How Sub's inbound actually works (and how it differs from the incident)

The incident was on the CRM stack, whose email path polled IMAP with a
last-seen-UID cursor and whose webhooks *enqueued* for a worker. Sub is
different, and the design must match Sub, not CRM:

- **Inbound is entirely push.** WhatsApp, Messenger/Instagram, and CRM arrive
  as HMAC-verified webhooks (`app/api/inbox_webhooks.py`,
  `meta_inbox_webhooks.py`, `crm_webhooks.py`); when its explicit deployment
  profile is active, email arrives over the dedicated SMTP runtime
  (`app/team_inbox_smtp.py`), which delegates ingestion to
  `app/services/team_inbox_smtp_inbound.py`. There is no poller and no cursor,
  so the incident's cursor-reset re-ingestion cannot occur here.
- **Webhooks are processed inline**, not enqueued — the route calls
  `team_inbox_channel_receive.receive_inbound_channel` and commits in-request.
  So Sub has no "worker stalls, queue drains in a burst" mode for inbound
  either. Its equivalent failure is inline latency: if processing slows, the
  provider times out, retries, and eventually drops — silent inbound loss.
- **Dedup is a DB partial-unique index** on `(channel_type,
  external_message_id) where direction='inbound'`. Redelivery is suppressed at
  write time, so a provider retry storm shows up as a spike in *suppressed*
  inbound, not as duplicate rows.

The push model trades the incident's two failure modes for three of its own,
which is what this design instruments.

## Authoritative channel health contracts

`network_monitoring.channel_health_contracts` is the database-authoritative
registry. `app.services.channel_health_contracts` validates and interprets it.
Every supported external `InboxChannelType` must appear exactly once. A channel
is either enabled with a complete enforceable policy or disabled with a written
reason; a missing or malformed entry makes the snapshot `error` and pages
`ChannelHealthContractInvalid`.

Each contract names the channel fact owner, natural/synthetic/hybrid monitoring
mode, active ISO weekdays and local-time window, maximum quiet period,
synthetic-probe maximum age where applicable, severity, and runbook. Business
windows are evaluated in the contract timezone before metrics are published,
so Prometheus consumes policy facts instead of maintaining parallel thresholds.
Inactive-window time is not charged as silence. A true 24x7 contract never
resets at midnight.

The checked-in defaults are enforceable policy templates, not inferred
production activation:

| channel | mode | active window | maximum age | severity |
| --- | --- | --- | --- | --- |
| email | synthetic | 24x7 | verified probe: 30m | critical |
| whatsapp | natural | daily 07:00–23:00 WAT | quiet: 30m | critical |
| facebook_messenger | natural | daily 07:00–23:00 WAT | quiet: 4h | warning |
| instagram_dm | natural | daily 07:00–23:00 WAT | quiet: 4h | warning |
| chat_widget | natural | daily 07:00–23:00 WAT | quiet: 1h | critical |

Defaults explicitly disable each channel because supported code is not proof
that an environment has credentials, routing, or a live upstream subscription.
Activation is a reviewed update to the registry after the channel's deployment
gate passes. Once enabled, the declared warning or critical alert is enforced
immediately; there is no shadow or ticket-only phase.

## Signals

Four signal groups. Two are web-process Counters (webhook and dedup events
happen in the web process that serves `/metrics`); two are worker-produced
snapshots exported through the existing `_ObservabilityStateCollector`
(`app/services/observability.py` `publish_state_snapshot` → `app/metrics.py`),
which exists because a gauge set in a Celery worker is invisible to the web
process.

### 1. Channel ingestion freshness — the dead-man's-switch

Worker snapshot, domain `channel_ingestion`. Per channel scope (`whatsapp`,
`email`, `facebook_messenger`, `instagram_dm`, `chat_widget`):

| signal | meaning | alert |
| --- | --- | --- |
| `seconds_since_last_inbound` | raw age of the newest natural inbound message | context only |
| `inbound_count_15m` | inbound rows written in the last 15 min | context for the silence alert; also a flood ceiling |
| `monitoring_active` | enabled contract is inside its active window | gates every silence alert |
| `silence_age_seconds` / `max_quiet_seconds` | policy-adjusted natural silence and its limit | natural dead-man's-switch |
| `synthetic_age_seconds` / `synthetic_max_age_seconds` | verified end-to-end probe age and its limit | low-volume channel dead-man's-switch |
| `natural_required` / `synthetic_required` | contract monitoring mode | selects the applicable alert |
| `severity_critical` | contract consequence level | selects warning or critical rule |

This is the signal the incident was missing. It fires when a channel stops
producing — the earliest warning that an endpoint or the SMTP intake is down,
before anyone notices missing conversations. It is channel-shaped, so a per-
channel contract lets low-volume email use a synthetic proof while high-volume
WhatsApp uses natural traffic without duplicating policy in Prometheus.

### 2. Webhook receipt, latency, and errors

Web-process instruments, incremented in each webhook route:

- `sub_webhook_events_total{provider, event, outcome}` — `outcome ∈ {accepted,
  rejected, error}`. `rejected` is a failed HMAC/verification; `error` is an
  unhandled processing failure.
- `sub_webhook_processing_seconds{provider, event}` histogram — because Sub
  processes inline, this latency *is* the deliver-or-drop margin. Rising
  latency predicts provider-side drops before they happen.

A rising `error`/`rejected` rate is the only in-app signal that we are losing
deliveries the provider still believes it made.

### 3. Inbound redelivery pressure

Web-process Counter `sub_inbound_dedup_suppressed_total{channel}`, incremented
where the receive path finds an existing `external_message_id`. A healthy
baseline is near zero. A spike is the Sub-native signature of a provider retry
storm — the push-model analogue of the incident's flood — visible without any
duplicate ever reaching an agent's inbox.

### 4. Async queue depth and worker liveness

Worker snapshot, domain `celery_queues`. Sub routes async work across several
Redis queues (`celery`, `billing`, `ingestion`, `crm`, `tr069`, `acs`,
`bandwidth`, `nin`). Per queue scope:

| signal | meaning | alert |
| --- | --- | --- |
| `queue_depth` | `LLEN` of the Redis queue list | sustained growth = a worker not draining |

Inbound is inline so this does not guard the inbox directly, but it guards
every other operational path (billing runs, provisioning, outbound
notifications) against the exact worker-stall the incident hinged on. It reads
Redis `LLEN` only — bounded, no broker introspection. (Head-of-queue task age
would need decoding the broker's message envelope; depth alone is the reliable
signal and is what the producer emits.)

## Producer

One scheduled beat task, `observe_channel_health`, runs about every 60s and
publishes the two worker snapshots in a single pass: natural/probe freshness,
contract signals, and 15-minute natural volume per channel, plus one `LLEN` per
queue. It writes no business state and calls no transport. Synthetic messages
are produced separately by the dedicated SMTP runtime and committed/verified
through the team-inbox owner, keeping the observer clear of the
consent/transport-ownership boundary
(`tests/architecture/test_communication_eligibility_ownership.py`).

## SMTP runtime and deployment gate

`app.team_inbox_smtp` is the dedicated process supervisor for email intake. It
starts exactly one `team_inbox_smtp_inbound` controller, exits if the controller
dies, and shuts it down on SIGTERM/SIGINT. It is intentionally not attached to
FastAPI lifespan: multiple web workers or reload processes would compete for
the same SMTP port and blur runtime ownership.

The `team-inbox-smtp` Compose service is behind the `smtp-inbound` profile and
publishes its listener on host loopback only. A host MTA owns public SMTP
security and relays only the configured
`TEAM_INBOX_SMTP_INBOUND_RECIPIENTS` to that port. The runtime refuses to start
unless the listener is explicitly enabled and the recipient allowlist is
non-empty.

Deployment has two separate gates:

1. `make prod-smtp-inbound-up` starts or recreates the single listener.
2. `make prod-smtp-inbound-probe` submits a synthetic email through the
   canonical outbound email transport and proves it returned through the real
   inbound route before the ingestion owner committed the marked inbox row.
   Configure a dedicated `TEAM_INBOX_SMTP_PROBE_RECIPIENT` route that does not
   auto-assign to an agent, plus an `observability_smtp_probe` activity sender
   mapping when the default sender must not be used.

The Compose health check is deliberately narrower: SMTP `NOOP` proves socket
readiness without writing inbox data. After cutover, the same runtime submits a
verified end-to-end probe every
`TEAM_INBOX_SMTP_PROBE_INTERVAL_SECONDS` (15 minutes by default). Only the exact
random Message-ID generated and verified by the runtime is marked as a probe;
a sender-controlled header cannot hide natural traffic from freshness. The
runtime allows up to `TEAM_INBOX_SMTP_PROBE_TIMEOUT_SECONDS` (two minutes by
default) for the outbound/MX round trip, and the email contract pages if no
verified probe lands within 30 minutes.

### Email

Enable the email contract only after the SMTP profile, recipient allowlist,
dedicated non-auto-assigned probe route, canonical outbound sender, host-MTA
relay, `NOOP` health check, and manual end-to-end probe all pass. Continuous
synthetic probing then proves outbound SMTP, the inbound relay, listener,
parser, routing, canonical inbox write, and readback.

### WhatsApp

Enable the WhatsApp contract only after the canonical comms settings contain
provider credentials, phone identity, webhook URL, app secret, and verify
token, and Meta webhook verification succeeds. Natural inbound traffic is the
dead-man's-switch during the declared active window.

### Meta social

Messenger, Facebook comments, Instagram direct messages, and Instagram comments
have separate contracts even though they share Meta signature settings. Enable
each only after its page/account subscription and canonical inbound producer
are verified. Outbound public-comment replies alone do not activate the comment
health contracts; one active platform or message type never masks silence on
another.

### Chat widget

Enable the chat-widget contract only after the CRM bridge/configuration is
active and an authenticated customer or reseller session can create a native
inbox message end to end.

## Production integration with Dotmac Observability

Sub already exposes `/metrics` (`app/main.py`) and runs
`dotmac_sub_victoriametrics` + promtail on prod; `dotmac-observe` runs
Prometheus/Alertmanager/Grafana/Loki as Dotmac's production observability
control plane. This feature does not introduce another monitoring stack.
Integration must follow the existing production deployment path:

1. Verify the existing Sub target, labels, and ingestion path in Dotmac
   Observability. Reuse that path; do not create a duplicate scrape.
2. If the existing target does not yet carry Sub's `/metrics` series, extend
   that established scrape/federation/remote-write configuration.
3. Load `deploy/observability/channel_observability.rules.yml` through the
   existing production rule deployment.
4. Route these alerts to the established on-call receiver.

The key-auth rejection encountered during the incident was an access/session
issue, not evidence that the production observability platform or Sub
integration was absent.

## Alert rules

See `deploy/observability/channel_observability.rules.yml`. Warning and critical
rules evaluate the same authoritative registry signals. Separate natural and
synthetic rules preserve failure meaning; missing/stale observer metrics and an
invalid registry are critical because they make silence evaluation blind.
