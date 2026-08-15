# CRM Network Map migration

Status: operator-gated source-evidence migration

Owner boundaries:

- `network.crm_map_source` validates and normalizes the restored CRM archive.
- `network.fiber_source_staging` owns immutable imported source evidence.
- `network.crm_network_map_point_migration` owns authoritative CRM point-batch
  selection, deterministic point-asset reconciliation, and CRM proposal/apply
  guards for FDH cabinets, fibre access points, and splice closures.
- `network.fiber_identity_decisions` and `network.fiber_connectivity_decisions`
  own reviewed proposals; they never infer identity or topology from proximity.
- `network.fiber_asset_changes` remains the canonical passive-plant writer.
- Selfcare network inventory remains the OLT identity owner. CRM OLT rows are
  comparison evidence only and are never inserted by this importer.

The importer never restores the CRM dump over a Selfcare database, never uses
direct SQL to repair Selfcare records, and never writes canonical map assets.
Deployment of this code does not start a snapshot, restore, staging run,
proposal generation, review, dry-run apply, or canonical apply.

The permanent production map is `GET /admin/network/map`, and it reads
canonical Selfcare models only. `GET /admin/network/map-v2` remains a temporary
preview/workbench. `GET /admin/gis` remains the generic GIS module. Staged CRM
observations must never be rendered directly as production map data.

## Immutable source

The historical 2026-08-12 rehearsal archive was:

```text
/var/lib/dotmac-migration/incoming/network-map/20260812T153420Z/
  crm-network-map-20260812T153420Z.dump
```

Its SHA-256 is:

```text
2d549f2f996bae8a31ac735cf265da294e23c5c4c80cd544abfff934da14b9ac
```

This archive is stale historical evidence and must not be selected for a future
production migration. A production migration requires a newly authorized,
checksum-bound snapshot taken only after this code has been merged and deployed.
The snapshot operation must produce a restricted receipt containing the actual
UTC dump capture time. Pass that receipt value to every dry-run and staging
invocation with `--snapshot-captured-at`; restore time and staging execution time
are not snapshot provenance. Batches produced with this capture-time contract
record importer version `stage_crm_network_map:v2`; historical `v1` batches stay
immutable evidence.
Keep every archive directory and contained file immutable. Perform restore and
inspection work from a separately permissioned working copy. A generated
report belongs under the working/report directory, never beside or inside the
immutable source.

## Restore rehearsal

Restore only into a disposable PostgreSQL/PostGIS 16 database whose name
contains `test` or `restore`. Do not publish its port and do not connect the
application to it as `DATABASE_URL`.

The selective dump does not contain six enum definitions used by its selected
tables. Before `pg_restore`, create these types in the disposable database from
the checked CRM schema:

- `fibersegmenttype`: `feeder`, `distribution`, `drop`
- `fibercabletype`: `single_mode`, `multi_mode`, `armored`, `aerial`,
  `underground`, `direct_buried`
- `fiberstrandstatus`: `available`, `in_use`, `reserved`, `damaged`, `retired`
- `fiberendpointtype`: `olt_port`, `splitter_port`, `fdh`, `ont`,
  `splice_closure`, `other`
- `odnendpointtype`: `fdh`, `splitter`, `splitter_port`, `pon_port`, `olt_port`,
  `ont`, `terminal`, `splice_closure`, `other`
- `oltporttype`: `pon`, `uplink`, `ethernet`, `mgmt`

Creating prerequisites in the disposable restore database is schema setup, not
a repair of CRM or Selfcare data. Restore with `--exit-on-error --no-owner
--no-privileges`. A restore failure stops the migration.

The 2026-08-13 rehearsal restored all 16 selected tables successfully after
supplying these enum prerequisites. All stored geometries were valid SRID 4326.

## Source contract and known gates

The restored archive contains:

| Source table | Total | Active/mappable | Import behavior |
| --- | ---: | ---: | --- |
| `fdh_cabinets` | 245 | 244 | Staged point evidence; one active row has no coordinates and is a hard blocker |
| `fiber_access_points` | 501 | 501 | Staged point evidence |
| `fiber_splice_closures` | 1,321 | 1,313 | Eight inactive rows are reported and excluded |
| `fiber_segments` | 2,890 | 2,890 | Staged LineString evidence; endpoints are not inferred |
| `service_buildings` | 2,742 | 2,742 | Staged identity evidence; canonical creation is not enabled |
| `olt_devices` | 39 | 39 | Comparison-only; never inserted by this importer |

The dependency tables in this archive are empty. That means the archive does
not establish splitter, strand, splice, termination, PON, or OLT child
topology. Route geometry is evidence, not connectivity.

Production already contains older CRM staging evidence created on 2026-08-06.
The new importer preserves the same source-system and profile identities so the
latest archive produces lineage and changed/new classifications instead of a
parallel source.

## Dry-run

Provide the isolated restore URL only through
`CRM_NETWORK_MAP_SOURCE_DATABASE_URL`; never put it in command arguments,
reports, shell history, Git, or logs.

```bash
python scripts/network/stage_crm_network_map.py \
  --archive /var/lib/dotmac-migration/work/network-map/20260812T153420Z/crm-network-map-20260812T153420Z.dump \
  --snapshot-captured-at 2026-08-12T15:34:20Z \
  --batch-size 50 \
  --report-path /var/lib/dotmac-migration/work/network-map/20260812T153420Z/reports/dry-run.json
```

Dry-run performs no writes. It must report:

- the exact archive hash;
- source and active counts;
- deterministic KML and manifest hashes;
- `new`, `unchanged`, `exact_external`, `candidate`, `ambiguous`, and `blocked`
  counts;
- every hard blocker;
- zero canonical asset writes; and
- the unsupported OLT comparison count.

Any `blocked` or `ambiguous` result stops before the first staging write. Do
not remove a source row or manufacture coordinates to make the report green.
Resolve the source fact through its owner and take a new checksum-bound dump.

## Bounded staging evidence

Only a clean dry-run may be replayed with `--stage`. The operator supplies the
same full archive digest explicitly:

```bash
python scripts/network/stage_crm_network_map.py \
  --archive /var/lib/dotmac-migration/work/network-map/20260812T153420Z/crm-network-map-20260812T153420Z.dump \
  --snapshot-captured-at 2026-08-12T15:34:20Z \
  --batch-size 50 \
  --stage \
  --confirm-archive-sha256 2d549f2f996bae8a31ac735cf265da294e23c5c4c80cd544abfff934da14b9ac \
  --actor "approved operator identity" \
  --report-path /var/lib/dotmac-migration/work/network-map/20260812T153420Z/reports/stage.json
```

The command classifies the complete source before writing, then preserves that
classification in transactions of no more than 100 features. Each transaction
creates immutable staging evidence only. Exact replay returns the existing
batch. A failing transaction rolls back and the command stops at the last
successful receipt.

## CRM point-asset reconciliation

This phase covers only:

- FDH cabinets;
- fibre access points; and
- fibre splice closures.

It explicitly excludes OLT creation, fibre segments, service buildings, support
structures, splitters, trays, strands, termination points, route topology, and
customer data. Selfcare network inventory remains authoritative for OLTs.

After an authorized fresh snapshot, isolated restore, and bounded staging run,
select the authoritative staged CRM point cohort with:

```bash
python scripts/network/crm_network_map_point_migration.py report \
  --expected-archive-sha256 <fresh-archive-sha256>
```

Authoritative selection requires matching source system, supported asset type,
completed staged status, archive SHA-256, snapshot timestamp, importer version,
source count, restored count, staged count, full manifest hash, and
`source_restore_staged_counts_match`. Older cohorts remain immutable evidence
but are treated as superseded and cannot generate or execute new proposals.
Authority is resolved across every eligible cohort before the operator-supplied
archive hash is checked. Supplying a superseded archive's own hash cannot make
that archive authoritative again.

Stable CRM source identities use:

```text
crm_network_map:{entity_type}:{crm_primary_key}
```

Names and coordinate proximity may create review candidates, but they never
establish identity automatically. Deterministic reconciliation checks, in
order: existing durable source link, exact unique code/reference match,
supported identifier match, human-review candidate, create-new eligibility,
then conflict/invalid/superseded refusal. Every staged feature receives one of
`already_linked`, `exact_match`, `candidate_match`, `create_new`, `unchanged`,
`conflict`, `invalid`, or `superseded_source` with a durable reason code.

Preview proposal generation without writes:

```bash
python scripts/network/crm_network_map_point_migration.py preview-proposals \
  --expected-archive-sha256 <fresh-archive-sha256> \
  --actor "approved proposer identity" \
  --reason "CRM Network Map point-asset migration phase 1"
```

Persist proposal batches only after the preview is reviewed:

```bash
python scripts/network/crm_network_map_point_migration.py propose-batch \
  --expected-archive-sha256 <fresh-archive-sha256> \
  --actor "approved proposer identity" \
  --reason "CRM Network Map point-asset migration phase 1"
```

Independent review remains the existing identity batch attestation workflow.
Dry-run apply is a separate read-only operation:

```bash
python scripts/network/crm_network_map_point_migration.py dry-run-apply \
  --batch-id <identity-proposal-batch-id> \
  --expected-archive-sha256 <fresh-archive-sha256>
```

Approved apply is a separate bounded command requiring both the exact archive
hash and exact proposal manifest hash:

```bash
python scripts/network/crm_network_map_point_migration.py apply-approved \
  --batch-id <identity-proposal-batch-id> \
  --expected-manifest-sha256 <proposal-manifest-sha256> \
  --expected-archive-sha256 <fresh-archive-sha256> \
  --actor "approved executor identity" \
  --limit 50
```

The `report`, `select`, `preview-proposals`, and `dry-run-apply` adapters run in
a PostgreSQL-enforced repeatable-read, read-only snapshot. `propose-batch` and
`apply-approved` instead open a transaction-free, write-capable owner-command
session; the owning service, not the CLI, completes each business transaction.
Do not route either mutation through the read-only reporting session. None of
these commands runs during application startup or deployment.

Creates emit pending `network.fiber_asset_changes` requests and do not
self-approve canonical asset mutations. Link decisions create only durable CRM
source links to existing canonical assets. Retrying the same proposal/apply
commands is idempotent; stale archive hashes, superseded source batches, changed
staged content, duplicate source identities, and canonical drift fail closed.

## Review and canonical application

Staging is not canonical import. After staging:

1. Review the complete identity and connectivity coverage reports.
2. Resolve candidates and collect field evidence where required.
3. Build proposal manifests with exact staged feature IDs and content hashes.
4. Require an independent reviewer; proposers cannot approve their own work.
5. Execute bounded reviewed proposals. Creates emit pending fibre change
   requests and never self-approve.
6. Reconcile applied requests and verify source links and audits.

Service buildings may only link to an existing canonical building. Fibre
segments require two explicit canonical endpoints; geometry or proximity can
never choose an endpoint. CRM OLT rows require a separate inventory-owned
comparison and resolution path.

## Production gate and recovery

Immediately before the first production staging/canonical batch:

1. Take a fresh Selfcare backup separate from the CRM dump.
2. Verify the backup is readable and record its checksum and image digest.
3. Confirm the exact importer image passed CI and staging acceptance.
4. Confirm permissions separate proposer and reviewer roles.

After each batch, verify counts, map layers, search, fibre geometry, explicit
topology, proposals, approvals, audit evidence, `/health`, workers, and Beat.

On any conflict or failure:

- roll back the current transaction automatically;
- stop at the last successful batch;
- restore the previous image only for an application regression;
- never restore the CRM migration dump over the live database; and
- never use direct SQL to edit or delete imported records.

Previously successful reviewed data remains authoritative evidence and is
corrected only through its owning command/reconciliation workflow.
