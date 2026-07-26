# Production deployment

`scripts/deploy.sh` is the production deployment owner. It deploys one immutable
GHCR image and keeps the database, proxy handoff, application health, and
rollback boundary in one operation.

## Host contract

- `nginx/selfcare.dotmac.io.conf` is installed and `nginx -t` passes.
- The primary upstream is `127.0.0.1:8001`.
- The deployment-only backup upstream is `127.0.0.1:18001`.
- `.env` contains the production service configuration and approved secret
  references. Secret values are not copied into deployment commands or logs.
- The database backup and deploy locks are writable.

The deployment refuses to start if the running Nginx configuration does not
contain the backup upstream.

## Release sequence

1. Verify the image exists and its OCI revision matches the requested SHA tag.
2. Back up the database.
3. Run candidate-image pre-migration state checks against the target database.
4. Pin the immutable image and revision.
5. Apply `alembic upgrade heads`, retrying bounded PostgreSQL lock timeouts.
6. Verify registered schema contracts and reject every invalid or unready
   user-schema index.
7. Verify every enabled integration installation pin resolves to a current or
   bounded historical definition in the new image. Unavailable pins block
   replacement; historical pins are reported for explicit adoption.
8. Verify that an enabled `crm.ticket_pull` control has exactly one enabled
   `crm.ticket_observation.v1` binding and one active job bound to it. Complete
   the reviewed
   [`CRM_TICKET_CAPABILITY_CUTOVER.md`](CRM_TICKET_CAPABILITY_CUTOVER.md)
   procedure with the candidate image before deployment when this gate fails.
9. Start and health-check the new application image on `127.0.0.1:18001`.
10. Recreate the primary application and workers. Nginx uses the healthy
   candidate while the primary port is unavailable.
11. Verify the primary image has no source-code bind mount and wait for its
   health endpoint.
12. Require every declared Celery worker to remain restart-free and answer a
   node-specific ping, and require Celery Beat to remain running without
   restarts, across a bounded stabilization window.
13. Gracefully drain the candidate and retain the configured rollback images.

The candidate runs the same image, environment, and database schema as the
primary. It is bound to localhost and exists only for the handoff window.

## Service-extension duplicate reconciliation

Migration 417 requires one
`(service_extension_id, subscription_id)` entry. The deployment owner runs the
candidate image's read-only check before Alembic:

```bash
python -m scripts.migration.reconcile_service_extension_duplicates --check
```

If it reports candidates, do not use direct `DELETE` or `UPDATE` SQL. Preview
the complete cohort with the candidate image, review the exact fingerprint and
dispositions, then apply through `financial.service_extensions`:

```bash
python -m scripts.migration.reconcile_service_extension_duplicates

python -m scripts.migration.reconcile_service_extension_duplicates \
  --apply \
  --fingerprint <reviewed-sha256> \
  --effective-at <iso-8601-with-timezone> \
  --idempotency-key <stable-key> \
  --actor <operator-id> \
  --reason <reviewed-reason> \
  --preserve-chained-entitlement
```

Apply collapses exact copies and preserves any approved chained interval as a
separately audited corrective extension. It does not shorten the current
customer billing anchor. Run `--check` again and require zero candidates before
retrying the guarded deployment.

## Migration/index invariant

Concurrent PostgreSQL index creation is not complete until the catalog reports
both `indisvalid` and `indisready`, and the index definition matches its
checked-in structural contract. A retry must remove an interrupted build before
recreating it; index-name existence alone is not success.

Run the read-only verification independently with:

```bash
docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.migration.verify_schema_contracts

docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.verify_manifest_pins

docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.verify_crm_ticket_readiness
```

## Failure behavior

- Migration, schema verification, unavailable integration-pin, or CRM ticket
  capability-readiness failure occurs before service replacement.
- Candidate startup failure leaves the primary release serving traffic.
- Primary health failure restores the previous image while the candidate
  continues serving, then removes the candidate after the rollback is healthy.
- Celery worker or Beat startup/readiness failure follows the same rollback
  path. A healthy web endpoint cannot make a release acceptable while
  background processing is unavailable.
- Database migrations are forward-only and are not rolled back automatically,
  so every release migration must remain compatible with the previous image.
