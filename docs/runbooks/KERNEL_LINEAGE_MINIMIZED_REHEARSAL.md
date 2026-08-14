# Kernel lineage minimized rehearsal

Owner: platform operations  
Gate: ADR-0017 kernel-lineage adoption  
Source: an explicitly named Sub deployment  
Target: an explicitly named isolated PostgreSQL test host

## Purpose

Exercise the installed kernel migration lineage against Sub's deployed schema
and production data **shape** without making a copy of customer data.

The production-side exporter is read-only and emits only:

- the Sub Alembic revision identifier;
- SHA-256 fingerprints of columns, constraints, indexes and RLS flags for the
  six lineage-sensitive tables;
- table row counts; and
- counts grouped into closed structural cohorts for roles, credentials, audit
  actors and Sub business capacities.

It emits no row UUID, name, email, phone number, address, username, password
hash, audit payload, metadata value or timestamp. The bundle schema rejects
unknown fields, so arbitrary row data cannot be added silently. The scratch
rehearsal generates new synthetic rows from each observed cohort and compares
complete-row digests before and after the kernel attempt.

This replaces the proposed full `pg_dump`/restore. A full customer-data copy is
not an accepted input to this runbook.

## Preconditions

1. Michael names both the source and scratch hosts for the execution. Do not
   infer either host from shell aliases, deploy configuration or prior runs.
2. The source runs the same Sub Alembic revision as the checked-out rehearsal
   branch. A mismatch is a refusal, not permission to stamp or upgrade either
   environment.
3. The operator-tenant transaction-scope predecessor is already deployed. The
   rehearsal verifies that prerequisite; it does not deploy it.
4. The target database is disposable and its name contains `test`, `pytest`,
   `ci`, `e2e` or `migration`.

## 1. Export on the named source

Choose a path outside the repository and synchronized directories. The command
creates the file with mode `0600` and refuses to overwrite an existing file.

```bash
LINEAGE_EVIDENCE_PATH=/tmp/sub-kernel-lineage-evidence.json
poetry run python -m scripts.migration.kernel_lineage_rehearsal_evidence \
  --output "$LINEAGE_EVIDENCE_PATH"
```

The script begins a `REPEATABLE READ, READ ONLY` transaction and rolls it back
after collection. Record the reported SHA-256 digest; do not print the bundle
through an unbounded CI log.

## 2. Transfer only the minimized bundle

Copy that one JSON file to the explicitly named isolated target over the
approved host-to-host mechanism. Do not transfer a database dump, volume,
WAL, application `.env`, OpenBao material or PostgreSQL credential file.

On the target, verify owner-only permissions and the digest reported at export.
Keep the artifact outside the checkout.

## 3. Run the one rehearsal lane

Set `TEST_DATABASE_URL` to an explicitly disposable PostgreSQL database and
`KERNEL_LINEAGE_EVIDENCE_PATH` to the transferred bundle, then run:

```bash
poetry run pytest \
  tests/integration/test_kernel_lineage_rehearsal.py::test_the_kernel_lineage_fails_exactly_where_expected \
  -v -o 'addopts='
```

The test:

1. builds Sub's schema with the real Alembic chain;
2. refuses if its revision or any lineage-table catalog fingerprint differs
   from the source bundle;
3. generates one synthetic canary for every production structural cohort;
4. fingerprints complete synthetic rows in `roles`, `user_credentials`,
   `audit_events`, `parties` and `party_roles`;
5. runs the installed kernel lineage with its independent version table;
6. asserts the exact expected first-failure revision; and
7. proves the failed attempt neither changed nor hid any canary.

The source row counts are evidence only. The scratch database never creates
hundreds of thousands of audit rows or copies the 2,000+ business-capacity
rows; cohort representatives exercise their distinct shapes instead.

## 4. Interpret the result

- A source/target contract mismatch means the rehearsal branch does not
  represent the deployed schema. Stop and reconcile the version or schema
  drift.
- A newly earlier kernel failure is a regression.
- A later failure is progress that requires a reviewed disposition and a
  deliberate forward move of `EXPECTED_FIRST_FAILURE`.
- A missing or changed canary is a blocking preservation failure even when the
  Alembic revision itself reports success.
- Empty reads after FORCE RLS are a blocking session-scope failure, not a clean
  migration.

This evidence proves the bounded catalog and cohort contract. It does not prove
behavior outside the declared cohorts, authorize the revision-0001 ratchet,
move audit or Party authority, or approve a production migration.

## 5. Cleanup

Delete the disposable database and remove the evidence file from both hosts
after the result is attached to the review. The file contains no direct PII,
but its production counts and schema identity are still operational evidence
and should not accumulate on general-purpose hosts.
