# Metrics Scrape Safety

Status: approved Dotmac architecture standard.

The canonical cross-system reference is the Observability section of
`dotmac_sub/docs/SOT_RELATIONSHIP_MAP.md`. This document records the local
enforcement contract for this repository.

## Owner and Boundary

The observability service owns metric publication policy. The `/metrics`
route, Prometheus collectors, and exporter helpers are thin transport adapters.
Business-domain services remain the source of truth for business state.

A scrape is an externally scheduled read. It must stay available when the
database, cache, worker fleet, or a business integration is degraded.

## Required Rules

1. Scrape-time code may read only:
   - process-local counters, gauges, histograms, and pool statistics;
   - bounded cache snapshots produced outside the request;
   - static build or runtime metadata.
2. Scrape-time code must not:
   - open an ORM session or database connection;
   - query business tables or system catalogs;
   - call a business resolver, reconciler, report, ledger, or cohort reader;
   - perform external network discovery;
   - mutate domain state or trigger a side effect.
3. Domain and infrastructure queries run in a scheduled worker or service loop
   with single-flight protection where overlap is possible, a hard total
   deadline, and an explicit reliability contract.
4. Producers publish bounded snapshots with:
   - a registered domain and maximum observation count;
   - a write timestamp;
   - an expiry;
   - finite numeric values and bounded labels;
   - availability and age signals at export.
5. Collectors are fail-soft. Missing, stale, or malformed snapshots suppress
   only the affected metric family and expose an availability or age signal;
   they never make `/metrics` unavailable.
6. Scrape configuration must have a timeout longer than the normal endpoint
   latency, but a client timeout is never treated as cancellation of backend
   work.
7. Every change to a metrics route, custom collector, or scrape helper must pass
   the local architecture test that rejects database and business-service
   access from the scrape path.
8. Moving a scan out of the scrape request does not by itself make the producer
   bounded. Frequent health producers use a fixed query budget and aggregate in
   the database; they do not load a complete historical cohort and issue
   relationship or settlement queries per record.
9. A fast health projection and a detailed forensic audit may expose different
   read models, but they share one domain owner and the same invariant
   definitions. The health projection publishes typed counts. The detailed
   audit returns entity-level evidence on a separate cadence or operator
   request and must not be called merely to obtain a count.

## Migration Rule

When a scrape-time query is found:

1. Name the service that owns the source state.
2. Move the query into a scheduled producer.
3. Publish one bounded latest-state snapshot.
4. Make the collector read only that snapshot.
5. Export snapshot availability and age.
6. Add query-budget tests to the producer and no-database tests to the
   collector.
7. Remove the old scrape-time path before enabling the scraper.
8. Prove aggregate equivalence against the owning detailed resolver for each
   invariant moved into the snapshot.

## Incident Basis

This standard follows the July 2026 Subscriber outage where a one-minute
metrics scrape reconstructed thousands of customer ledgers. Timed-out clients
did not stop synchronous worker threads, overlapping scans exhausted
PostgreSQL and application capacity, and customer requests returned 504.
