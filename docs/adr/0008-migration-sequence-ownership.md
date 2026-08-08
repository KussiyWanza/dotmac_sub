# ADR 0008: Migration sequence ownership

Status: accepted

Date: 2026-08-07

Decision owner: Michael

Affected systems and domains: `alembic` revision chain, CI gates on `dev` and
`main`, `scripts/deploy.sh` migration step, branch protection on `dev`.

## Context

Nobody owns the invariant "the deployed schema has one coherent order". It is
asserted independently by **13 test files** (`tests/test_quote_send_permission_migration.py`,
`tests/test_billing_run_launch_evidence_migration.py`,
`tests/test_service_extension_reversal_migration.py`,
`tests/test_ont_reconcile_admission_migration.py`, and nine others), each
re-deriving `len(script.get_heads()) == 1`. Thirteen enforcers of one global
rule is the shape this repository's SOT standard exists to prevent: the
invariant has no named owner, so it is enforced everywhere and maintained
nowhere.

The concrete failure it produces: a migration's number and `down_revision` are
chosen **when a branch is created**, but validated **when it merges**. With
parallel branches that gap is days. Between 2026-08-06 and 2026-08-07 this
produced, in one repository, in two days:

- `480` claimed by three PRs; `482` by two; `483` by two; `487` by two;
  `496` by three (#2106, #2102, #2114 — all still open, all children of
  `495_plan_family_catalogues`).
- Seven migrations renumbered by hand, each costing a rebase and a CI cycle.
- One orphaned stacked child (`KeyError: '477_service_handoffs'`) surfacing at
  revision-map load, not as a merge conflict.
- One stacked merge duplicating a parent's migrations under both old and new
  numbers (12 conflicts, three `add/add`).

`scripts/new_migration.py` (#2073) reads the current head at authoring time.
That narrows the window; it does not close it, because the head still moves
between authoring and merge. #2114 was authored after that tool shipped and
still hand-picked a taken number.

Two facts constrain the options:

- **Production already tolerates multiple heads.** `scripts/deploy.sh:298` and
  `:598`, `Makefile:157`, and `docs/runbooks/PRODUCTION_DEPLOYMENT.md:33` all
  run `alembic upgrade heads`, plural. Linearity is not a deployment
  requirement; it is a test-imposed one.
- **Branch protection on `dev` has `strict: false` and no merge queue.** A pull
  request may merge against a base it has never seen. This is the mechanism by
  which a stale `down_revision` reaches `dev`.

The three open migrations touch disjoint tables (`usage_allowances`, vendor
route data, project numbers) and have no ordering dependency on each other.
The linear chain currently forces an order that the domain does not require.

## Decision

Proposed, pending Michael's approval. Three options are stated because the
choice is a genuine trade, not a formality.

**Option A — automated sequence owner (recommended).** The canonical order
stays linear. Branch protection on `dev` sets `strict: true` (or enables a
merge queue), and a CI check requires that a pull request adding a migration
has `down_revision` equal to the head of its base branch at merge time. A
colliding migration becomes **unmergeable** rather than discovered afterwards.
The 13 scattered assertions collapse into that single gate, which becomes the
named owner of the invariant. Drift is prevented by construction; no manual
step and no ambiguity is introduced.

**Option B — tolerate forks, reconcile at release.** Branches chain off
whatever head they saw. Multiple heads are legal on `dev`. The promotion flow
runs `alembic merge` to collapse them, and the single-head assertion moves from
per-PR to release gate. This removes all blocking but **permits drift and
reconciles it after the fact**, depends on a human remembering the merge step,
and makes `downgrade` ambiguous across merge points. It is the weakest fit for
the SOT standard and is recorded here because it is the cheapest to implement
and the alembic-native answer.

**Option C — status quo.** Continue renumbering by hand. Costs roughly one
rebase and CI cycle per collision, serializes unrelated pull requests behind
each other, and scales with parallelism.

## Invariants

Whichever option is accepted, these must hold:

- The deployed schema has exactly one coherent order at the moment of
  promotion to `main`.
- The invariant has exactly one named enforcer. No behaviour test re-derives
  it.
- A migration cannot reach `main` with a `down_revision` that is absent from
  the chain.
- Two migrations that modify the same table are never applied in an
  unspecified order.
- `tests/architecture/test_migration_chain_assertions.py` continues to reject
  any test pinning `get_heads()` to a literal. That guard is orthogonal to this
  decision and survives all three options.

## Consequences

**Option A.** One rebase for whoever merges second, made explicit and
deterministic instead of discovered in review. `strict: true` means every pull
request must be current before merging, which increases rebase traffic across
*all* pull requests, not only those with migrations — this is the main cost and
should be measured against the roughly 30 merges/day this repository sustains.
A merge queue softens it by rebasing automatically.

**Option B.** No pull request blocks another. Ordering between independent
migrations becomes unspecified, so an overlap guard (parsing `op.*` targets per
head) becomes load-bearing rather than advisory. Rollback through a merge point
is ambiguous. Reviewers lose "the chain is a straight line" as a mental model.

**Option C.** No new machinery. Continues to consume engineering time
proportional to parallelism, and blocks unrelated work: as of this ADR, #2102
and #2114 are complete and blocked behind #2106, which is itself blocked on an
unrelated Playwright failure in the ticket column picker.

## Migration and cutover

- **Old owner and paths:** 13 test files each asserting `len(heads) == 1`.
- **New owner and paths:** Option A — the merge-time CI gate plus branch
  protection. Option B — the promotion-time release gate.
- **Backfill/repair:** none. Existing revisions are unaffected; this changes
  only how future ones are admitted.
- **Shadow or verification phase:** run the new gate in report-only mode for
  one week, recording how many pull requests it would have blocked, before it
  becomes required.
- **Cutover gate and evidence:** the gate has blocked at least one real
  collision in report-only mode, and the 13 assertions are removed in the same
  change that makes it required.
- **Fallback retirement:** `scripts/new_migration.py` stays as an authoring
  convenience but stops being the primary defence; the workflow documentation
  must stop implying it is sufficient.
- **Schema contract step:** none.

## Verification

- Architecture: exactly one test asserts single-headedness after cutover.
- Behaviour: a pull request whose migration has a stale `down_revision` fails
  the gate; the same pull request rebased passes.
- Migration: `alembic upgrade heads` against a production-shaped database still
  succeeds from the currently deployed revision.
- Operational: promotion to `main` refuses when `dev` is not single-headed.
- Overlap (Option B only): the release gate fails when two heads modify the
  same table.

## Rollback or forward-fix

Reversible. Option A is branch-protection settings plus one CI job; reverting
restores today's behaviour with no data consequence. Option B is harder to
reverse *after* a merge revision exists on `main`, because collapsing a DAG back
into a line requires rewriting applied history — so if Option B is chosen, the
first merge revision is the point of no easy return.

Nothing here touches applied migrations or production data.

## Review and retirement

- Review date: 2026-09-07, or on the next collision after cutover, whichever is
  first.
- Retirement condition: superseded if the repository adopts a merge queue that
  makes the explicit gate redundant.
- Supersedes or is superseded by: none.
