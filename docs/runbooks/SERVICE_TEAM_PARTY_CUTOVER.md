# Service-team source retirement before migration 426

Status: implementation ready; production execution requires Michael to name
the target host and authorize the exact operation.

Owner: `operations.service_team_source_retirement`

## Purpose

Migration 426 remains immutable. Production may contain CRM-era scalar manager
identifiers or workflow membership settings that migration 426 would try to
interpret as reviewed Party identity. Those values are not authoritative and
must not force a CRM membership import or a staff-identity review.

The replacement gate is deliberately narrow:

1. preserve and verify the support-ticket team pointer;
2. preserve and verify the project team pointer;
3. preserve and verify the dispatch-rule team pointer;
4. preserve and verify the Inbox email-route team pointer;
5. preserve and verify the set-valued Inbox conversation-team pointer; and
6. retire the two workflow settings plus the scalar manager pointer.

The owner removes only compatibility membership rows that migration 426 would
reject: unresolved, inactive or ambiguous identity, missing active principal,
or duplicate Party targets. When a native Party-backed membership and a
compatibility SystemUser row resolve to the same team/Party, the native row is
retained. Several compatibility principals resolving to one Party are all
removed rather than inventing a winner. The owner never reads CRM, creates a
Party, matches staff by email, creates or imports membership, changes
credentials, or grants RBAC.

## Read-only gate

Run from the candidate image or checkout:

```bash
python -m scripts.migration.retire_legacy_service_team_sources --check
```

The command reports aggregate counts only. Readiness requires:

- exactly five declared pointer contracts;
- zero dangling pointer rows;
- zero duplicate case-insensitive native team names;
- zero active legacy workflow sources;
- zero scalar manager pointers; and
- zero membership rows that would block migration 426.

`scripts/deploy.sh` runs this read-only check before Alembic. It does not make
the retirement mutation automatically.

## Reviewed retirement

After reviewing the read-only counts and the current backup evidence, an
authorized operator may run:

```bash
python -m scripts.migration.retire_legacy_service_team_sources \
  --execute \
  --actor service:<operator-identity> \
  --reason "<reviewed release reason>"
```

This enters the registered owner command once, locks native teams,
memberships, and the two settings, rechecks all five pointers, retires the
sources, clears `manager_person_id`, removes only unresolvable compatibility
membership rows, inactive or ambiguous identity blockers, missing-principal
rows, or conflicting duplicate targets, and stages aggregate audit/event
evidence atomically. Exact replay makes no changes.

Duplicate resolution details worth reviewing in the evidence counters:

- When a native Party row and a compatibility row resolve to the same
  (team, Party), the compatibility row is removed. If the native row was
  inactive while the removed compatibility row was the person's only active
  membership, the native row is reactivated so the person's effective
  membership survives (`reactivated_native_membership_count`).
- When several compatibility rows resolve to one Party with no native row,
  none wins and all are removed.
- Entries in the retired `support_service_team_members` setting are counted
  (`abandoned_legacy_member_entry_count`) but never imported.

The deploy gate is self-retiring: once the composable schema from migration
440 exists, `--check` reports ready and skips the audit, so later ordinary
identity drift can never block a deploy behind this one-time tool.

Rerun `--check`, then rehearse `alembic upgrade heads` against a restored
pre-cutover backup. Do not stamp past migration 426, edit migration 426, infer
GeoArea from a region label, or create CRM membership/Party identity to make the
gate pass.

## Composable forward migration

Migration 440 is the expand/backfill phase. It registers capability vocabulary,
adds capability, responsibility, topology, typed scope, external-reference,
and routing-policy tables, and copies existing scalar state into shadow
bindings: legacy `team_type` becomes capabilities (legacy `operations` teams
also receive `outage_response`), legacy member `role` becomes responsibilities
for active members, a still-set `manager_person_id` is preserved as a
membership row (never reactivating a deactivated one) with the
`accountable_manager` responsibility, and `workforce_*` pairs become external
references with a casefolded provider. It also seeds three `network.outage`
continuity routes replicating the retired oldest-active-team-of-type
selection, each only when a matching team exists and no policy for the route
exists yet. It creates no new team, no new person identity, and no access
grant, and it never overrides an operator-configured route.

After cutover, verify shadow agreement with the read-only drift gate:

```bash
python -m scripts.migration.inspect_service_team_shadow_drift
```

It prints the five drift counters and exits nonzero while any legacy pointer
lacks its composed equivalent.

Legacy scalar columns remain nullable shadow inputs. They may be removed only
after the five-field composition drift query is zero for a reviewed complete
cohort, all region labels have been bound to reviewed `GeoArea` records where
appropriate, all consumers use composition, and rollback requirements expire.

## Rollback

Before migration 426, a failed retirement command rolls back atomically. After
successful source retirement, restore the reviewed pre-cutover backup if the
release is abandoned; do not recreate settings or manager pointers manually.

Migration 426 remains an irreversible authority cutover. Migration 440 is
forward-fix only because reconstructing scalar authority would violate the
target contract.
