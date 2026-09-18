# FUP contention isolation

The FUP owner still commits or rolls back one complete subscription command.
Only SQLSTATE 55P03, 40P01 and 40001 are retried, once, using the same command
identity after the owner returns transaction-free. Unknown database errors and
broken transaction boundaries propagate. Successfully committed subscriptions
are not included in the deferred subset and are not replayed as a whole batch.

The typed sweep outcome gives the Celery adapter committed totals, retry count
and exact deferred identities. The adapter queues that subset once, after sixty
seconds on billing. That follow-up never chains another retry. Subsequent
scheduled full sweeps remain the safety net for persistent contention, lock-skips
or broker submission failures; these do not remove eligible subscriptions.

Monitor deferred, retried, deferred_retry_queued and retry_enqueue_failed counts.
Persistent deferrals need investigation of blockers, not higher retry budgets.
No throttling, notification, usage authority or subscriber eligibility rule is
changed. Unit tests inject PostgreSQL error codes while exercising actual owner
rollback; migrated PostgreSQL concurrency acceptance remains the CI lane.
