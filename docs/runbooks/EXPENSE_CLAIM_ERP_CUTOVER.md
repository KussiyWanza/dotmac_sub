# Expense claim ERP cutover

## Authority and release point

- Sub owns the technician's expense request and the manager's approval.
- ERP owns the resulting accounting claim and payment record.
- Submission records the request only. Manager approval is the single release
  point that creates an ERP delivery intent.
- After cutover, approval and its outbox row commit atomically. ERP delivery and
  status reconciliation remain asynchronous and idempotent.

## Deployment prerequisites

1. Apply the existing `field_erp_sync_events` and `sync_flow_ownership` migration
   chain through repository head.
2. Enable the typed ERP outbox-delivery and expense-status capabilities.
3. Verify the ERP identity can create and read expense claims, but has no broader
   human or finance-administration permissions.
4. Confirm the ERP accepts `source_claim_id` and the stable
   `exp-{request_id}-submit-v1` idempotency key.
5. Confirm the previous expense sender is disabled before changing ownership.

## Controlled activation

1. Record and retain the pre-cutover legacy owner for
   `sync_flow_ownership.expense_claim` while deploying and validating the
   application change.
2. Verify a submitted expense creates no outbox row.
3. Assign `expense_claim` ownership to `sub` through the reviewed production
   configuration procedure.
4. Approve one newly created, non-test canary expense and verify exactly one
   pending outbox row is created with the approval.
5. Verify the same row reaches `accepted` and the ERP claim reference is projected
   back to Sub.
6. Verify the field app reports the real ERP delivery state instead of the generic
   “Expense updated” message.

## No historical backfill

Do not enqueue or replay previously approved expenses during this cutover. The
existing records are test expenses and are intentionally excluded. This branch
contains no migration, startup hook, scheduled scan, or repair command that
backfills them. Any future historical repair requires a separate reviewed scope.

## Rollback

Restore the recorded pre-cutover legacy ownership of `expense_claim` to stop new
Sub deliveries, then disable the expense delivery/status capabilities. Do not
delete outbox evidence.
Investigate any pending event by its event ID before deciding whether to resume
delivery. Production configuration and ownership changes are operational work and
are not performed by this branch.
