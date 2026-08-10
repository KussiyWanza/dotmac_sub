# Database transaction pressure

## Signal

`database_transaction_span_seconds` measures every root SQLAlchemy transaction
from its first statement until completion. `database_transaction_spans_slow_total`
increments at 30 seconds, matching the structured
`database_transaction_span_slow` log event. Metrics have no customer, request,
or SQL labels; use the log `request_id` for correlation without creating
high-cardinality metric series.

## Triage

1. Confirm public health, worker count, connection utilisation, lock waits, and
   idle-in-transaction age. Do not assume host or pool exhaustion.
2. Correlate `database_transaction_span_slow` events with HTTP request logs by
   `request_id`; group by route and inspect the longest repeated cohort.
3. Capture `EXPLAIN (ANALYZE, BUFFERS)` for the responsible read on staging or a
   safe replica. Never run an unreviewed expensive plan on production.
4. Reduce work inside the transaction: use grouped reads, bounded pagination,
   lazy panels, or scheduled snapshots. External probes must stay outside it.
5. Verify the focused query-budget test and both alert series before promotion.

Restarting workers, raising Nginx timeouts, or enlarging the pool may hide the
symptom while preserving the transaction and worker contention. Use those only
for a separately evidenced infrastructure failure.

## Alert installation

Load `deploy/observability/database_transactions.rules.yml` through the same
Prometheus/vmalert rule loader used for the other files in
`deploy/observability`. Validate with `promtool check rules` (or the equivalent
vmalert rule check) before reloading the evaluator.
