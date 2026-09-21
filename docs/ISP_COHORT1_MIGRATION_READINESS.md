# Cohort-isp-01 migration readiness runbook

The sequence that would move Governance cohort `cohort-isp-01` — "Foundation
party and customer" — from `asm-dotmac-sub-legacy` to `asm-dotmac-isp`, and the
evidence each step has to produce.

**Nothing in this document is authorised to run.** Every cohort in the accepted
matrix is `blocked`, `dec-isp-002` (production deployment owner) and
`dec-isp-003` (the enforceable legacy Sub transition rule) are open and are
Michael's, and no host, date or window is named here or may be inferred from
here. This is the shape of the work, written down while it is cheap, so that
when it is authorised nobody has to invent it under time pressure.

Companion documents: `docs/adr/0012-isp-cohort-source-readiness.md` (the
decision), `docs/ISP_COHORT1_SOURCE_OWNERSHIP.md` (who writes what today).

## Where the source stands

| | |
|---|---|
| Programme | `pgm-dotmac-isp-replacement` at `d91a87f6823bfd2afa6c2025bdb1af644331fa39` |
| Cohort | `cohort-isp-01`, sequence 1, state **blocked** |
| Source | `asm-dotmac-sub-legacy` — source-authoritative |
| Target | `asm-dotmac-isp` — candidate, independent database, no deployment owner |
| Entity types | 12 |
| Production writers | 28 (8 declared owners, 2 derived projections, 18 parallel) |
| Contract version | 1 |
| Record schema version | 1 |

What Sub can claim today is exactly the four members of
`SourceReadinessClaim`, and no more:

- `inventoried` — every writer and caller of cohort-1 state is classified;
- `export_contract_ready` — the typed read-only snapshot exists and is tested;
- `digest_contract_ready` — the comparison digest is deterministic and tested;
- `writer_surface_ratcheted` — the current writer surface is frozen in both
  directions.

The vocabulary has no member that spells adoption or cutover, on purpose. Read
those four next to the cohort's state, which is **blocked**: both statements
are true at once, and the pairing is the point. `ctl-isp-001` — attributable
human approval of the programme itself — is the one control already `verified`;
every other control below is not.

## The ten steps

The order is Governance's, and every step is gated by the control named beside
it. A step cannot start because the previous one looks finished; it starts when
the control cites immutable evidence.

### 1. The destination pins a registry-verified module set — `ctl-isp-004`

Cohort 1's components are `dotmac-kernel` and `dotmac-ui` (reuse),
`dotmac-party` (release), `dotmac-brand-profiles` (adopt) and
`dotmac-customers` (build). Each must be an immutable published artifact pinned
by digest, not by version and never by tag.

Sub's part: none. Sub does not pin, compose or install any of them. A Sub
change that added one would be composing a Starter module in the source
assembly, which the programme forbids for exactly this cohort.

### 2. The destination composes the lineage disabled — `ctl-isp-004`

The target assembly installs the module lineages without activating any
writer. Composition is not adoption; a composed module with no rows and no
callers has moved nothing.

### 3. The destination applies its own migrations — `ctl-isp-005`

Against the target's own database. It shares no table, session, transaction or
migration with Sub, and the two applications never read each other's rows. The
catalog, RLS and grant proof is the target's evidence to produce.

### 4. The destination imports typed Sub snapshots as observations — `ctl-isp-006`

The first step that touches Sub, and it touches it read-only.

**Backfill ordering.** Referential closure decides it, and it is not the same
as the entity list's alphabetical order. Import in this order:

1. `party`
2. `party_role`, `party_external_reference` — both reference `party` only
3. `organization` — references `party`, and its own `parent_id` self-reference
   means it needs a second pass or deferred constraints
4. `brand_profile` — its `scope_id` points at a reseller or organization
5. `customer_account` — references `party`, `organization`, and later-cohort
   ids (`reseller_id`, `tax_rate_id`, `policy_set_id`, `sales_order_id`) that
   **do not exist in the target yet**
6. `party_relationship`, `party_membership`, `party_contact_point` — reference
   two parties each
7. `organization_membership` — references `organization` and a person id
8. `customer_contact`, `customer_address` — reference `customer_account`

Steps 5 onward carry references the target cannot resolve during cohort 1. They
are imported as **unresolved correlation references**, never as foreign keys
and never dropped. A dropped reference is unrecoverable; an unresolved one is a
later cohort's join.

**Deduplication keys.** The stable opaque identity — `<entity_type>:<uuid>` —
is the only deduplication key. Not the email, not the phone, not the subscriber
number: Sub's own model documents that `subscribers.email` is deliberately
non-unique because customers under one reseller share a contact address, and
deduplicating on it would silently merge distinct accounts. A row already held
under its source identity is replaced or skipped; it is never inserted twice
and never merged with a similar-looking row.

**Checkpoint and retry.** Pagination is keyset — `after_source_id` — so a retry
resumes without re-reading and without skipping. An interrupted drain resumes
from its last checkpoint at the *same* source revision; if the revision has
moved, the drain restarts. Retrying an export is free: it writes nothing and
has nothing to compensate.

**Snapshot consistency.** One drain is one REPEATABLE READ, READ ONLY
transaction, and its `source_revision` carries the Alembic head, the
application version and the PostgreSQL visibility snapshot that fixed the read.
Two pages with different `snapshot_transaction_id` values are two snapshots and
must not be assembled into one import.

**Observations, not decisions.** The destination writes what Sub said, marked
as an observation. It does not set lifecycle state from it. The snapshot has no
disposition field and no target status vocabulary, so there is nothing in it to
mistake for a decision — but the discipline still belongs on the import side.

### 5. The destination's resolver builds local candidate state — `ctl-isp-006`

The target's own resolver derives its state from the observations. Three
classes of source fact must not survive as authority:

- **Declared derived fields.** `CustomerAccountRecord.DERIVED_FIELDS` names
  them: `status`, `is_active`, `lifecycle_override_status`,
  `lifecycle_override_reason`, `lifecycle_override_source`,
  `lifecycle_override_at`,
  `legacy_party_status` and `mrr_total`. Every one is a projection of a
  decision owned elsewhere in Sub. The target recomputes them from its own
  subscriptions and its own policy. `mrr_total` in particular is a money figure
  written by a module Sub's registry does not declare; adopting it would give
  the destination a number with no owner on either side.
- **Opaque blobs.** Every `metadata` column, `access_scope` and
  `legal_address` cross as a key inventory and a digest. They are evidence that
  a convention exists, not data. Reading structure into them would invent a
  contract nobody owns.
- **Unresolved references.** See the ordering note above.

### 6. Shadow comparison runs against source digests — `ctl-isp-007`

Sub publishes a `CohortDigest`; the destination produces its own over the state
its resolver built; `digest.compare` reduces the pair to bounded verdicts.

The comparison needs no Sub database access and no Sub credentials — that is
the point of the digest artifact. It carries identities and hashes and no field
values, so it can be attached to a control record and read by whoever
adjudicates it.

Run it against a digest whose `completeness` is `complete` for every entity
type. A `partial` type yields `source-unknown` or `target-unknown`, which is
the honest verdict and not a finding to work around.

### 7. Mismatches reach zero, or an explicitly adjudicated baseline — `ctl-isp-007`

Six verdicts, and each has one correct response:

| Verdict | What it means | Response |
|---|---|---|
| `missing-from-target` | Sub has a row the target does not | Re-import; if it will not import, that is a defect, not a baseline |
| `unexpected-in-target` | The target has a row Sub does not | Find what created it; a target row with no source is a second writer |
| `divergent` | Same identity, different content | Diff the canonical fields; usually a mapping or a normalisation bug |
| `unsupported-version` | The two digests are not comparable | Re-export at one version; never compare across versions |
| `source-unknown` | Sub's drain was partial | Finish the drain |
| `target-unknown` | The target's drain was partial | Finish the drain |

**Adjudication.** A mismatch may only be baselined by a named human, in a
written record, citing the source identity and the reason. There is deliberately
no "acceptable" or "expected difference" verdict in the vocabulary: an
adjudication is a decision about a specific row, and a comparator that could
express it would let a class of differences be waved through as a category.

Silence is not adjudication. A cohort with unadjudicated mismatches has not met
`ctl-isp-007`, however small the count.

### 8. Rollback and recovery are rehearsed — `ctl-isp-008`

Before any authority moves, prove the way back on a disposable target:

- The rollback point is the **immutable source watermark** — the
  `source_revision` of the last complete pre-switch drain. Record it; it is
  what a recovery replays from.
- Prove the import is idempotent: replay the same snapshot twice and show the
  target's digest is unchanged. An import that is not idempotent cannot be
  safely retried, and every recovery is a retry.
- Prove delta capture: take a second drain at a later revision, replay it over
  the first, and show the target converges without duplicating rows.
- Prove Sub is untouched: Sub's own cohort digest before and after the entire
  rehearsal must be identical. The export path writes nothing, so this should
  be trivially true — rehearse it anyway, because "should be" is the assumption
  a rehearsal exists to test.

### 9. A later authorisation seals the writer switch — `ctl-isp-008`

Not this work, and not this document's to schedule. What the switch needs:

- a named production deployment owner (`dec-isp-002`, open);
- the enforceable legacy Sub transition rule (`dec-isp-003`, open);
- an operator approval attributable to a person, recorded in Governance;
- **traffic-zero evidence** for the cohort's Sub write paths: a measured window
  in which none of the 28 production writers executed. Sub already emits
  structured logs and audit events; the evidence is a query over them naming
  each writer and showing no invocation, not an assertion that the surface is
  quiet;
- a delta capture from the sealed watermark forward, so writes that landed
  between the last drain and the seal are not lost;
- explicit rollback conditions, agreed before the switch rather than
  discovered after it.

### 10. Displaced Sub writers are removed and the ratchet is lowered — `ctl-isp-009`

Only after the switch. The ratchet baselines hold 36 entries; the 28 that can
still write production are what must reach zero — `displaced_writer_paths()`
is that set, and every one of them carries a disposition that removes it
(`RETIRE_AFTER_CUTOVER`, `ROUTE_THROUGH_OWNER_FIRST`, or `UNDECIDED`). A
displaced writer marked to stay would be a contradiction the ratchet could
never resolve, so a test refuses one.

Fourteen of the 28 are `ROUTE_THROUGH_OWNER_FIRST`, and those come **earlier**
than this step: a shadow comparison run against a source with two writers
cannot tell drift from the second writer. They gate `ctl-isp-007`, not
`ctl-isp-009`.

The rule is one direction per change, deliberately: the pull request that
removes a writer lowers its baseline line in the same commit. Removing a writer
without lowering the line fails the guard — a ratchet that silently absorbs
removals can be spent twice, and the next addition would pass a check that
should have caught it.

The eight non-production entries — two disposable-database tools and six
applied migrations — stay. They cannot write production again and there is
nothing to retire; they remain in the baseline so a *new* fixture seeder or a
*new* migration touching cohort tables still fails the guard.

## Evidence required before authority can move

| Control | Owner | Evidence |
|---|---|---|
| `ctl-isp-002` | Michael Ayoade | A named production deployment owner for `asm-dotmac-isp` |
| `ctl-isp-003` | Michael Ayoade | The enforceable legacy Sub transition rule |
| `ctl-isp-004` | Dotmac ISP technical owner | Digest-pinned releases and one composed migration graph |
| `ctl-isp-005` | Dotmac ISP technical owner | Target catalog, RLS, grants and migration rehearsal on the exact pins |
| `ctl-isp-006` | Dotmac Sub technical owner | Every source row dispositioned; idempotent replay proved by digest equality |
| `ctl-isp-007` | Dotmac ISP technical owner | Zero unexplained drift at an immutable source watermark |
| `ctl-isp-008` | Michael Ayoade | Sealed switch: approval, delta capture, traffic-zero evidence, rollback conditions |
| `ctl-isp-009` | Dotmac Sub technical owner | 28 production writers to zero, ratchet lowered writer by writer |

A control is verified only in Governance, citing an immutable
controlled-source reference. Nothing in this repository advances one, and an
agent-authored assertion is not evidence.

## Open source-side questions

Governance answered five decisions on 2026-08-21 (`d91a87f`), and the three
that were carried as `Disposition.UNDECIDED` on specific surfaces are now
closed. What remains for `ctl-isp-006` is **work created by those answers**,
plus the two dispositions that were always going to need one:

- **`customer_location_requests.py` bypasses `customer.accounts`.** Not, as an
  earlier version of this document said, because addresses have no Sub owner —
  they have four declared ones (see the ownership map). The write simply does
  not go through any of them, and it has to before the cohort can be shadowed.
  Extraction into `dotmac-addresses` draws on `customer.accounts`,
  `gis.spatial_sync`, `customer.location_capture` and
  `customer.location_verification`.
- **The account-recovery cascade must be decomposed.** Customers owns recovery
  intent, but one restore currently touches invoices, payments, credentials,
  RADIUS, IP assignments and ONT assignments in the same pass. Only the account
  rows are cohort 1, and the pass has to be separable before they can move.
- **`subscribers.metadata` has seven writers and no declared shape.** Decided:
  it crosses as an opaque key inventory plus a digest until an owner declares
  its keys. That is a standing rule, not a pending question — but the seven
  writers still have to route through `customer.accounts`.
- **`organizations` and `organization_memberships` have no counted writer.**
  Historical B2B account records with no live owner. Do they migrate, stay in
  Sub as history, or retire with evidence? Still open.
- **`subscriber_nin_verifications`.** Decided: a Compliance/Records retention
  disposition rather than a cohort-1 migration one. It stays out of the export
  and is governed by retention policy; the cohort carries only a presence flag
  on the account.
- **Nine other cohort-adjacent tables are deliberately unmapped.** Each is
  recorded in `surfaces.UNMAPPED_ADJACENT_TABLES` with its reason; each still
  needs a disposition before the cohort can claim every source row is
  accounted for.

## Operating the export

Both modes read only, and both refuse a tenant that is not the operator tenant
rather than answering with an empty page.

```bash
# privacy-safe comparison digest — identities and hashes, no field values
python scripts/migration/export_isp_cohort_snapshot.py --digest

# one page of one entity type; refuses a terminal, writes 0600
python scripts/migration/export_isp_cohort_snapshot.py \
    --snapshot --entity-type party --page-size 200 --out /path/to/party-0.json
```

Handle a snapshot file as customer data: it carries names, emails, phones and
addresses. The digest does not, and is the artifact to attach to a control
record or hand to whoever runs the comparison.

## Why there is no online export endpoint

There is deliberately no HTTP export route, and adding one is not pending work
that got dropped.

An authenticated `/api/v1` export endpoint would be a standing egress surface
for the customer identity data of the whole cohort, reachable from the network,
available continuously. Today it would have no consumer at all: `ctl-isp-002`
is open, so no destination is named, no deployment owner exists, and nothing is
positioned to call it. That is the worst possible trade — permanent exposure
against zero current use.

The operator CLI has the properties the online route would have to re-earn:
access is host access rather than a token, output is a 0600 file rather than a
response body, the default mode cannot leak field values, and every invocation
is a deliberate human act on a host somebody already had to reach.

Build the online adapter when, and only when, all of these hold: `ctl-isp-002`
is verified with a named destination and deployment owner; the destination can
authenticate as a specific principal rather than a shared token; the route is
guarded by a dedicated permission that no seeded role carries by default;
egress is restricted to the destination; and each export is audited with its
contract version, checkpoint and requesting principal. Until then the absence
is the correct design.
