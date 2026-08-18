# Field location retention

The `operations.field_location_retention` owner removes detailed technician GPS
pings 30 days after the server-owned `received_at` time. Celery runs one locked
batch of at most 10,000 rows hourly. Current `FieldTechPresence` and work-order
evidence are not deleted.

## Signals

- `observability_snapshot_age_seconds{domain="field_location_retention"}`:
  cross-process task heartbeat.
- `observability_snapshot_status{domain="field_location_retention",status=...}`:
  latest successful, degraded, or failed state.
- `observability_state{domain="field_location_retention",signal="deleted_rows"}`:
  rows deleted by the latest run.
- `observability_state{domain="field_location_retention",signal="batch_limit_reached"}`:
  whether the latest run exhausted its transaction bound.

Structured events are `field_location_history_retention_completed`,
`field_location_history_retention_failed`, and
`field_location_history_retention_backlog_detected`. Logs and durable audit/event
evidence contain counts and cutoffs only, never coordinates.

## Response

1. Confirm Celery beat contains `field_location_history_retention` and a worker
   registers `app.tasks.field_location_retention.prune_field_location_history`.
2. Inspect the structured failure log and PostgreSQL health. Let automatic
   retries complete before manually dispatching another run.
3. For a backlog alert, compare deleted counts over successive hourly runs.
   A steady 10,000 means cleanup is active; a falling count means it is draining.
4. Do not increase the batch limit or run ad-hoc SQL deletion without reviewing
   lock duration, WAL growth, replica lag, and the exact cutoff.
5. Escalate to field operations and privacy if no successful run occurs within
   three hours or the batch cap remains reached for three consecutive runs.
