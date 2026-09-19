# Prepaid renewal review-item savepoint recovery

The prepaid draft reconciliation owner remains the sole review-item writer.
Funding-consequence commands invoke it as a flush-only participant. New review
rows use the authorized owner savepoint when a command is active; standalone
callers retain their existing nested transaction and remain responsible for the
root transaction. The insert is still arbitrated by the invoice/no-invoice
identity constraints. Replays retain one review item and one alert fingerprint.

An ORM flush can mark a savepoint inactive before raising IntegrityError. The
owner executor now recognizes only that internal subtransaction rollback and
explicitly closes its own failed savepoint. Direct participant root or nested
commit/rollback remains prohibited. No transaction ownership or billing
validation rule is relaxed, and no balance, funding baseline or production
record is repaired by deployment.

Regression coverage: tests/test_owner_commands.py (real constraint failure and
illegal completion), tests/test_prepaid_draft_reconciliation_exception_writer.py
(owner entry and duplicate insert). These are fast unit-lane checks; migrated
PostgreSQL acceptance remains the repository integration CI lane. After approved
deployment, review the previously failed funding-event handlers and replay only
unresolved work using the existing idempotent event recovery process.
