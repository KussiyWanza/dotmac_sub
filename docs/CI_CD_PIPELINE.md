# CI/CD pipeline

The `CI` workflow is the deployment-quality owner for pull requests targeting
`dev` or `main`, merge groups, and promotion through `dev` and `main`.

## Required gates

Application changes retain all release gates:

- lint, formatting, typing, import-boundary, pre-commit, and blocking security
  checks;
- the complete non-integration suite, split deterministically across four
  runners, plus the explicit four-worker architecture suite;
- Alembic and integration tests against PostgreSQL before merge and again on
  promotion branches; and
- one production Docker build followed by migration and health checks.

Pull requests receive the Docker migration and health gate. Branch pushes do
not repeat that build in CI: `ghcr.yml` is the sole publisher for `dev` and
`main` and remains the named trigger for staging deployment. Dev receives only
its immutable SHA tag; only the default branch receives `latest`. The BuildKit
cache scope `dotmac-sub-application` is shared by pre-merge validation and
publication so unchanged layers are reused without unrelated builds
overwriting the cache history.

Browser E2E remains nightly and manually dispatchable in `e2e.yml`. It pulls
the immutable `sha-<commit>` image published by CI, rather than rebuilding it,
and is not a per-change merge gate.

## Dependency reuse

`python-environment` is the only CI job that installs Poetry or resolves
dependencies. Poetry 2.4.1 is pinned and cached by exact runner Python version.
Its installation and the exact project environment are packed into a one-day
workflow artifact and restored by the local `setup-ci-python` composite action.
The project environment cache is keyed by operating system, exact Python
version, Poetry version, `poetry.lock`, and `pyproject.toml`. Parallel jobs
therefore do not reinstall Poetry or the dependency set. Both setup paths first
remove the repository's workstation-only `.venv` symlink from the runner
checkout so the CI environment is always created inside the writable workspace.

The production image installs only Poetry's `main` dependency group. Test,
lint, type-check, browser, and pre-commit tools belong only to
`dependency-groups.dev`; do not duplicate them under
`project.optional-dependencies`, because Poetry then records those packages in
the main lock group and includes them in `poetry install --only main`.

## Change classification

A change is documentation-only only when every changed path is under `docs/`
or is a Markdown file. Required check names still report, but application
tests, PostgreSQL services, Docker builds, and security scans do no heavy work.
Workflow, dependency, migration, test, and application changes always receive
the full pipeline. Manual runs also always execute the full pipeline.

## Unit-test sharding

`scripts/ci/select_test_shard.py` assigns every non-integration,
non-architecture test file to exactly one of four shards. The first run uses
source size as a deterministic fallback. Every completed run records aggregate
execution time per test file, combines the four records, and stores the small
duration index in the GitHub Actions cache. Later runs greedily balance by those
measured durations, while new files retain the source-size fallback. Each shard
still uses exactly four pytest-xdist workers. A worker crash fails the shard
without a silent restart, and `pytest-timeout` terminates any individual test
that exceeds 180 seconds while emitting thread stacks for diagnosis. The
worker count and timeout can be overridden locally through
`CI_UNIT_TEST_WORKERS` and `CI_TEST_TIMEOUT_SECONDS`; CI retains the checked-in
defaults. Coverage data from all shards is combined before the XML report is
published.

Architecture guards remain a separate required group. This prevents the same
architecture suite from being collected and run as part of every general unit
shard while retaining the repository's measured four-worker architecture
default. High-volume static guards share a worker-local index of file listings,
source text, and parsed ASTs, and the job reports its 50 slowest checks so the
next optimization remains evidence-led.

GitHub-hosted CI owns every full pytest suite. Deployment hosts are runtime
acceptance environments, not test runners: production refuses every pytest
invocation, while staging permits only a serial selection of at most ten
explicit Python test files for focused diagnosis. Full-suite Make targets and
direct pytest collection both evaluate the non-secret `APP_ENV` and
`SERVER_NAME` host markers before heavy setup begins. This preserves the same
required checks without allowing validation load to starve the deployed app,
workers, or database.

The deployment owner independently verifies the exact immutable image revision
before mutating a host. `scripts/deploy.sh` requires successful `CI` and
`Mobile CI` push workflow runs for that full SHA on `main` in production or
`dev` in staging. It performs this GitHub API check before the database backup,
migrations, or service replacement. The on-host schema, integration-readiness,
web-health, and worker-health gates are runtime acceptance checks, not pytest.

The staging runner uses the dedicated persistent worktree
`/home/dotmac/deploy-worktrees/dotmac-sub-staging`. Interactive development,
agents, and diagnostic edits must use other worktrees. Sharing a checkout lets
development writes race deployment verification, so the workflow pins the
dedicated path and refuses tracked changes rather than discarding them.

Each unit-test shard and the architecture job also has a 30-minute job timeout.
The per-test watchdog catches a blocked unit test first; the job timeout remains
the outer guard for collection, environment, plugin, and architecture-suite
failures that occur outside an individual unit test.
