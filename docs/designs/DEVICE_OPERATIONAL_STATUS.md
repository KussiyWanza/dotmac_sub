# Binary device operational lifecycle

Status: implemented contract

Owner: `network.device_state`

## Decision

The public device operational result has exactly two values:

- `working`: current verification confirms operation.
- `not_working`: verification confirms failure, administrative lifecycle
  prevents operation, or the verification lifecycle cannot currently confirm
  operation.

Fresh, stale, retry-pending, unknown, degraded, and maintenance are not public
device states. Observation age is an internal input that makes verification
due. The permanent verifier attempts confirmation and the resolver publishes
one binary result.

The reason is required because `not_working` does not always mean physical
failure:

- `observed_not_working`, `ping_failed`, and device-specific offline reasons
  identify confirmed negative evidence.
- `verification_not_started`, `verification_expired`,
  `verification_inconclusive`, `verification_path_unavailable`, and
  `verification_error` identify inability to verify.
- `admin_maintenance`, `admin_decommissioned`, and `admin_retired` identify an
  administrative lifecycle cause and suppress operational alarms.
- `active_trigger`, `health_degraded`, and `poll_*` identify a separate
  impairment while the device remains `working`.

Only confirmed negative device evidence is alarming. Verification
infrastructure failures are operationally `not_working` but must not be
reported as proof that the physical device is down.

## Inputs and precedence

Administrative lifecycle, verification-path coverage, and timestamped
observations remain distinct authoritative inputs. The resolver applies this
precedence:

```text
administrative maintenance/decommission/retirement
  -> not_working(admin_*)

no usable verification path
  -> not_working(verification_path_unavailable)

missing, expired, or inconclusive confirmation
  -> not_working(verification_not_started|expired|inconclusive)

current negative observation
  -> not_working(confirmed device-specific reason)

current positive observation with impairment
  -> working(active_trigger|health_degraded|poll_*)

current positive observation
  -> working(confirmed device-specific reason)
```

For core `NetworkDevice` rows, `last_ping_at` and `last_snmp_at` are the
observation timestamps used for freshness. `live_status_at` has a different,
non-observational meaning: it records when the derived `live_status` entered
its current value so debounce and availability-history consumers can measure
state dwell. A continuously working device therefore retains its transition
timestamp while each successful native poll renews its verification evidence.

Per-device verification sources differ:

- core devices: native infrastructure polling and warmed live observations;
- OLTs: direct ping/poll observations, with linked monitored-device evidence as
  a fallback;
- ONTs: current OLT status, ACS informs, and last-seen observations;
- linked NAS devices: their canonical `NetworkDevice` observation;
- unlinked NAS devices: explicit health evidence; administrative `active`
  alone is not proof of operation;
- routers: synchronized router state plus current last-seen evidence;
- CPE without an approved verifier: `not_working(verification_not_configured)`.

## Permanent verification

The following lifecycle inputs and projections cannot be disabled:

- native infrastructure polling and topology warming;
- Huawei ONT status polling and TR-069 runtime reconciliation;
- monitoring-path coverage refresh;
- monitoring inventory synchronization;
- channel-health observation; and
- device-projection reconciliation.

Settings may tune cadence, thresholds, windows, and bounded retry. They cannot
disable verification or projection repair. Migration
`416_binary_device_operational_lifecycle` removes the retired observer controls,
re-enables their scheduled tasks, backfills the projection, and adds the
database vocabulary constraint.

### Reserved execution capacity

Native infrastructure polling and topology status warming run on the dedicated
`monitoring` Celery queue, consumed by `celery-worker-monitoring`. Bulk
collection remains on `ingestion`; in particular, the scheduled Huawei OLT MAC
harvest dispatches one independently locked, time-limited, and retryable task
per OLT on that queue.

This is a transport-capacity boundary, not a second status decision path.
`runtime.infrastructure_polling` still owns reachability observations and
`network.device_state` still owns their interpretation. A slow inventory or OLT
SSH collection must not delay verification until devices appear
`not_working(verification_not_started|verification_expired)`.

## Projection and UI

`network.device_projection` is the sole writer of the rebuildable
`device_projections` read model. It persists only `working` or `not_working`.
`refreshed_at` is repair evidence: it can trigger reconciliation and support
diagnostics, but UI/API clients do not turn it into freshness, pending, or
unknown device states.

Web, API, mobile, maps, dashboards, ONT/OLT inventory, wallboards, filters, and
KPIs consume the shared binary presentation:

| Value | Label | Tone | Icon |
| --- | --- | --- | --- |
| `working` | Working | positive | check |
| `not_working` | Not working | negative | x |

The UI may show the owner-supplied reason and raw timestamped observations at
investigation/evidence depth. It must not derive a different status from them.
Administrative lifecycle and impairments remain separately labeled facts, not
extra operational badges.

## Drift repair and enforcement

- `reconcile_device_projections` deterministically rebuilds binary status from
  authoritative inputs and prunes orphan rows.
- the database check constraint rejects any projection value outside the
  binary vocabulary;
- architecture tests reject public freshness/retry fields and labels;
- architecture tests require every verification task to remain permanent and
  literally enabled; and
- focused resolver tests cover positive, negative, unavailable-verifier,
  impairment, administrative, and per-device-type cases.
