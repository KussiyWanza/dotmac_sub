# CRM ticket capability cutover

This runbook completes the explicit `dotmac.crm` ticket-observation cutover
after migration 377 disabled legacy jobs that had no capability binding.

The operation is not a database patch:

- `integration.installations` adds `crm.ticket_observation.v1`, validates the
  pinned connector and existing configuration, performs a live non-mutating CRM
  connection check, and atomically restores the enabled installation/bindings.
- `integration.jobs` binds and activates the reviewed `Pull CRM Tickets` job.
- `scheduler.registry` remains the only cadence owner. The job stays `manual`;
  the `crm.ticket_pull` control schedules incremental and daily reconciliation.

The two owner commands are independently idempotent. Scheduler and webhook
adapters fail closed between them, so a partial operation cannot start an
unbound background job or silently accept an unexecutable ticket event.

## Preconditions

1. Name and verify the target host.
2. Use an immutable candidate image whose OCI revision matches the reviewed
   release.
3. Confirm the deployed `dotmac.crm` manifest pin is available.
4. Do not retrieve or print CRM secret values. The installation owner resolves
   its existing approved references only inside connection validation.

## Preview

Run the candidate image against the target database without changing state:

```bash
APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-<reviewed-sha> \
docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.reconcile_crm_ticket_capability
```

Review all of the following:

- exactly one production `dotmac.crm` installation;
- its exact installation ID, connector version, and manifest digest;
- exactly one CRM target job named `Pull CRM Tickets`;
- the current binding/job state;
- `eligible: true`;
- the exact preview `fingerprint`.

An enabled `crm.ticket_pull` control with zero enabled ticket bindings and zero
active ticket jobs is the expected pre-cutover production state. Any ambiguity,
unexpected existing binding, different job binding, unavailable manifest, or
unvalidated installation must be resolved before apply.

## Apply

Apply only the reviewed IDs and fingerprint:

```bash
APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-<reviewed-sha> \
docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.reconcile_crm_ticket_capability \
  --apply \
  --installation-id <reviewed-installation-uuid> \
  --job-id <reviewed-job-uuid> \
  --expected-fingerprint <reviewed-sha256> \
  --idempotency-key <reviewed-operation-key> \
  --actor <operator-identity> \
  --reason "<reviewed reason>"
```

The command exits non-zero if the preview changed, connection validation
failed, either owner rejected its exact state, or final readiness is not green.
If the installation step succeeds but job activation fails, do not edit tables.
Run preview again and safely replay the operation with the new reviewed
fingerprint.

## Verify and deploy

The final apply output must report:

- `already_ready: true`;
- one enabled ticket-observation binding;
- one active ticket job bound to that binding;
- `schedule_enabled: true`.

Run the independent deployment gate:

```bash
APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-<reviewed-sha> \
docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.verify_crm_ticket_readiness
```

Then run the normal guarded deployment. `scripts/deploy.sh` repeats this check
before starting the warm candidate.

After deployment, verify:

1. the five-minute pull produces a successful `IntegrationRun`;
2. one CRM ticket read succeeds without `InstallationError`;
3. a reviewed webhook test queues `sync_crm_ticket`;
4. no new `no active CRM ticket capability job configured` or
   `no enabled binding for crm.ticket_observation.v1` errors appear.

## Recovery

If the provider connection becomes unavailable, disable `crm.ticket_pull`
through the canonical control owner before disabling the binding. Use the
installation and job admin owners for lifecycle changes; never direct-edit
`integration_installations`, `integration_capability_bindings`, or
`integration_jobs`.
