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


def test_ghcr_builds_dev_and_main_but_latest_remains_default_branch_only() -> None:
    workflow = _read(".github/workflows/ghcr.yml")

    assert "push:\n    branches: [main, dev]" in workflow
    assert "type=sha" in workflow
    assert "type=raw,value=latest,enable={{is_default_branch}}" in workflow


def test_staging_deploy_is_disabled_and_pinned_to_the_staging_host() -> None:
    workflow = _read(".github/workflows/staging-deploy.yml")

    # Parsing catches malformed YAML independently of the text contract checks.
    assert yaml.safe_load(workflow)
    assert 'workflows: ["Build & Push to GHCR"]' in workflow
    assert "branches: [dev]" in workflow
    assert "vars.STAGING_AUTO_DEPLOY_ENABLED == 'true'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert (
        "github.event.workflow_run.head_repository.full_name == github.repository"
        in workflow
    )
    assert 'const required = ["CI", "Mobile CI"]' in workflow
    assert "Refusing stale staging deploy" in workflow
    assert "runs-on: [self-hosted, linux, x64, dotmac-sub-staging]" in workflow
    assert "environment: staging" in workflow
    assert 'expected_dir="/home/dotmac/projects/dotmac_sub"' in workflow
    assert 'git -C "$STAGING_DEPLOY_DIR" merge --ff-only' in workflow
    assert "git reset --hard" not in workflow
    assert 'bash scripts/deploy_staging.sh "$IMAGE_TAG"' in workflow
    assert 'bash scripts/deploy.sh "$IMAGE_TAG"' not in workflow
    assert "10.120.121.20:8001:8001/tcp" in workflow
    assert "grep -qx celery-beat" in workflow

    staging_adapter = _read("scripts/deploy_staging.sh")
    assert 'require_exact_env_line "APP_ENV=staging"' in staging_adapter
    assert 'require_exact_env_line "SERVER_NAME=dotmac-sub-staging"' in staging_adapter
    assert (
        'require_exact_env_line "HEALTH_URL=http://10.120.121.20:8001/health"'
        in staging_adapter
    )
    assert "export SKIP_BACKUP=1" in staging_adapter
    assert "export REQUIRE_PROXY_HANDOFF=0" in staging_adapter
    assert "export HEALTH_TIMEOUT_SECONDS=600" in staging_adapter
    assert 'exec bash "${ROOT_DIR}/scripts/deploy.sh" "$@"' in staging_adapter


def test_agents_guidance_requires_staging_before_main() -> None:
    guidance = _read("AGENTS.md")

    assert "immutable dev image -> staging deployment and acceptance" in guidance
    assert "A dev image is staging-only" in guidance
    assert "must never receive the `latest` tag" in guidance
    assert "Require the resulting `main` CI" in guidance
    assert "source pull requests do not edit `VERSION`" in guidance
    assert "automation owns the separate rolling" in guidance


def test_staging_promotion_runbook_records_activation_and_failure_contracts() -> None:
    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "STAGING_AUTO_DEPLOY_ENABLED" in runbook
    assert "dotmac-sub-staging" in runbook
    assert "STAGING_DEPLOY_DIR=/home/dotmac/projects/dotmac_sub" in runbook
    assert "updates local `dev` only by fast-forward" in runbook
    assert "scripts/deploy_staging.sh" in runbook
    assert "Production backup behavior remains unchanged" in runbook
    assert "ten-minute health budget" in runbook
    assert "Do not edit `VERSION` in the source pull request" in runbook
    assert (
        "A failed staging deployment never authorizes promotion to `main`." in runbook
    )


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


def test_runbook_requires_fast_forwarding_dev_after_a_promotion() -> None:
    """The promotion merge commit exists only on main, leaving dev behind.

    Ancestry therefore breaks again the moment a promotion lands, and the next
    one re-enters the reconciliation path the merge-method rule exists to
    remove. Observed live on 2026-07-31 promoting 7.77.3.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "Fast-forward `dev` to `main`" in runbook
    # The step belongs in the numbered sequence, not only in a later section,
    # because that is what someone actually follows during a release.
    sequence = runbook[
        runbook.index("## Promotion sequence") : runbook.index("## Merge methods")
    ]
    assert "Fast-forward `dev` to `main`" in sequence
    # A fast-forward, not a force push — branch protection forbids the latter.
    assert "refs/heads/dev" in runbook
    assert "not a force" in runbook


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
