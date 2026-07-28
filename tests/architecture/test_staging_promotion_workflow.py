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
    assert 'REQUIRE_PROXY_HANDOFF: "0"' in workflow
    assert "10.120.121.20:8001:8001/tcp" in workflow
    assert "grep -qx celery-beat" in workflow


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
    assert "Do not edit `VERSION` in the source pull request" in runbook
    assert (
        "A failed staging deployment never authorizes promotion to `main`." in runbook
    )
