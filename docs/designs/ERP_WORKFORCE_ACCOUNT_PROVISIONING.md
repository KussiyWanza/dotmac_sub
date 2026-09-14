# ERP workforce account provisioning

## Decision

Dotmac ERP is the source of truth for new workforce identities. This rollout is
forward-looking: it does not backfill employees that predate enablement.

The ordered onboarding flow is:

1. ERP validates the employee work email and required personal email.
2. ERP creates the Mailcow mailbox and sends activation material to the
   employee's personal email.
3. ERP creates the Nextcloud account and persists the exact Nextcloud user ID.
4. ERP creates the Selfcare staff account with create-only semantics.
5. ERP binds the returned Selfcare user ID to the persisted Nextcloud user ID.

Mailcow provisioning must remain disabled until every downstream step is
deployed and configured. The complete flow is then enabled as one controlled
rollout and verified with one new test employee.

## Selfcare contract

`POST /api/v1/staff-accounts` accepts
`existing_account_policy=reject` for ERP onboarding. If the email already
exists, Selfcare returns `409` and makes no identity, role, activation, or
credential changes. The existing `reconcile` policy remains available for
trusted administrative reconciliation.

After creation, ERP calls
`PUT /api/v1/staff-accounts/{user_id}/nextcloud-talk` with the exact
`nextcloud_user_id`. Selfcare resolves the single enabled default Talk binding
and upserts the mapping. It does not create a room at onboarding time. The
one-to-one room is created lazily on the first notification delivery or an
administrator connection test, then cached and reused.

On permanent ERP deactivation, ERP disables the Selfcare account and calls
`POST /api/v1/staff-accounts/{user_id}/nextcloud-talk/disable`. Selfcare
disables all active mappings for that principal and invalidates cached direct
rooms.

## Authorization

The ERP API key must carry these explicit scopes:

- `rbac:roles:read`
- `rbac:assign`
- `operations:service_team:membership`
- `communications:nextcloud_talk_staff:manage`

The Talk mapping scope is a non-UI-assignable machine scope. It is seeded both
in the RBAC catalog and by a deployment migration so a green deployment cannot
leave the endpoint unreachable. Endpoint dependencies enforce it on both map
and disable operations.

## Reconciliation and failure handling

Every step is retryable and idempotent around persisted provider identifiers.
For create-only requests, Selfcare records the stable ERP command reference as
Party external-reference provenance in the account-creation transaction. An
exact replay returns the originally created UUID without reconciling mutable
fields. ERP records
that UUID immediately after successful creation and reuses it on later
reconciliation. A true create-only conflict is surfaced for review rather than
silently claiming or changing an existing account. A missing enabled Talk
binding fails closed with `503`; malformed mappings return `422`; missing staff
accounts return `404`.

ERP reconciliation repairs incomplete mappings after transient failures. No
invitation is sent until Mailcow exists, and no Selfcare/Talk mapping is
attempted until ERP has the authoritative Nextcloud user ID.
