# Operational evidence and retry

Status: implemented contract for the production-observability follow-up

Owners:

- Source collectors and integration runtimes own observations.
- `observability.recording` owns bounded task/collector result facts.
- `scheduler.registry` owns whether and when scheduled work is expected.
- `integration.installations` owns executable capability binding/configuration.
- `ui.operational_evidence_projection` owns the operator-facing composition.
- The task or collector owner, never the UI, owns retry timing.

## Operator contract

An operational screen must not ask an operator to interpret one overloaded
label such as `healthy`, `degraded`, `unreachable`, `stale`, `unknown`, or
`retrying`. Those terms mix different questions and can turn missing telemetry
into a false outage.

The projection answers these questions separately:

1. **Should this run?** Effective administrative enablement, eligibility, and
   cadence.
2. **What was observed?** The exact bounded result: completed counts, timeout,
   rejected authentication, no route to host, or no observation yet.
3. **When was it observed?** Source timestamp and last successful evidence.
4. **What does it affect?** Telemetry, inventory freshness, portal mirror data,
   or proven customer service impact. A telemetry gap is not service-down proof.
5. **What happens next?** Exact automatic retry time, next scheduled run, or
   operator repair action.

`up` or `down` may be shown only when current authoritative evidence directly
answers reachability for that subject. An administrative setting, record
existence, stale cache, missed poll, or collector transport failure cannot
create that claim.

## Retry contracts

### Bandwidth poller

Each active, credential-complete MikroTik NAS has an independent connection
state. Failed attempts retain safe error classification, consecutive failures,
last attempt, last successful poll, affected live-bandwidth mappings, and next
attempt. Exponential backoff is bounded at fifteen minutes. The poller remains
an observer and never retires or disables a NAS.

`network.live_bandwidth_observations` owns the subscription-scoped live
projection. Normal admin and customer charts open its SSE stream; they do not
poll RouterOS directly or choose collection retry cadence. A connected stream
refreshes the poller's ephemeral demand evidence, VictoriaMetrics is the
preferred observation projection, and either source must carry an observation
timestamp no older than two minutes. When neither source is fresh, the stream
reports `has_sample=false` and does not claim that a zero value is live.

The direct RouterOS read is retained only as an explicit operator diagnostic.
The owner materializes a typed subscription/NAS target, ends the clean database
read transaction, and then acquires a shared per-NAS single-flight
claim before network I/O. The pinned RouterOS client applies a fifteen-second
socket timeout, and the admission claim outlives that bound. Missing distributed
coordination fails closed. Its
public outcome excludes PPPoE login, framed address, caller ID, credentials,
and interface names that may embed customer identity.

### TR-069 inventory

The complete ACS pass is idempotent. A soft timeout, unexpected failure, or
partial server pass records duration, server counts, freshness, consecutive
failures, and next attempt, then retries after 1, 5, and 15 minutes. The normal
scheduled pass remains the recovery backstop. A partial pass is not reported as
success.

### CRM operational observation

Every `crm.operational_observation.v1` call records a redacted success/failure
receipt. Capability readiness comes only from one executable binding: enabled
installation and binding, exactly one default when ambiguous, current validated
configuration, and matching deployed manifest/version. Installation state and
transport evidence remain separate. The quote-mirror reconciler is the retry
backstop while native quote read/write cutover is incomplete.

## Database diagnosis

OLT running-config reads materialize an immutable ONT/OLT transport target and
close the database read transaction before Redis or SSH I/O.

CRM subscriber session projection uses a database-ranked one-row-per-
subscription query supported by
`ix_radius_accounting_sessions_subscription_latest`; it never loads complete
accounting history into Python.

Schema drift and idle-transaction failures emit structured, redacted evidence:
SQLSTATE, safe missing relation/column identifier, stable statement fingerprint,
request ID, and nearest application caller. SQL text, parameters, results,
credentials, and customer data are never logged. Transactions lasting at least
thirty seconds emit a request-correlated duration span.

## UI cutover

The NOC page shows the three operational evidence checks and exact per-router
collector failures. Installed Integrations shows the last observed runtime
result and the CRM capability contract instead of generic health badges.
Templates do not calculate freshness, classify failures, or decide retries.

The fleet-wide `network.device_state` owner now exposes only
`working`/`not_working`. Collector freshness and retry evidence remain internal
verification inputs; templates consume the owner result and reason without
creating another state.
