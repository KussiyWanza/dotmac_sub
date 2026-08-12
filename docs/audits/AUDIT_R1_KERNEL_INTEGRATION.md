# Audit R1 kernel integration

Status: integration candidate pinned to and rehearsed against the released
kernel a42 artifact; no Sub release, deployment, lineage stamp, or authority
cutover has occurred.

Owner: `observability.audit_log`

## Outcome

Sub migration `524_audit_events_kernel_r1` adds the three missing members of
the accepted audit union:

- `actor_party_id UUID NULL`, indexed and deliberately without a foreign key;
- `details JSONB NULL`, dual-written from legacy `metadata` plus IP/user-agent;
- `created_at timestamptz NULL`, added without a default and assigned
  `DEFAULT now()` only in a second DDL statement.

Historical `created_at` remains NULL. No R1 code copies `occurred_at`, stamps a
migration time, drops a legacy column, changes RLS, or composes the kernel's
Alembic lineage. The Sub owner remains authoritative throughout expansion.

## Coordinated integration, dependency-ordered release

The two repositories are authored together on integration branches so the
contract, product migration, writer changes, and parity evidence can be
reviewed as one programme slice. They still cannot be released atomically:

1. **complete** — publish the reviewed kernel a42 source and verify the
   immutable package;
2. **complete in this branch** — pin that exact released artifact in Sub and
   regenerate the lock hash;
3. **complete** — rehearse Sub's complete migration chain and candidate
   application against the released wheel;
4. merge and deploy only after the required checks are green.

The machine-readable candidate identity is
[`audit-r1-kernel-candidate.json`](audit-r1-kernel-candidate.json). It records a
candidate source commit and local wheel digest, not a package-release claim. It
is frozen historical evidence; its `does_not_prove` list describes that
pre-release artifact rather than the current programme state.

The authoritative release-and-pin evidence is
[`audit-r1-kernel-release.json`](audit-r1-kernel-release.json). Protected
workflow run `31592573094` published and registry-verified a42 from Starter main
`048662dbd944aca95b2e89f133b0c864c3fd5a59`; the annotated tag
`dotmac-kernel-v0.1.0a42` resolves to that commit. Sub now pins a42 in all four
dependency declarations, and `poetry.lock` records the published wheel and
sdist digests without moving any unrelated package.

On 2026-08-12, the released wheel was installed into a disposable Linux
environment on the explicitly named Observe host. Sub's complete Alembic chain
reached `524_audit_events_kernel_r1` on PostgreSQL 16 with PostGIS 3.4. The
rehearsal ran 103 integration tests: all passed against that migrated database. It
returned exit code 0 and removed its disposable database container and network.
This proves released-artifact compatibility; it does not compose kernel
revision 0001, cut audit authority over, merge Sub, or deploy anything.

## Writer and parity contract

`AuditEvents._build_event` is the sole model constructor. Every prior direct
`AuditEvent(...)` caller now reaches `record_audit_event`, `stage_audit_event`,
or the owner itself, and an AST ratchet prevents those bypasses returning.

Every R1 write keeps `metadata` unchanged and constructs `details` as follows:

1. copy legacy `metadata`;
2. merge caller-supplied additive details;
3. make the `ip_address` and `user_agent` columns authoritative over same-named
   JSON keys, omitting each key when its column is NULL.

Run the read-only aggregate report with:

```bash
poetry run python -m scripts.audit_r1_parity
```

The report retrieves counts only. It fails on missing details, metadata/IP/UA
drift, actor types outside the closed four-kind contract, or a non-system actor
without an identifier. `no_r1_rows` is reported separately from `parity`; zero
new rows is not evidence that the writer works.

The report identifies post-expansion writes by non-NULL `created_at`. That is a
reliable statement about rows written through the R1 schema default, not proof
that a malicious raw writer could not explicitly insert NULL. The architecture
ratchet and database access controls own prevention of unsanctioned writers.

## Cutover and rollback boundaries

R1 is expansion only. Reads continue using Sub's legacy columns. A code rollback
is safe while migration 524 remains because older code ignores the new nullable
columns. Downgrading 524 after R1 writes begin destroys the additive evidence
and requires explicit operator approval; normal rollback leaves the columns in
place.

Kernel revision `0001_initial_tenant_schema` remains blocked by the other table
collisions and its atomic tenant function, grants, and FORCE RLS. Audit R1 must
not be described as kernel-lineage adoption.
