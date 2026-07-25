# Service-extension activity cutover

This runbook applies migration `417_service_extension_activity_sot`. It does
not authorize production execution; Michael must name the target host first.

## Before migration

1. Take the normal database backup and record the deployment correlation ID.
2. Confirm migration 413 is current.
3. Run the duplicate-evidence preflight:

   ```sql
   SELECT extension_id, subscription_id, COUNT(*) AS row_count
   FROM service_extension_entries
   GROUP BY extension_id, subscription_id
   HAVING COUNT(*) > 1
   ORDER BY extension_id, subscription_id;
   ```

4. If the query returns rows, stop. Do not choose or delete a row
   automatically. Compare the exact previous/resulting anchors and surrounding
   subscription history, prepare an approved repair plan, and retain durable
   evidence of the reviewed decision. The migration intentionally fails while
   duplicates exist.

No lifecycle audit events are backfilled. Historical display continues to use
provenance-labelled aggregate fields.

## Apply

Run the standard migration command for the named environment. Migration 414:

- adds nullable cancellation, idempotency, command, and correlation evidence;
- adds zero-default apply outcome counters; and
- adds unique `(extension_id, subscription_id)` enforcement.

The migration does not rewrite extension status, actor, time, entry, audit, or
event history.

## Verify

Verify the head and schema:

```sql
SELECT version_num FROM alembic_version;

SELECT column_name, is_nullable
FROM information_schema.columns
WHERE table_name = 'service_extensions'
  AND column_name IN (
    'canceled_by',
    'canceled_at',
    'create_fingerprint_sha256',
    'create_command_id',
    'apply_command_id',
    'cancel_command_id'
  )
ORDER BY column_name;

SELECT constraint_name
FROM information_schema.table_constraints
WHERE table_name = 'service_extension_entries'
  AND constraint_name =
      'uq_service_extension_entries_extension_subscription';
```

In a non-production rehearsal, create, apply, and cancel separate extensions
through the admin adapter. Verify one exact entity-linked lifecycle audit and
one aggregate event per transition, then replay each command and verify counts
remain unchanged. Verify a historical row without audit evidence renders only
the labelled creation/apply fallback and no invented cancellation item.

## Rollback

The downgrade removes only migration-414 columns and the uniqueness constraint.
It does not delete extensions, entries, audits, or domain events. Before
downgrading, confirm no deployed application process requires the new typed
contracts; otherwise stop and restore application/schema compatibility first.
