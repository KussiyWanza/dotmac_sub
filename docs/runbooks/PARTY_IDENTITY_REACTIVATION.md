# Reviewed Party identity reactivation

Status: operator runbook

Owner: `party.identity_reactivation`

Use this workflow only after an attributable administrator has resolved the
identity evidence that caused one exact Party to be quarantined. Reactivation
does not create or activate a login, grant a role, merge identities, repoint a
principal, or change service-team membership.

## Preconditions

1. Name the deployment target and confirm it is authorized for production work.
2. Confirm the Party UUID from the canonical principal binding, never by name or
   email inference.
3. Run the read-only check and record the exact `party_type`, `party_status`, and
   `updated_at` value. The status must be `quarantined`.
4. Record the approving active SystemUser UUID, aware review timestamp, and a
   meaningful reason explaining how the ambiguity was resolved.
5. Use a fresh command UUID. Preserve it for an exact retry.

```bash
poetry run python -m scripts.migration.reactivate_party_identity \
  --check \
  --party-id PARTY_UUID
```

## Execute

The candidate containing this owner must already have passed the prescribed
validation and immutable staging gate before it is deployed to production.

```bash
poetry run python -m scripts.migration.reactivate_party_identity \
  --execute \
  --party-id PARTY_UUID \
  --expected-party-type person \
  --expected-updated-at UPDATED_AT_FROM_CHECK \
  --approved-by-user-id APPROVING_SYSTEM_USER_UUID \
  --reviewed-at AWARE_REVIEW_TIMESTAMP \
  --reason 'Reviewed explanation of the resolved ambiguity' \
  --command-id STABLE_COMMAND_UUID
```

The command locks the Party, requires the exact expected type and timestamp,
accepts only `quarantined -> active`, clears the obsolete quarantine reason,
and commits the status, PII-free audit evidence, and versioned domain event in
one owner transaction. An exact replay returns `replayed`; changed evidence or
state fails closed.

## Verify

Run `--check` again. Confirm `party_status` is `active`, then verify the owning
consumer (for example the service-team dropdown) resolves the principal. Never
repair a consumer projection by bypassing Party status.

## Failure and rollback

Do not use direct SQL or ORM writes. A stale timestamp, unexpected Party type,
active Party without matching replay evidence, or merged/archived status
requires a fresh identity review. If reactivation was incorrect, use the
existing `party.registry.quarantine_party` path with a new attributable reason;
do not rewrite or delete the reactivation audit/event.
