# ADR 0016: Retire the legacy `dotmac_ro` reporting role

Status: accepted

Date: 2026-09-13

Decision owner: Michael

Affected systems and domains: Dotmac Sub reporting, PostgreSQL role
governance, infrastructure/DB operations, security review, operator
workflows.

## Context

`dotmac_ro` is a legacy PostgreSQL role observed on the known production
cluster. It carries roughly 291 public-schema `SELECT` grants (per the
current operator record) and has never been captured in this repository.
Whether it also exists elsewhere in the fleet or on DR/standby
infrastructure is UNVERIFIED from here — establishing that is Phase 1
(Inventory) work, not a fact this ADR asserts. Prior to this ADR,
an exhaustive grep across this repo's `.py`, `.md`, `.sql`, `.sh` and `.yml`
files found zero occurrences of the actual role identifier `dotmac_ro`
standing alone. (Two substring collisions already existed and are not
references to this role: `dotmac_router_ssh`, a docker-compose volume/service
name, and `dotmac_roles_r1_...`, a test-generated role prefix — both merely
start with the same characters.) This ADR, its companion runbook
(`docs/runbooks/DOTMAC_RO_RETIREMENT.md`), and the architecture guard test
(`tests/architecture/test_commercial_module_prerequisites.py`) are the first
and only place in this repository that now deliberately names `dotmac_ro`,
for the purpose of describing and permanently forbidding it everywhere else.
It sits entirely outside the closed
application-role contract this repository does own: `MODULE_DATABASE_ROLE_CONTRACT`
in `app/commercial_module_prereqs.py` names exactly `app_admin` (BYPASSRLS),
`app_user` (NOBYPASSRLS), and `platform_api` (NOBYPASSRLS) — this is the
"closed application-role contract" this ADR refers to throughout.
`COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT`, applied by
`scripts/bootstrap_commercial_module_prereqs.py`, is a broader bootstrap-role
contract: it contains `MODULE_DATABASE_ROLE_CONTRACT` in full, plus
`dotmac_app` (the migration/schema-owner role, not an application identity)
and the `PUBLIC_PROBE_ROLE` entry (`dotmac_public_probe`, deliberately
NOLOGIN — a measuring instrument, never an identity anything authenticates
as). Neither `dotmac_app` nor `PUBLIC_PROBE_ROLE` is part of the closed
application-role contract; do not conflate the two contracts.

That closed contract has no seam for an ad hoc reporting identity, and it
should not grow one: `dotmac_ro`'s broad, undifferentiated `SELECT` surface is
a different kind of thing from a named application role with a scoped,
reviewed privilege set. Because the role predates this repository's role
governance and was never brought under it, its actual current privileges,
consumers and provenance in production are not fully known from here — that
inventory work is the substance of Phase 1 below, not something this ADR can
assert.

This ADR is governance and documentation only. It performs no database
migration, creates no role, and authorizes no retirement action by itself. It
records the decision to retire `dotmac_ro` over a bounded set of phases, and
freezes further growth of its footprint starting now.

## Decision

Retire `dotmac_ro` without disrupting legitimate operational reporting.
`app_admin`, `app_user` and `platform_api` remain the closed, UNCHANGED
application-role contract throughout this retirement — nothing in this
decision adds `dotmac_ro`, or any successor reporting identity, to that
contract.

Reporting and audit identities are OUTSIDE the application-role contract by
deliberate design, not oversight: application roles authenticate the running
service; reporting/audit access is a separate concern with its own
authorization, redaction and audit requirements, and conflating the two is
what produced a role with 291 ungoverned grants in the first place.

The retirement proceeds in seven phases, summarized here at decision-record
altitude (full procedure: `docs/runbooks/DOTMAC_RO_RETIREMENT.md`):

0. **Freeze** — prohibit new grants, memberships, scripts, views, or jobs using
   `dotmac_ro`. Exit gate: owners accept the freeze and rollback
   responsibility.
1. **Inventory** — capture every effective privilege, membership, consumer and
   provisioning path. Exit gate: every privilege and consumer is classified
   required, orphaned, or unknown; elevated attributes or unknowns block
   progress.
2. **Replace** — migrate required consumers to typed, fixed-query operator
   reports satisfying the approved read-only snapshot property (REPEATABLE
   READ + READ ONLY, in one transaction), via either `read_only_snapshot_session()`
   (`app/db.py:228`, for a report that can accept a yielded session) or
   `begin_read_only_snapshot(db)` (`app/db.py:214`, for a report that owns
   its own session, e.g. one needing its own savepoint discipline). Exit
   gate: each legitimate use case has an accepted replacement with
   authorization, redaction, query bounds, timeouts, auditing and negative
   tests.
3. **Cut over** — deploy replacements through the normal release path,
   monitor for legacy connections. Exit gate: no unexplained output
   differences; no declared consumer requires `dotmac_ro`; rollback rehearsed.
4. **Disable** — block direct login and membership/`SET ROLE` paths into
   `dotmac_ro`, in an authorized production window. Exit gate: 30 days and one
   full relevant reporting cycle with no legitimate attempts or failures.
5. **Revoke** — remove memberships, direct grants and default privileges (not
   `DROP OWNED`), in a second authorized window. Exit gate: readback proves
   zero effective access, through another reporting cycle.
6. **Remove** — verify nothing can recreate the role, then drop it in a
   separately authorized window. Exit gate: independent readback proves the
   role is absent fleet-wide and stays absent after reconciliation/recovery
   checks. **"Independent readback" is defined once, here, for every
   occurrence in this ADR and its runbook: a separate QUERY against actual
   database state, producing its own evidence record, run instead of or in
   addition to trusting a command's exit code — independence of evidence
   source, not a separate human reviewer.** (Security review's acceptance,
   above, was reworded from "independently accepts" to "separately recorded
   security gate" precisely because THAT independence does not exist in a
   single-operator fleet; this one does, because a second query against the
   database is a real, distinct evidence source regardless of who is
   running it.)

### Ownership boundary

- **Infrastructure/DB-ops** (Michael) owns authentication, role lifecycle,
  memberships, host provisioning, production execution, rollback, and fleet
  reconciliation.
- **Sub** (Michael, application capacity) owns reporting semantics and the
  replacement interfaces satisfying the approved read-only snapshot property
  — REPEATABLE READ + READ ONLY, in one transaction — via either
  `read_only_snapshot_session()` (`app/db.py:228`) or
  `begin_read_only_snapshot(db)` (`app/db.py:214`), the two seams any future
  typed reporting interface builds on.
- **Security review** (Michael, security capacity) performs a separately
  recorded security gate over the privilege inventory and the denial
  evidence at each stage — "separately recorded," not "independent," because
  this is a single-operator fleet and no distinct reviewer performs it (see
  below).
- **Operator acceptance** (Michael, operations capacity) accepts that
  replacement reports satisfy real operational workflows before a consumer is
  considered migrated.

This is a single-operator fleet: one person acts in all four capacities
above, and each is named separately here because the review each capacity
performs is a distinct check, not because they are different individuals.

> Exception record: infrastructure authority (owner: infra/DB-ops) — pointer
> not yet created; to be filled in when the infra-side freeze record exists.
> This ADR/runbook does not itself constitute the exception record.

Phase 0's hard exit gate — "owners accept the freeze and rollback
responsibility" — is NOT yet fully closed by this PR alone: this repository's
contribution (the architecture guard plus this ADR and runbook) is in place,
but the exception record above is still an empty pointer, and the gate closes
only once the infrastructure-side freeze record exists and the freeze is
accepted there too.

## Invariants

- `COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT` never gains a `dotmac_ro` entry.
- No migration, application code, or operational script in this repository
  ever references `dotmac_ro` (enforced by
  `tests/architecture/test_commercial_module_prerequisites.py`).
- `app_admin`, `app_user`, and `platform_api` remain unchanged by this
  retirement.
- Any future direct-SQL reporting need is met by a separate, individually
  audited, least-privilege plane — never by reviving `dotmac_ro`.

## Consequences

- No runtime behavior changes as a result of this ADR. The retirement's
  operational phases (4–6) happen in infrastructure, under separate explicit
  authorization each, entirely outside this repository.
- A permanent architecture guard exists so `dotmac_ro` cannot silently
  re-enter this repository's migrations, application code, or scripts while
  the broader retirement is in flight.
- Rejected alternative: adding `dotmac_ro` to the closed role contract as a
  fourth "reporting" application role. Rejected because the contract's value
  is that every member is a scoped, reviewed identity with a known
  `bypass_rls`/`superuser` posture — folding in an inherited, broad-grant
  legacy role would defeat that closure rather than extend it.

## Migration and cutover

- Old owner and paths: none — this refers specifically to version control:
  this repository has never owned or tracked `dotmac_ro`, so there is no
  repo-code migration path to retire. This is a distinct statement from the
  "Ownership boundary" subsection above, which assigns who is accountable for
  the role's actual production lifecycle going forward; `dotmac_ro` itself is
  observed only on the known production cluster's infrastructure state,
  outside version control — whether it also exists elsewhere in the fleet or
  on DR/standby infrastructure is unverified from here (Phase 1 work),
  consistent with the Context section above.
- New owner and paths: none created by this ADR. Phases 1–3
  (`docs/runbooks/DOTMAC_RO_RETIREMENT.md`) are NOT uniformly
  repo-actionable — per-action ownership is annotated in the runbook's
  phase detail (Phase 1's live production/DR data capture and part of
  Phase 3's monitoring are infra-owned, not repo-actionable); only Phase 2
  (typed operator reports satisfying the approved read-only snapshot
  property via `read_only_snapshot_session()` or
  `begin_read_only_snapshot(db)`) and the deploy-through-normal-release-path
  portion of Phase 3 are fully Sub-repo-actionable. Phases 4–6 are
  infrastructure-owned and out of this repository's scope entirely.
- Backfill/repair: not applicable — no data ownership changes here.
- Shadow or verification phase: Phase 2 (Replace) requires each replacement
  report to carry negative tests before a consumer is considered cut over.
- Cutover gate and evidence: as stated per phase above; full detail in the
  runbook.
- Fallback retirement: not applicable to this slice.
- Schema contract step: none — this ADR makes no schema or role change.

## Verification

- This slice: `tests/architecture/test_commercial_module_prerequisites.py`
  is a LITERAL-IDENTIFIER RATCHET, not a proof of absence — it catches the
  literal string `dotmac_ro` reappearing as a standalone identifier in a
  tracked file's RAW BYTES, in either of two covered byte-level encodings
  (ASCII/UTF-8 and UTF-16, both byte orders), and cannot prove the absence
  of a constructed or obfuscated reference (string concatenation, another
  encoding, an environment-variable name that only resolves to `dotmac_ro`
  at runtime, unicode homoglyphs). It scans every git-tracked file in this
  repository that also satisfies its own enforceable completeness premises,
  checked against TWO separate sources of truth about the same path: the
  git INDEX (every tracked path's mode checked against an EXACT ALLOWLIST
  of `100644`/`100755` — checked first, on the index's word alone, before
  anything is read from disk; a mode outside the allowlist refuses,
  including `120000` symlink or `160000` gitlink/submodule and any mode
  this guard has never named before; see `_tracked_files_with_modes`'s
  docstring) and the WORKING TREE (no symlink
  or non-regular file actually encountered while walking to a leaf, no
  missing tracked path; see `_files_containing`'s docstring). An earlier
  version of this guard checked only the working tree, which left a real
  gap: a tracked symlink or gitlink locally replaced by an ordinary
  regular file on disk would have been scanned as if it were normal
  content, because a disk-only walk cannot see what only the index
  declares. A violated premise, from either source, REFUSES the scan
  rather than silently skipping the offending path — except the three
  files that must themselves name `dotmac_ro` to describe and forbid it
  (this guard test file, ADR-0016, and the companion runbook; excluded by
  exact relative-path match, not by directory or suffix, and asserted to be
  EXACTLY those three via a literal written independently of the exclusion
  tuple) — for the identifier `dotmac_ro`, case-insensitively in both
  encodings, and identifier-boundary-aware in ASCII/UTF-8 ONLY (not a plain
  substring, so it does not false-positive on a real identifier that
  merely starts with the same characters, such as `dotmac_router_ssh` or
  `dotmac_roles_r1_...`); the UTF-16 patterns (both byte orders) are
  deliberately UNBOUNDED — a boundary check cannot know a UTF-16 payload's
  byte alignment, and one that doesn't is a source of silent misses, not a
  refinement, so this guard accepts the wider over-report in that encoding
  instead. It fails the build if the identifier appears; every OTHER test
  file and every OTHER
  Markdown document — including other docs and other runbooks — is a real
  control surface and is scanned like any other tracked file. There is
  deliberately NO binary-file exemption: matching happens directly against
  raw bytes rather than after decoding to text, so it applies uniformly to
  this repository's tracked binary assets (images, fonts) too — a tracked
  SQLite fixture, compiled catalog, PDF, object file, or archived export
  can all carry the literal bytes `dotmac_ro`, and this scan finds them the
  same way it would in a `.py` file. A companion assertion proves
  `"dotmac_ro" not in COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT`. This replaced an
  earlier directory-allowlist design (`alembic/`, `app/`, `scripts/`,
  `.github/workflows/`, `deploy/`, `docker/`, `nginx/`, plus a top-level
  scan) that structurally could not keep up: it missed real tracked
  surfaces (`.github/actions/setup-ci-python/action.yml`,
  `config/freeradius/schema.sql`,
  `config/freeradius/sql/admin_schema.sql`) simply because nothing had
  added them to the list.
- Later phases: each phase's exit gate above is its own verification; Phase 1
  requires every privilege/consumer to be classified; Phase 6 requires an
  independent readback (defined in the Decision section's Phase 6 bullet: a
  separate query and evidence record, not a separate reviewer) proving
  absence, not merely the absence of an error.

## Rollback or forward-fix

Rollback of THIS PR is reverting the two documents
(`docs/adr/0016-retire-dotmac-ro-legacy-reporting-role.md`,
`docs/runbooks/DOTMAC_RO_RETIREMENT.md`) and the one test addition in
`tests/architecture/test_commercial_module_prerequisites.py` — it does not
touch, and cannot roll back, the retirement program itself, which lives in
infrastructure and has not yet taken any production action under this ADR.

## Review and retirement

- Review date: 2026-11-22
- Retirement condition: this ADR is retired/superseded when Phase 6 (Remove)
  completes and an independent readback (a separate query and evidence
  record, not a separate reviewer — defined in the Decision section's Phase
  6 bullet) confirms `dotmac_ro` is absent fleet-wide (see
  `docs/runbooks/DOTMAC_RO_RETIREMENT.md`). If Phase 2
  (Replace) has not reached acceptance by the review date above, the plan's
  scoping should be revisited.
- Supersedes or is superseded by: none yet.
