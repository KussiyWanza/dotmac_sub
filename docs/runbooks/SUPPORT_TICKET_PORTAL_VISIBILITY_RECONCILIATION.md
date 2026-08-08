# Support ticket portal visibility reconciliation

## Incident and policy

PR #69 moved customer support pages to the local ticket store. It projected raw
ticket descriptions and every comment where the legacy default
`is_internal = false` held. The old CRM offered customers no ticket timeline,
so that value was storage default—not publication evidence. PR #2108 made new
admin comments internal by default, but did not reconcile existing comments or
protect descriptions.

Selfcare is now the ticket authority. Existing narrative is internal. After
migration 503, customer-authored Selfcare descriptions and replies are public;
staff/system narrative remains internal unless explicitly published.

## Pre-deployment preview

Run read-only against the target database and retain the aggregate output:

```sql
SELECT
  count(*) FILTER (WHERE COALESCE(description, '') <> '') AS descriptions_to_hide,
  (SELECT count(*) FROM support_ticket_comments WHERE is_internal = false)
    AS comments_to_hide
FROM support_tickets;
```

Take the normal production backup. Do not manually update business rows.
Migration 503 owns the deterministic reconciliation.

## Verification

After deployment, both drift counts must be zero:

```sql
SELECT count(*)
FROM support_tickets
WHERE description_is_internal = false
  AND created_at < :deployment_started_at;

SELECT count(*)
FROM support_ticket_comments
WHERE is_internal = false
  AND created_at < :deployment_started_at;
```

Bind `deployment_started_at` to the recorded start of the migration run. Also
verify with a customer test account that a
legacy ticket retains its number/status/dates but exposes neither description,
attachments, nor comments. Create a new customer ticket and reply, then verify
both appear. Add an internal note and verify it does not appear; explicitly
publish one staff reply and one reviewed description and verify they do.

## Rollback

Do not restore legacy `false` values: they were never publication decisions.
Application rollback may leave the additive column in place, but the previous
portal code is not privacy-safe and must not be restored as a customer-facing
release. If the new projection fails, disable the customer ticket-detail route
until a corrected image is available.
