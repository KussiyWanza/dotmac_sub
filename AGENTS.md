# Dotmac Sub repository guidance

This file applies to the whole repository. More specific checked-in guidance
may narrow it for a subdirectory but must not weaken the source-of-truth,
security, or validation rules below.

## Read before changing behavior

- `docs/CODING_STANDARD.md`
- `docs/SOT_RELATIONSHIP_MAP.md`
- `app/services/sot_relationships.py`
- `docs/UI_INFORMATION_AND_ACTION_STANDARD.md` for UI-facing work
- The owning domain design and migration documents

If these sources disagree, stop and report the conflict. Update the
authoritative documents in the same change that updates the contract.

## Source-of-truth rules

- Every fact, interpretation, decision, transition, projection, repair, and
  side effect has one named owner.
- Routes, API handlers, Celery tasks, event handlers, CLI commands, templates,
  and integrations are adapters. They do not own business decisions or
  transactions.
- Adapters create and close sessions. A registered public command owner controls
  the atomic business transaction. Nested helpers use `flush()` and never
  commit independently.
- An optional participant consequence may use only
  `app.services.owner_commands.execute_owner_savepoint`; its callback remains
  flush-only, and the owner must record durable failure evidence after rollback.
  Direct `begin_nested()`, savepoint completion, commit, or rollback in a
  participant is forbidden.
- New and migrated write owners enter
  `app.services.owner_commands.execute_owner_command` once on a
  transaction-free session. Adapters and nested helpers never call it.
- Observations are facts, not decisions. Resolvers and policies derive meaning
  and consequences from authoritative observations.
- Derived state must name its authoritative inputs, freshness semantics, drift
  signal, idempotent rebuild path, and repair owner.
- External systems and caches are transports or projections unless an approved
  contract explicitly assigns authority to them.
- A migrated boundary is incomplete until old writers and fallbacks are
  removed, existing drift can be repaired, and architecture tests prevent the
  parallel path from returning.

## Coding rules

- Typed contracts are mandatory for every implementation change: features,
  fixes, migrations, jobs, scripts, routes, tasks, webhooks, integrations, and
  tests must use the same typed command/query/event/value-object boundary they
  exercise. There is no "small change" or "internal process" exception.
- A change that touches an untyped cross-component boundary migrates that
  boundary in the same change. Leaving it untyped requires an explicitly
  approved architecture decision naming the owner, rationale, drift control,
  and retirement plan.
- Public commands and queries use typed inputs and typed outcomes.
- New and materially changed owner interfaces use precise identifier,
  collection, optional, enum, value-object, and provenance types. Do not expose
  `Any` containers or free-form primitive bags as domain contracts.
- Keep domain values typed internally. Serialize UUIDs, enums, decimals, dates,
  and value objects explicitly at adapter, persistence, or reporting boundaries.
- Domain services raise domain errors. HTTP responses, redirects, task retries,
  and transport-specific errors are mapped only by adapters.
- Never mass-assign untrusted mappings to ORM entities. Update an explicit set
  of fields from a validated command.
- State-changing commands define locking, idempotency, audit, event, and retry
  semantics. Financial, access, identity, provisioning, and destructive
  commands fail closed on stale or ambiguous inputs.
- Domain events are staged transactionally with the authoritative state change.
  Delivery happens after commit through the durable dispatcher/outbox.
- Use structured logging. Do not log secrets, credentials, private payloads, or
  unnecessary customer identity data.
- Migrations follow expand, backfill, verify, cut over, and contract. Destructive
  or irreversible steps require an approved design and operator runbook.

## Change workflow

- Work on a feature branch; never commit directly to `main`.
- Follow the mandatory promotion sequence for every release change:
  feature branch -> `dev` -> `origin/dev` -> prescribed validation on that exact
  remote commit -> immutable dev image -> staging deployment and acceptance ->
  `main` -> immutable main image -> production deployment.
- Do not merge a feature branch directly into `main`. Merge it into `dev`,
  update `origin/dev`, and require the repository-prescribed tests and CI to
  pass on that exact `origin/dev` commit before merging `dev` into `main`.
- Deploy the validated immutable `origin/dev` image to the explicitly named
  staging host and complete staging acceptance before promoting `dev` to
  `main`. A dev image is staging-only and must never receive the `latest` tag.
- Promote only the staged and accepted code from `dev` to `main`; do not add
  unrelated changes during promotion. Require the resulting `main` CI and
  immutable image build to pass before any production deployment.
- Keep each implementation slice coherent and reviewable even when several
  slices are assembled into a larger release.
- Every pull request must declare exactly one appropriate `version:major`,
  `version:minor`, `version:patch`, or `version:none` label. Release-impacting
  source pull requests do not edit `VERSION`; after merge, the checked-in
  automation owns the separate rolling `chore: bump version to X.Y.Z` pull
  request for the target branch.
- Do not commit, push, open or update a pull request, merge, release, deploy, or
  perform production work unless Michael explicitly requests that action.
- Production or SSH work requires Michael to name the target host.
- A source-of-truth slice must update the executable registry, relationship map
  or generator, focused behavior tests, architecture guards, and relevant
  operator/developer documentation together.
- New and migrated registry services require a complete typed `ServiceContract`
  from `app/services/sot_manifest.py`. Do not add names to the shrink-only
  legacy manifest baseline.

## Validation

Run the checks appropriate to the changed surface. Before publication, run the
full repository-prescribed suite:

```bash
poetry run ruff check app tests scripts alembic
poetry run ruff format --check app tests scripts alembic
poetry run mypy app --ignore-missing-imports --no-incremental
poetry run lint-imports
poetry run bandit -r app -c pyproject.toml -q
make test-architecture
make test
make test-integration
```

`make test` is the canonical parallel non-integration suite used by CI. It uses
all detected workers by default; set `UNIT_TEST_WORKERS=4` (or another explicit
limit) when local CPU or memory is constrained. Set `UNIT_TEST_WORKERS=0` for
serial execution only when isolating worker-order or shared-state failures.

Also run migration and browser/mobile checks when the changed behavior reaches
those surfaces. Report any skipped or failed check explicitly.
