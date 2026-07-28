# Service-team Party cutover

Status: implementation ready; production execution and migration rehearsal
require separate authorization.

Owner: `operations.service_team_party_cutover`

## Purpose

Migration `426_service_team_lifecycle` requires every service-team manager and
active member to resolve to an active Person Party with an active SystemUser
principal. The earlier CRM import copied team rows and CRM Person UUIDs but did
not copy CRM membership rows or create Party identities. A production database
can therefore be healthy at revision 424 while being unable to cross migration
426.

This runbook closes that prerequisite before Alembic runs. It does not seed
sample data. It adopts reviewed production identity and membership facts.

CRM Person UUIDs are preserved as the new Person Party UUIDs. The CRM UUID is
also stored as a non-authoritative `dotmac_crm/person` external reference.
Preserving the identifier lets migration 426 validate existing manager
references without an inferred rewrite. `party.registry` remains authoritative
for the resulting Party and SystemUser binding.

## Safety contract

- The audit and planner are read-only.
- The planner verifies the CRM team copy against current Sub team state and
  snapshots every CRM membership.
- Every referenced CRM Person requires an explicit private decision:
  `bind` to one reviewed SystemUser, or `identity_only` for an inactive
  historical member with no Sub principal.
- Every active member and every manager requires an active reviewed SystemUser.
- The private plan is SHA-256 bound to the decision file and source snapshot.
- Execution requires a separate approval bound to the exact plan and decision
  files. Approval expires within 24 hours and caps identity/membership counts.
- One serializable owner transaction writes the predetermined Parties,
  external references, principal bindings, memberships, audit receipt, and
  event. Any conflict or remaining migration blocker rolls the whole command
  back.
- Replay verifies the receipt and exact applied rows. It never repoints an
  identity, changes a credential or RBAC grant, changes a team or manager, or
  activates/deactivates an account.
- Operator output and durable receipt evidence contain hashes and aggregate
  counts only. Private artifacts contain internal identity data and must never
  enter Git, logs, prompts, reports, or durable knowledge.

## Inputs

Keep the CRM and Sub database URLs in their approved secret locations. Export
them only into the operator process as `CRM_DATABASE_URL` and
`SUB_DATABASE_URL`; do not place values in a tracked or synchronized file.

Create a mode-0600 decision CSV outside the repository:

```text
crm_person_id,decision,system_user_id,decision_id,reason
```

- `decision=bind`: `system_user_id` is required.
- `decision=identity_only`: `system_user_id` is blank and is allowed only for
  an inactive historical member.
- `decision_id` is a fresh UUID for the human decision.
- `reason` records why this CRM Person and SystemUser are the same person. It is
  hashed before entering the plan receipt.

Email matching output from `build_crm_staff_map.py` is candidate evidence only.
It cannot replace the reviewed decision CSV.

## Read-only audit

Run from the candidate image or checked-out candidate source:

```bash
python -m scripts.migration.audit_service_team_party_cutover
```

`--check` exits 2 when blockers remain. `scripts/deploy.sh` runs this check
before Alembic, so an unprepared database fails before migrations begin.

The summary reports team, manager, membership, malformed-setting,
setting-to-native conflict, and blocker counts only. Preserve the output as
operator evidence.

## Build the private plan

```bash
python -m scripts.migration.plan_service_team_party_cutover \
  --decisions /approved/local/path/service-team-decisions.csv \
  --out /approved/local/path/service-team-cutover-plan.json
```

The output path must not already exist. The planner creates it mode 0600. Review
the aggregate counts, source snapshot digest, and plan digest. Independently
verify that the decision count and membership count match the reviewed source
census.

## Approval

Create a separate mode-0600 JSON file outside the repository:

```json
{
  "schema_version": 1,
  "plan_digest": "<planner output>",
  "plan_file_sha256": "<sha256 of the exact plan file>",
  "decision_file_sha256": "<sha256 of the exact decision CSV>",
  "approved_by": "<reviewer identity>",
  "approved_at": "2026-01-01T10:00:00+00:00",
  "expires_at": "2026-01-01T18:00:00+00:00",
  "reason": "<reviewed production cutover reason>",
  "maximum_identities": 200,
  "maximum_memberships": 1000
}
```

The reviewer must be distinct from the automatic matching process and must
approve the actual hashes and count limits. The approval window cannot exceed
24 hours.

## Apply

Execution is a separate, explicitly authorized operation:

```bash
python -m scripts.migration.execute_service_team_party_cutover \
  --plan /approved/local/path/service-team-cutover-plan.json \
  --approval /approved/local/path/service-team-cutover-approval.json \
  --actor service:<operator-identity> \
  --execute
```

Expected output is `status=applied` with counts. An exact retry returns
`status=replayed` and zero new rows. A refusal is not permission to edit the
database manually; refresh the read-only census and prepare a new reviewed
plan.

After apply, rerun:

```bash
python -m scripts.migration.audit_service_team_party_cutover --check
```

The gate must report `ready=true` and `blocker_count=0`.

## Rehearsal and release gate

Before production authorization:

1. restore the pre-cutover production backup into staging;
2. verify its Alembic revision and candidate image;
3. run the read-only audit and compare counts with production evidence;
4. apply the exact reviewed procedure to the restored data;
5. run `alembic upgrade heads`;
6. verify schema contracts, service-team projections, authentication, audit,
   outbox state, application health, and one image/revision across containers;
7. preserve aggregate results and the approved operator record; and
8. authorize production separately by explicitly naming the production host.

Do not stamp past migration 426, run manual SQL, deploy current code against a
424 schema, or infer that an empty Sub membership table means CRM has no
memberships.

## Rollback

Before Alembic, any failed adoption rolls back atomically. After a successful
adoption but before migration 426, do not manually delete identity rows; use
the reviewed pre-cutover backup if the release is abandoned.

Migration 426 is an irreversible authority cutover. Its rollback is restore
from the verified pre-cutover backup and restore the previous image pin. The
deploy script's image rollback does not reverse migrations.
