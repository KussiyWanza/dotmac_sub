"""Keep local, agent, and CI validation commands on one executable owner."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_makefile_owns_parallel_non_integration_suite() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert (
        "UNIT_TEST_PATHS := tests/ --ignore=tests/integration --ignore=tests/e2e"
        in makefile
    )
    assert "UNIT_TEST_WORKERS ?= auto" in makefile
    assert "-n $(UNIT_TEST_WORKERS)" in makefile
    assert "test-ci:" in makefile
    assert "test-integration:" in makefile


def test_ci_and_agent_guidance_call_makefile_validation_owners() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "make test-ci-shard" in workflow
    assert "scripts/ci/select_test_shard.py" in makefile
    assert "make test-architecture" in workflow
    assert "run: make test-integration" in workflow
    assert "make test-architecture" in guidance
    assert "make test\n" in guidance
    assert "make test-integration" in guidance


def test_e2e_workflow_runs_the_browser_it_installs() -> None:
    workflow = (ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8")

    assert "playwright install chromium --with-deps" in workflow
    assert 'PLAYWRIGHT_BROWSER: "chromium"' in workflow
    assert 'E2E_JWT_SECRET="$(openssl rand -hex 32)"' in workflow
    assert '-e JWT_SECRET="$E2E_JWT_SECRET"' in workflow
    assert "POSTGRES_HOST_AUTH_METHOD=trust" in workflow
    assert "POSTGRES_PASSWORD" not in workflow
    assert (
        'TEST_DATABASE_URL: "postgresql+psycopg://postgres@127.0.0.1:55432/'
        'dotmac_sub_e2e"'
    ) in workflow
    assert "type: choice" in workflow
    assert "- service-teams" in workflow
    assert 'if [ "$E2E_SUITE" = "service-teams" ]; then' in workflow
    assert "tests/playwright/e2e/test_service_teams.py" in workflow
