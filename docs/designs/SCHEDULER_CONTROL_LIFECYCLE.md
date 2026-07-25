# Scheduler control and lifecycle boundary

Status: implemented contract

Owners:

- `control.settings_spec` owns schema, default, coercion, and
  database-authoritative resolution for operational scheduler booleans.
- `control.feature_registry` owns optional capability admission.
- `scheduler.registry` owns task registration and cadence synchronization.
- Each named lifecycle or projection reconciler owns progression and repair.

## Rule

A scheduler is an adapter, not a state-machine owner. Settings may tune cadence,
thresholds, windows, and retry bounds. A mutable capability control may reject
new intent at the owning admission boundary. Once durable intent or derived
state exists, its owner must continue to drain or repair it, or record an
explicit terminal/deferred outcome with evidence.

Scheduler enablement booleans therefore have only two valid resolution paths:

1. a canonical optional capability key through
   `control_registry.is_enabled`; or
2. a boolean key listed in `SCHEDULER_BOOLEAN_SETTING_KEYS` with a complete
   `SettingSpec`, resolved through `_scheduler_setting_enabled`.

Ad-hoc environment → database → default merging is forbidden. An unregistered
boolean fails closed during schedule construction and the architecture guard
makes the missing registration a build failure.

Scheduler cadence, thresholds, windows, retry bounds, and other scalar tuning
must have a typed `SettingSpec` and resolve through `resolve_integer` or
`resolve_string`. Their environment variables are bootstrap inputs only:
`seed_scheduler_runtime_settings` materializes a missing database row once and
never overwrites an existing operator decision. The broker and result-backend
URLs are deployment transport configuration and remain explicit environment
inputs; they are not mutable domain settings.

Permanent lifecycle and repair tasks have no enablement control. They are
registered with `enabled=True`, listed in `PERMANENT_LIFECYCLE_TASKS`, and
cannot be disabled, renamed, or deleted through scheduler operations.

The permanent set includes:

- customer financial lifecycle and account-access reconciliation;
- active-session and device-login security projection;
- accepted provisioning-compensation retry work;
- expiry cleanup for existing FUP enforcement;
- admitted campaign and campaign-sequence drainage; and
- canonical device-projection repair;
- monitoring-path coverage observation;
- monitoring inventory projection repair; and
- channel-health observation.

Usage rating, metering, new FUP evaluation, and bundle notifications retain an
admission control. That control prevents new usage/FUP decisions; it does not
prevent expiry cleanup for enforcement already recorded.

## Campaign admission and drainage

`communications.campaigns` owns the scheduled-campaign admission decision.
`comms.campaign_processing_enabled` is checked when a caller creates a
scheduled campaign or moves an existing campaign into `scheduled`. It is not
read by the scheduler or task adapter.

Migration `415_permanent_lifecycle_drainage` fails closed at cutover: if the
database does not contain a true campaign-admission decision, existing
`scheduled` campaigns move to the explicit `paused` state with migration
evidence. Campaigns already `sending` and accepted sequence work continue to a
terminal outcome. Operators can resume paused campaigns only through a new
admission after enabling the control.

## Device-projection repair

`network.device_projection` is the sole writer and repair owner for
`device_projections`. Canonical device identity, monitoring observations, and
resolved operational state remain authoritative inputs. The materialized table
is a rebuildable SQL read model with `refreshed_at`, natural-key convergence,
and orphan pruning.

The reconcile task is permanent because disabling it would let the admin device
view diverge indefinitely from canonical network state. The
`network_monitoring.device_projection_reconcile_enabled` setting and
`DEVICE_PROJECTION_RECONCILE_ENABLED` environment control are retired.
`device_projection_reconcile_interval_seconds` remains a cadence input with a
60-second floor.

Migration `414_permanent_device_projection` deletes the retired database row,
re-enables any existing scheduled-task row, and intentionally does not recreate
the control on downgrade.

## Binary device verification

`network.device_state` owns the public `working`/`not_working` device result.
Observation age is an internal verification-due input, not a public state.
Coverage, monitoring inventory, and channel-health observations are required
inputs to verification and drift repair, so their tasks are permanent.
Native reachability polling and topology warming also have reserved execution
capacity on the `monitoring` queue; bulk ingestion cannot consume that worker.
The Huawei OLT MAC harvest remains bulk ingestion and fans out into bounded
per-OLT tasks.

Migration `416_binary_device_operational_lifecycle` removes
`monitoring_coverage_enabled`, `monitoring_inventory_sync_enabled`, and
`channel_health_enabled`, re-enables the scheduled tasks, and constrains the
device projection to its binary vocabulary. Their cadence settings remain
tunable.

## Boolean control cutover

The former `_effective_bool` resolver read deployment environment, raw
`DomainSetting`, and a call-site default independently. It is removed.

Registered scheduler-setting environment variables are bootstrap inputs only.
`seed_scheduler_runtime_settings` materializes them when no database decision
exists; an existing database value wins and is not overwritten on restart.
Optional capability tasks use canonical feature keys directly, so retired alias
names no longer route scheduler callers.

Before deployment, verify that the application settings seed has run before
the beat process is cut over and compare the resulting registered database
values with the intended deployment values. To preserve release of previously
scheduled campaigns, materialize `comms.campaign_processing_enabled=true` in
the database before migration `411`; absence is intentionally treated as
closed. This is an operational release gate, not authorization to access or
deploy any production host.

## Enforcement

`tests/architecture/test_scheduler_boolean_control_boundary.py` proves:

- every scheduler setting boolean exactly matches the registered key set and
  has a boolean `SettingSpec`;
- every scheduler feature gate names a real canonical feature control;
- the legacy boolean fallback cannot return; and
- device-projection repair remains permanent and uncontrolled.

`tests/architecture/test_scheduler_lifecycle_drainage_boundary.py` proves:

- every scheduler integer/string resolver call names a registered typed spec;
- live environment/database/default scalar fallback cannot return;
- accepted-work, expiry, security, and projection tasks remain permanent;
- retired compensation and device-login enablement controls cannot return; and
- campaign processing is an owner-level admission decision, never a scheduler
  drainage gate.
