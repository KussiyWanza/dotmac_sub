from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_dev_pull_requests_run_required_ci_gates() -> None:
    for path in (".github/workflows/ci.yml", ".github/workflows/mobile.yml"):
        workflow = _read(path)
        assert "pull_request:\n    branches: [main, dev]" in workflow

    version_impact = _read(".github/workflows/version-impact.yml")
    assert "pull_request:" in version_impact
    assert "branches: [main, dev]" in version_impact


def test_ghcr_isolated_to_the_pinned_genieacs_runtime() -> None:
    workflow = _read(".github/workflows/ghcr.yml")

    assert "branches: [main]" in workflow
    assert "docker/genieacs/**" in workflow
    assert "context: docker/genieacs" in workflow
    assert workflow.count("uses: docker/build-push-action@v6") == 1
    assert "context: .\n" not in workflow
    assert "type=raw,value=latest" not in workflow


def test_release_candidate_build_is_explicit_green_dev_and_digest_evidenced() -> None:
    workflow = _read(".github/workflows/release-candidate.yml")

    assert yaml.safe_load(workflow)
    assert "on:\n  workflow_dispatch:" in workflow
    assert "on:\n  push:" not in workflow
    assert "candidate_sha:" in workflow
    assert "ref: dev" in workflow
    assert "WORKFLOW_REF: ${{ github.ref }}" in workflow
    assert '"refs/heads/dev"' in workflow
    assert 'const required = ["CI", "Mobile CI"]' in workflow
    assert "Refusing stale candidate" in workflow
    assert "Candidate image already exists" in workflow
    assert "Cannot safely determine whether" in workflow
    assert "runs-on: ubuntu-latest" in workflow
    assert "self-hosted" not in workflow
    assert workflow.count("uses: docker/build-push-action@v6") == 1
    assert "id: build" in workflow
    assert "${{ steps.build.outputs.digest }}" in workflow
    assert "python -m scripts.release_candidate_evidence write-candidate" in workflow
    assert "name: release-candidate-evidence" in workflow
    assert "retention-days: 90" in workflow


def test_staging_deploy_is_disabled_and_pinned_to_the_staging_host() -> None:
    workflow = _read(".github/workflows/staging-deploy.yml")

    # Parsing catches malformed YAML independently of the text contract checks.
    assert yaml.safe_load(workflow)
    assert 'workflows: ["Build release candidate once"]' in workflow
    assert "branches: [dev]" in workflow
    assert "vars.STAGING_AUTO_DEPLOY_ENABLED == 'true'" in workflow
    assert "github.event.workflow_run.event == 'workflow_dispatch'" in workflow
    assert (
        "github.event.workflow_run.head_repository.full_name == github.repository"
        in workflow
    )
    assert 'const required = ["CI", "Mobile CI"]' in workflow
    assert "Refusing stale staging deploy" in workflow
    assert "runs-on: [self-hosted, linux, x64, dotmac-sub-staging]" in workflow
    assert "environment: staging" in workflow
    assert 'expected_dir="/home/dotmac/deploy-worktrees/dotmac-sub-staging"' in workflow
    assert "/home/dotmac/projects/dotmac_sub" not in workflow
    assert 'test -e "$STAGING_DEPLOY_DIR/.git"' in workflow
    assert "rev-parse --is-inside-work-tree" in workflow
    assert "Check out exact candidate without moving deployment branches" in workflow
    assert 'git -C "$STAGING_DEPLOY_DIR" checkout --detach "$CANDIDATE_SHA"' in workflow
    assert 'git -C "$STAGING_DEPLOY_DIR" symbolic-ref -q HEAD' in workflow
    for forbidden_branch_mutation in (
        'git -C "$STAGING_DEPLOY_DIR" checkout dev',
        'git -C "$STAGING_DEPLOY_DIR" merge --ff-only',
        'git -C "$STAGING_DEPLOY_DIR" branch --force',
        "git reset --hard",
    ):
        assert forbidden_branch_mutation not in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "python -m scripts.release_candidate_evidence verify-candidate" in workflow
    assert 'bash scripts/deploy_staging.sh "$IMAGE_DIGEST"' in workflow
    assert 'bash scripts/deploy.sh "$IMAGE_DIGEST"' not in workflow
    assert 'expected_image="ghcr.io/michaelayoade/dotmac_sub@$IMAGE_DIGEST"' in workflow
    assert "io.dotmac.release.source-tree" in workflow
    assert "io.dotmac.release.build-run" in workflow
    assert (
        "python -m scripts.release_candidate_evidence write-staging-acceptance"
        in workflow
    )
    assert (
        "name: staging-acceptance-${{ needs.verify.outputs.candidate_sha }}" in workflow
    )
    assert "10.120.121.20:8001:8001/tcp" in workflow
    assert "grep -qx celery-beat" in workflow

    deploy_job = workflow[
        workflow.index("  deploy:\n") : workflow.index("  record-acceptance:\n")
    ]
    for forbidden in (
        "pytest",
        "make test",
        "ruff",
        "mypy",
        "lint-imports",
        "bandit",
        "docker build",
    ):
        assert forbidden not in deploy_job
    acceptance_job = workflow[workflow.index("  record-acceptance:\n") :]
    assert "runs-on: ubuntu-latest" in acceptance_job

    staging_adapter = _read("scripts/deploy_staging.sh")
    assert 'require_exact_env_line "APP_ENV=staging"' in staging_adapter
    assert 'require_exact_env_line "SERVER_NAME=dotmac-sub-staging"' in staging_adapter
    assert (
        'require_exact_env_line "HEALTH_URL=http://10.120.121.20:8001/health"'
        in staging_adapter
    )
    assert "unset SKIP_BACKUP" in staging_adapter
    assert "export DEPLOY_BACKUP_MODE=skip_staging" in staging_adapter
    assert "export REQUIRE_PROXY_HANDOFF=0" in staging_adapter
    assert "export HEALTH_TIMEOUT_SECONDS=600" in staging_adapter
    assert 'exec bash "${ROOT_DIR}/scripts/deploy.sh" "$@"' in staging_adapter


def test_agents_guidance_requires_staging_before_main() -> None:
    guidance = " ".join(_read("AGENTS.md").split())

    assert "explicit one-time candidate build" in guidance
    assert "immutable candidate digest -> staging deployment and acceptance" in guidance
    assert "After all source and rolling version pull requests have merged" in guidance
    assert "Deploy that exact OCI" in guidance
    assert "A dev image is staging-only" in guidance
    assert "must never receive the `latest` tag" in guidance
    assert "one-time bootstrap promotion" in guidance
    assert "cannot dispatch a new workflow" in guidance
    assert "Require the resulting `main` CI" in guidance
    assert "source pull requests do not edit `VERSION`" in guidance
    assert "automation owns the separate rolling" in guidance


def test_staging_promotion_runbook_records_activation_and_failure_contracts() -> None:
    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "STAGING_AUTO_DEPLOY_ENABLED" in runbook
    assert "dotmac-sub-staging" in runbook
    assert (
        "STAGING_DEPLOY_DIR=/home/dotmac/deploy-worktrees/dotmac-sub-staging" in runbook
    )
    assert "used only by the staging" in runbook
    assert "never write into the deployment worktree" in runbook
    assert "leaves every local branch pointer unchanged" in runbook
    assert "detached `HEAD`" in runbook
    assert "scripts/deploy_staging.sh" in runbook
    assert "Build release candidate once" in runbook
    assert "release-candidate-evidence" in runbook
    assert "staging-acceptance-<source-sha>" in runbook
    assert "builds only on a GitHub-hosted runner" in runbook
    assert "## One-time workflow bootstrap" in runbook
    assert "workflow_dispatch" in runbook
    assert "Do not fabricate an" in runbook
    assert "Production requires a backup by default" in runbook
    assert "ten-minute health budget" in runbook
    assert "Do not edit `VERSION` in the source pull request" in runbook
    assert (
        "A failed staging deployment never authorizes promotion to `main`." in runbook
    )


def test_production_promotion_reuses_the_staged_digest_without_a_build() -> None:
    workflow = _read(".github/workflows/release-promotion.yml")

    assert yaml.safe_load(workflow)
    assert "on:\n  workflow_dispatch:" in workflow
    assert "ref: main" in workflow
    assert 'for (const workflowName of ["CI", "Mobile CI"])' in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert "authorize-production" in workflow
    assert (
        "const expectedRepository = `${context.repo.owner}/${context.repo.repo}`;"
        in workflow
    )
    assert '[stagingId, "Deploy dev to staging", "main", "workflow_run"]' in workflow
    assert "run.head_repository?.full_name !== expectedRepository" in workflow
    assert "run.head_repository.full_name !== context.repo.repo" not in workflow
    assert "docker buildx imagetools create" in workflow
    assert "--prefer-index=false" in workflow
    assert "production-authorization-${{ steps.release.outputs.sha }}" in workflow
    assert "docker/build-push-action" not in workflow
    assert "docker build " not in workflow
    assert "self-hosted" not in workflow


def test_production_deploy_requires_authorization_and_runs_no_test_suite() -> None:
    workflow = _read(".github/workflows/production-deploy.yml")
    deploy = _read("scripts/deploy.sh")
    adapter = _read("scripts/deploy_production.sh")

    assert yaml.safe_load(workflow)
    assert "PRODUCTION_DEPLOY_ENABLED" in workflow
    assert "target_server_name" in workflow
    assert "dotmac-sub-prod" in workflow
    assert "runs-on: [self-hosted, linux, x64, dotmac-sub-production]" in workflow
    assert "environment: production" in workflow
    assert "verify-production" in workflow
    assert (
        "const expectedRepository = `${context.repo.owner}/${context.repo.repo}`;"
        in workflow
    )
    assert "run.head_repository?.full_name !== expectedRepository" in workflow
    assert "run.head_repository.full_name !== context.repo.repo" not in workflow
    assert "bash scripts/deploy_production.sh" in workflow
    for forbidden in ("pytest", "make test", "ruff", "mypy", "lint-imports", "bandit"):
        assert forbidden not in workflow

    assert "production does not accept SKIP_BACKUP=1" in deploy
    assert "verify-production-decision" in deploy
    assert "deploy_production.sh" in adapter
    assert "write-production-decision" in adapter
    assert "SELECT version_num FROM alembic_version ORDER BY version_num" in adapter


def test_runbook_records_the_merge_method_per_pull_request_kind() -> None:
    """Squash-merging a promotion stops main from being an ancestor of dev.

    The next promotion opened with head=dev then conflicts on version metadata,
    which is what produced the repeated reconciliation commits this rule ends.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "## Merge methods" in runbook
    assert "| Promotion, `dev` into `main` | **Merge commit** |" in runbook
    assert "| Reconciliation, `main` into `dev` | **Merge commit** |" in runbook
    assert "| Feature or fix into `dev` | Squash |" in runbook
    assert "git merge-base --is-ancestor origin/main origin/dev" in runbook


def test_runbook_explains_why_dev_requires_no_approving_review() -> None:
    """The rule was added and reverted within an hour on 2026-07-31.

    Single-account automation cannot satisfy a review requirement: bump and
    agent pull requests are authored by the only admin, nobody may self-approve,
    so every automated merge becomes an admin override. Recording why keeps it
    from being re-added, and names the precondition for reconsidering it.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "an approving review is not" in runbook
    assert "VERSION_BUMP_TOKEN" in runbook
    # The deadlock that makes GITHUB_TOKEN the wrong identity must stay stated.
    assert "GITHUB_TOKEN" in runbook
    assert "permanently unmergeable" in runbook


def test_runbook_requires_pull_request_sync_after_a_promotion() -> None:
    """The promotion merge commit exists only on main, leaving dev behind.

    Ancestry therefore breaks again the moment a promotion lands, and the next
    one re-enters the reconciliation path the merge-method rule exists to
    remove. Observed live on 2026-07-31 promoting 7.77.3.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "Synchronize `dev` after a promotion" in runbook
    # The step belongs in the numbered sequence, not only in a later section,
    # because that is what someone actually follows during a release.
    sequence = runbook[
        runbook.index("## Promotion sequence") : runbook.index("## Merge methods")
    ]
    assert "zero-file pull request" in sequence
    assert "gh pr create --base dev" in runbook
    assert "rejects even a non-force fast-forward" in runbook


def test_runbook_warns_that_merging_a_promotion_deletes_dev() -> None:
    """delete_branch_on_merge deletes the head branch, and a promotion's head is dev.

    Hit on 2026-07-31: merging the promotion removed `dev` outright. Branch
    protection prevents it now, so this records why that protection exists and
    must not be removed.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "delete_branch_on_merge" in runbook
    assert "allow_deletions" in runbook
    # gh's --delete-branch governs only the local branch; the remote deletion
    # comes from the repository setting. Mistaking the two is the whole trap.
    assert "--delete-branch" in runbook
    # Recreating at the former head would leave the branches diverged again.
    assert "not at its own former head" in runbook


def test_dev_first_gate_refuses_pull_requests_that_bypass_staging() -> None:
    workflow = _read(".github/workflows/dev-first-gate.yml")

    assert yaml.safe_load(workflow)
    assert "branches: [main]" in workflow

    # Exactly these heads reach main. `dev` is matched exactly, so a branch
    # merely starting with "dev" (develop, dev-experiment) is still refused.
    assert "dev|agent/promote-*|agent/reconcile-*|promote/*|reconcile/*)" in workflow

    # A production incident must never be blocked outright, and the escape
    # hatch has to be visible rather than silent.
    assert "dev-first:override" in workflow
    assert "::warning::Dev-first gate overridden" in workflow

    # The failure has to teach the flow; a bare non-zero exit does not.
    assert "gh pr edit ${PR_NUMBER} --base dev" in workflow
    assert "docs/runbooks/STAGING_PROMOTION.md" in workflow
