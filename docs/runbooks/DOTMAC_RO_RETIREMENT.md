# Runbook: Retiring the legacy `dotmac_ro` reporting role

Decision record: [`docs/adr/0016-retire-dotmac-ro-legacy-reporting-role.md`](../adr/0016-retire-dotmac-ro-legacy-reporting-role.md).

## Scope of this runbook

This runbook covers **Phases 0–3** (Freeze, Inventory, Replace, Cut over),
but ownership within those phases is NOT uniformly repo-actionable —
per-action ownership is annotated in each phase's detail section below.
Notably: Phase 1's live production/DR data capture requires production
database/host access and is infrastructure-executed, not something this
repository can do or verify, and Phase 3's "start monitoring for legacy
connections" is infrastructure-adjacent/owned even though deploying the
replacement code itself is Sub-owned. Only Phase 2 (Replace) and the
deploy-through-normal-release-path portion of Phase 3 are fully
Sub-repo-actionable.

**Phases 4–6 (Disable, Revoke, Remove) are infrastructure-owned production
role-lifecycle actions**, executed under separate explicit authorization for
each window, and are **out of scope for this repository**. They are recorded
here for continuity of the overall plan, not as work this repository performs
or can verify.

> Exception record: infrastructure authority (owner: infra/DB-ops) — pointer
> not yet created; to be filled in when the infra-side freeze record exists.
> This runbook does not itself constitute the exception record. The Phase 4–6
> forward-link (infra tracks those separately) has no location established
> yet.

## The seven phases

| Phase | Work | Hard exit gate |
|---|---|---|
| 0 Freeze | Prohibit new grants, memberships, scripts, views, or jobs using dotmac_ro. Name infrastructure, Sub, security-review, and operator owners (here: Michael, all four capacities). Record the exception and retirement date. | Owners accept the freeze and rollback responsibility. |
| 1 Inventory | Capture role attributes, transitive memberships, ownership, default ACLs, database/schema/table/column/sequence/routine privileges, RLS behavior, active sessions, authentication paths, consumers, automation, production/DR presence, provisioning history. | Every effective privilege and known consumer is classified as required, orphaned, or unknown. Elevated attributes or unknown consumers block changes. |
| 2 Replace | Migrate required consumers to typed, fixed-query operator reports satisfying the approved read-only snapshot property (REPEATABLE READ + READ ONLY, in one transaction) via `read_only_snapshot_session()` (`app/db.py:228`) or `begin_read_only_snapshot(db)` (`app/db.py:214`). Remove obsolete consumers. | Each legitimate use case has an accepted replacement with authorization, redaction, query bounds, timeouts, auditing, and negative tests. |
| 3 Cut over | Deploy replacements through the normal release path. Update runbooks. Compare old/new aggregates where possible. Start reliable monitoring for legacy connections and failed jobs. | No unexplained output differences and no declared consumer requires dotmac_ro. Rollback procedure is rehearsed. |
| 4 Disable | During an explicitly authorized production window, block direct login and every membership/SET ROLE path into dotmac_ro. Preserve object grants temporarily for fast rollback. | No legitimate attempts or failures for at least 30 days and one complete relevant monthly/quarterly reporting cycle. |
| 5 Revoke | In a second authorized window, remove memberships, direct grants, default privileges, and other effective access. Do not use DROP OWNED. | Readback proves zero effective access. Complete another reporting cycle without rollback. |
| 6 Remove | Verify no ownership, dependencies, sessions, automation, DR/bootstrap definitions, or provisioning process can recreate the role. Drop it in a separately authorized window. | Independent readback[^independent-readback] proves the role is absent across the intended fleet and remains absent after reconciliation/recovery checks. |

[^independent-readback]: "Independent readback" is defined ONCE, in ADR-0016's Decision section, Phase 6 bullet — this footnote points there rather than restating it, so the definition cannot drift between the two documents. Short pointer: it means evidence-source independence (a separate query, not a separate reviewer), which is a distinct concept from this runbook's "separately recorded security gate" language.

## Non-negotiable gates

These apply across every phase above and must not be relaxed by convenience
or schedule pressure:

- **NOLOGIN alone is insufficient** if another principal can inherit or
  assume `dotmac_ro`. Disabling login on the role itself does not close a
  path through membership or `SET ROLE`.
- **PostgreSQL normally cannot provide a trustworthy role creation
  timestamp**; provenance may require host audit logs or infrastructure
  history instead.
- **A lack of current sessions does not prove a scheduled monthly or
  quarterly consumer is gone.** Absence of activity at the moment of
  inspection is not absence of a consumer.
- **A failed consumer after disable should be migrated, not automatically
  granted broad access again.** Reverting to broad access defeats the
  purpose of the retirement.
- **Emergency restoration must name the consumer, expire automatically, and
  reopen the retirement finding.** An undocumented, indefinite restoration is
  a silent reversal of Phase 4/5/6, not an exception to it.
- **If direct SQL reporting proves genuinely necessary, pause removal and
  design a separate reporting plane** with individual auditable identities
  and a least-privilege NOLOGIN group. Do not revive `dotmac_ro` as the
  permanent solution.

## Phase detail (0–3; ownership annotated per action, see "Scope of this runbook" above)

### Phase 0 — Freeze

- No new grant, membership, script, view, or scheduled job may reference
  `dotmac_ro`, starting now.
- Owners (all Michael, four capacities): infrastructure/DB-ops (role
  lifecycle, execution, rollback), Sub application (reporting semantics,
  replacements), security review (privilege inventory acceptance, denial
  evidence acceptance), operator acceptance (replacement reports satisfy real
  workflows).
- This repository enforces its half of the freeze with an architecture
  guard (a LITERAL-IDENTIFIER RATCHET, not proof of absence — it catches the
  literal identifier reappearing, not a constructed or obfuscated
  reference): `tests/architecture/test_commercial_module_prerequisites.py`
  fails the build if the identifier `dotmac_ro` appears in a tracked file's
  RAW BYTES — case-insensitively in either of two covered byte-level
  encodings (ASCII/UTF-8 and UTF-16, both byte orders), and
  identifier-boundary-aware in ASCII/UTF-8 ONLY, so a real identifier that
  merely starts with the same characters (`dotmac_router_ssh`,
  `dotmac_roles_r1_...`) does not false-positive there. The UTF-16 patterns
  are deliberately UNBOUNDED — a boundary check cannot know a UTF-16
  payload's byte alignment, so this guard accepts a wider over-report in
  that encoding instead of risking a silent miss — in any git-tracked file
  in this repository that also satisfies the scan's enforceable
  completeness premises, checked against the git INDEX (every tracked
  path's mode checked against an EXACT ALLOWLIST of `100644`/`100755`,
  checked first, before anything is read from disk — a mode outside the
  allowlist refuses, including `120000` symlink or `160000` gitlink/
  submodule and any mode this guard has never named before) as well as
  the WORKING TREE (no symlink or non-regular
  file actually encountered on disk, no missing tracked path) — TWO
  separate sources of truth about the same path, because a tracked
  symlink or gitlink locally replaced by an ordinary regular file on disk
  would otherwise be scanned as normal content: a disk-only check cannot
  see what only the index declares. A violated premise, from either
  source, REFUSES rather than silently skips, except the three files that
  must themselves name it to describe and forbid it (this guard test file,
  ADR-0016, and this runbook — asserted to be EXACTLY those three via a
  literal written independently of the exclusion list); every other test
  file and every other Markdown document is scanned like any other tracked
  file. There is deliberately NO binary-file exemption: matching happens
  directly against raw bytes, not after decoding to text, so it applies
  uniformly to this repository's tracked binary assets (images, fonts) as
  well — a tracked SQLite fixture, compiled catalog, PDF, object file, or
  archived export can all carry the literal bytes `dotmac_ro`, and this
  scan finds them the same way it would in a `.py` file. It also fails if
  `dotmac_ro` is ever added to `COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT`.
- Exit gate: owners accept the freeze and rollback responsibility.

### Phase 1 — Inventory

Capture, for `dotmac_ro`, in production. Data capture against the live
production/DR cluster is **infra-owned** (requires production database/host
access this repository does not have); classifying and analyzing the
captured data against the exit-gate criteria below is **Sub-side**:

- Role attributes (login, superuser, bypassrls, createrole, createdb,
  connection limit). — infra-owned capture.
- Transitive memberships (roles it is a member of, and roles that are
  members of it). — infra-owned capture.
- Object ownership, default ACLs. — infra-owned capture.
- Database/schema/table/column/sequence/routine privileges. — infra-owned
  capture.
- RLS interaction (does it bypass RLS anywhere; does any policy reference
  it). — infra-owned capture.
- Active sessions at time of inventory, and known scheduled consumers
  (jobs, dashboards, ad hoc scripts) even if not currently connected. —
  infra-owned capture.
- Authentication paths (password, peer, cert, any pgBouncer/connection
  pooler mapping). — infra-owned capture.
- Production and DR/standby presence. — infra-owned capture.
- Provisioning history, to the extent host/audit logs allow (PostgreSQL
  itself will not reliably provide role creation time — see the
  non-negotiable gate above). — infra-owned capture.
- Classifying each captured privilege/consumer as `required`, `orphaned`, or
  `unknown` against Sub's actual reporting semantics — Sub-side analysis,
  once infra has supplied the raw capture above.

Exit gate: every effective privilege and every known or suspected consumer is
classified `required`, `orphaned`, or `unknown`. An elevated attribute (e.g.
bypassrls, superuser, createrole) blocks moving to Phase 2 until it is
explained and accepted or removed; a consumer still classified `unknown`
blocks moving to Phase 2 until it is reclassified as `required` or
`orphaned` — `unknown` is not an acceptable resting state for Phase 2 entry.

### Phase 2 — Replace

For each consumer classified `required` in Phase 1:

- Design and implement a typed, fixed-query operator report in Sub,
  satisfying the approved read-only snapshot property (REPEATABLE READ +
  READ ONLY, in one transaction) via either `read_only_snapshot_session()`
  (`app/db.py:228`, for a report that can accept a yielded session) or
  `begin_read_only_snapshot(db)` (`app/db.py:214`, for a report that owns
  its own session, e.g. one needing its own savepoint discipline) — see
  either function's docstring.
- The replacement must have: explicit authorization (who may call it),
  redaction of anything the original broad grant exposed but the specific
  report should not, bounded queries (no unbounded scan standing in for the
  old broad `SELECT`), timeouts, audit logging of access, and negative tests
  proving unauthorized callers are denied.
- Remove consumers classified `orphaned` in Phase 1 instead of migrating
  them.

Exit gate: every `required` use case has an accepted replacement meeting all
of the above, with negative tests. Security review's separately recorded
security gate covers the replacement's authorization and denial evidence;
operator acceptance confirms the replacement report satisfies the real
workflow it replaces.

### Phase 3 — Cut over

- Deploy the Phase 2 replacements through Sub's normal release path — no
  out-of-band deploy for this. — **Sub-owned.**
- Where feasible, compare old (`dotmac_ro`-backed) and new (replacement)
  aggregates for the same period to catch a silent behavioral difference. —
  ownership follows the SAME per-artifact model below, not a blanket
  assignment: for an in-repo Sub artifact, this comparison is Sub-owned
  cutover/drift evidence; for an externally configured artifact (e.g. a
  Grafana dashboard), this comparison IS the cutover/drift evidence the
  per-artifact record assigns to `dotmac_observability`, and a Sub-owned
  comparison must never be substituted for it — doing so would let a
  Sub-side record wrongly close an external artifact's output-difference
  gate that only its actual configuration owner can close. Reading
  production data for either case may itself require **infra** access to
  run against.
- Start reliable monitoring for any remaining connection as `dotmac_ro` and
  for any job that fails because it can no longer reach it (expected, once
  Phase 4 begins — but monitoring should exist before then). —
  **infra-owned/infra-adjacent**: production connection and job monitoring
  is not a Sub-repo release action.

**Every operational runbook or dashboard reference that pointed at a
`dotmac_ro`-backed query is a separate artifact, not one blanket bullet.**
"Operational" does not automatically mean Sub-owned — that inference is
itself a defect this runbook previously carried. The ownership model:

- **Sub owns the operational meaning**: service state, incidents, work
  orders, customer impact, handover, and official timelines.
- **`dotmac_observability` owns production dashboard/Grafana configuration**
  — promotion, rollback, drift evidence, and receipts for anything rendered
  outside this repository.
- **A runbook is owned by whichever repository or control plane actually
  versions it.** A runbook living in `docs/runbooks/` in THIS repository is
  Sub-owned; a runbook that documents an externally-versioned dashboard or
  pipeline is owned wherever that artifact is actually versioned, regardless
  of where its prose happens to be read.

Before Phase 3 cutover proceeds, EACH affected artifact from Phase 1's
consumer inventory needs its own record carrying:

| Field | Meaning |
|---|---|
| Semantic authority | Who owns what the artifact means operationally (Sub, per above) |
| Configuration owner | Who owns the artifact's actual configuration (Sub for in-repo code/templates; `dotmac_observability` for production dashboard/Grafana config; the versioning system of record for anything else) |
| Exact revision | The specific committed/deployed revision being cut over to |
| Cutover action | What changes, concretely, for this artifact |
| Owner-specific rollback | The configuration owner's own rollback procedure for THIS artifact — reverting a Sub release does not, and must never be described as restoring, an externally-owned artifact's prior state |

**If an external dashboard or other externally-versioned artifact has no
established `dotmac_observability` revision and rollback on record, mark
that artifact's row UNRESOLVED and BLOCK its cutover.** Neither this
runbook nor ADR-0016 currently names a `dotmac_observability` revision or
rollback for any specific dashboard — no per-artifact record exists yet
because Phase 1's consumer inventory has not run. Blocking is the correct
outcome for an unresolved artifact; this runbook does not invent a revision
or rollback procedure to make a record look complete.

Exit gate: no unexplained output differences between old and new reports;
no consumer still requires `dotmac_ro`; the rollback procedure for cutover
has been rehearsed **per artifact, by that artifact's own configuration
owner** — a rehearsed Sub release rollback proves nothing about, and must
never be cited as evidence for, an externally-owned artifact's rollback.

## Phases out of repo scope (infrastructure-owned: 4–6)

Phases 4 (Disable), 5 (Revoke), and 6 (Remove) are production role-lifecycle
actions against the live PostgreSQL cluster. They are performed by
infrastructure/DB-ops, each under its own separately named, explicitly
authorized production window, and are not executed, verified, or rolled back
from this repository. See the phase table above for their work and exit
gates; this repository's only relationship to them is the frozen state this
runbook and its architecture guard maintain until they complete.

## Rollback of this runbook

Reverting this document and its companion ADR
(`docs/adr/0016-retire-dotmac-ro-legacy-reporting-role.md`) and the
architecture guard in
`tests/architecture/test_commercial_module_prerequisites.py` rolls back only
this repository's documentation and static guard. It does not — and cannot —
roll back any production action, because no production action against
`dotmac_ro` is taken under this repository's actual scope: Phases 0–3 are
NOT uniformly repo-actionable (see "Scope of this runbook" above — Phase
1's live capture and part of Phase 3's monitoring are infra-owned), but
none of Phases 0–3, in any of their per-action ownership, touches the role
itself in production.
