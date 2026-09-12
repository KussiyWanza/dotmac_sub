# September 12 consolidation validation

The consolidation includes every pull request that was open when the batch was
selected except PR 2992. The included PRs are 3102, 3104, 3105, 3106, 3107,
3108, 3109, 3110, 3111, 3112, 3113, 3114, 3115, 3116, and 3117. PR 2992 is
explicitly excluded and none of its commits are part of the consolidation.

## Combined behavior

Field material-request history remains visible through canonical requester
identity, including inactive and profileless technicians. Managers retain both
material and expense destinations. Material status polling rotates fairly across
in-flight requests, and ERP-backed cancellation stays pending until ERP confirms
the outcome; an ERP-issued observation wins a cancellation race.

Expense requests use one canonical UUID across the client reference, local
request, destination verification, ERP claim, receipts, manager decisions, and
polling. Submission and manager decisions stage ordered v3 consequences while
retaining compatibility for already-staged v2 release events. Technician and
manager history, approver, and payment projections remain available.

The batch also includes the first-subscription-invoice VAT correction, reviewed
historical prepaid-draft settlement and opening-boundary consumption, Team Inbox
team-sender selection and the audited legacy completion override, EG8145V5
dual-band WiFi paths, and the rolling 8.45.8 version metadata update.

## Migration chain

The application migrations form one linear sequence after the existing prepaid
chain:

1. `599_prepaid_draft_exception_period_evidence`
2. `600_prepaid_funding_trigger_execution`
3. `601_prepaid_draft_exception_no_invoice_identity`
4. `602_inbox_completion_legacy_override`
5. `603_eg8145v5_wifi`
6. `604_material_cancel_pending`

The unpublished network and material-cancellation migrations were renumbered and
linked after the inbox-override migration already present on `main`; their schema
operations and downgrade contracts are otherwise unchanged. A disposable
database that used one of the superseded unpublished revision identifiers must
be recreated rather than stamped forward.

Before staging, rehearse both a fresh migrated PostgreSQL/PostGIS database and
the deployed predecessor through the combined head. Use an explicit disposable
`TEST_DATABASE_URL` and treat SQLite unit results only as non-authoritative fast
feedback.

Release through the protected feature-branch-to-`main` path, validate the exact
`origin/main` commit, build one immutable candidate, complete staging acceptance,
and authorize that same digest for production. Michael must name any staging or
production host before deployment. This document records required gates, not
evidence that they have passed.
