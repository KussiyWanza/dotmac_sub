from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
SHARD_SCRIPT = ROOT / "scripts/ci/select_test_shard.py"


def _load_shard_module():
    spec = importlib.util.spec_from_file_location("select_test_shard", SHARD_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unit_shards_partition_all_unit_test_files_once(tmp_path: Path) -> None:
    module = _load_shard_module()
    expected = set(module._test_files())
    durations_path = tmp_path / "missing-durations.json"
    groups = [
        set(
            module.select_shard(
                shard=shard,
                shards=4,
                durations_path=durations_path,
            )
        )
        for shard in range(1, 5)
    ]

    assert set().union(*groups) == expected
    assert sum(len(group) for group in groups) == len(expected)


def test_unit_shards_prefer_measured_durations_over_source_size(
    tmp_path: Path,
) -> None:
    module = _load_shard_module()
    paths = module._test_files()
    durations = {path.relative_to(ROOT).as_posix(): 1.0 for path in paths}
    slowest = min(paths, key=lambda path: path.stat().st_size)
    durations[slowest.relative_to(ROOT).as_posix()] = 1000.0
    durations_path = tmp_path / "test-durations.json"
    durations_path.write_text(
        json.dumps({"schema_version": 1, "durations": durations}),
        encoding="utf-8",
    )

    groups = [
        module.select_shard(
            shard=shard,
            shards=4,
            durations_path=durations_path,
        )
        for shard in range(1, 5)
    ]

    assert groups[0] == [slowest]


def test_ci_uses_one_named_application_cache_and_one_branch_publisher() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    e2e_workflow = (ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8")
    ghcr_workflow = (ROOT / ".github/workflows/ghcr.yml").read_text(encoding="utf-8")

    assert workflow.count("uses: docker/build-push-action@v6") == 1
    assert "scope=dotmac-sub-application" in workflow
    assert "github.event_name != 'push'" in workflow
    assert "docker push" not in workflow
    assert "docker/build-push-action" not in e2e_workflow
    assert 'image_tag="sha-${GITHUB_SHA::7}"' in e2e_workflow
    # ghcr.yml publishes exactly two images: the application and the pinned
    # GenieACS runtime (built in CI so no prod host needs a source checkout).
    # Each publisher keeps its own named buildx cache scope, and only the
    # application image may carry the moving `latest` tag — GenieACS is pinned
    # to the version parsed from its Dockerfile.
    assert ghcr_workflow.count("uses: docker/build-push-action@v6") == 2
    assert "branches: [main, dev]" in ghcr_workflow
    assert (
        ghcr_workflow.count("type=raw,value=latest,enable={{is_default_branch}}") == 1
    )
    assert "scope=dotmac-sub-application" in ghcr_workflow
    assert "scope=genieacs" in ghcr_workflow
    assert "-genieacs:${{ steps.version.outputs.version }}" in ghcr_workflow
    assert "genieacs:latest" not in ghcr_workflow


def test_ci_removes_workstation_venv_pointer_before_cache_and_restore() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    setup_action = (ROOT / ".github/actions/setup-ci-python/action.yml").read_text(
        encoding="utf-8"
    )

    assert "if [ -L .venv ]; then" in workflow
    assert "if [ -L .venv ]; then" in setup_action
    assert workflow.index("if [ -L .venv ]; then") < workflow.index("path: .venv")
    assert setup_action.index("if [ -L .venv ]; then") < setup_action.index(
        'tar -xzf "$RUNNER_TEMP/python-environment/python-venv.tar.gz"'
    )


def test_ci_pins_and_shares_one_poetry_installation() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    setup_action = (ROOT / ".github/actions/setup-ci-python/action.yml").read_text(
        encoding="utf-8"
    )

    assert 'POETRY_VERSION: "2.4.1"' in workflow
    assert "version: ${{ env.POETRY_VERSION }}" in workflow
    assert "poetry-home.tar.gz" in workflow
    assert "poetry-home.tar.gz" in setup_action
    assert "snok/install-poetry" not in setup_action


def test_ci_persists_unit_test_duration_history() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "unit-test-durations-v1-" in workflow
    assert "scripts/ci/merge_test_durations.py" in workflow
    assert "make test-ci-shard" in workflow
    assert "test-ci-shard:" in makefile
    assert "--ci-durations-output" in makefile
    assert "--durations=50" in makefile


def test_ci_test_jobs_fail_closed_instead_of_hanging_indefinitely() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    unit_job = workflow[
        workflow.index("  unit-shards:\n") : workflow.index("  architecture:\n")
    ]
    architecture_job = workflow[
        workflow.index("  architecture:\n") : workflow.index("  test:\n")
    ]

    assert "    timeout-minutes: 30\n" in unit_job
    assert "    timeout-minutes: 30\n" in architecture_job
    assert "CI_UNIT_TEST_WORKERS ?= 4" in makefile
    assert "CI_TEST_TIMEOUT_SECONDS ?= 180" in makefile
    assert "-n $(CI_UNIT_TEST_WORKERS)" in makefile
    assert "--max-worker-restart=0" in makefile
    assert "--timeout=$(CI_TEST_TIMEOUT_SECONDS)" in makefile
    assert "--timeout-method=signal" in makefile
    assert "pytest-timeout==2.4.0" in project["dependency-groups"]["dev"]


def test_ci_change_classifier_fetches_missing_comparison_base() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    classifier = workflow[
        workflow.index("  changes:\n") : workflow.index("  python-environment:\n")
    ]

    existence_check = 'git cat-file -e "$base^{commit}"'
    targeted_fetch = 'git fetch --no-tags --depth=1 origin "$base"'
    path_diff = 'git diff --name-only "$base" HEAD'

    assert existence_check in classifier
    assert targeted_fetch in classifier
    assert classifier.index(existence_check) < classifier.index(targeted_fetch)
    assert classifier.index(targeted_fetch) < classifier.index(path_diff)


def test_ci_change_classifier_does_not_resolve_a_base_from_shallow_roots() -> None:
    """`git rev-list --max-parents=0 HEAD` returns two shas for a merge commit.

    Under `fetch-depth: 2` the shallow clone grafts both parents of a merge
    commit, so both look parentless. The two shas were then interpolated into a
    single refspec and the job died with `fatal: invalid refspec`, which
    happened for real when a branch was created at a promotion merge commit.

    With no base there is nothing to diff, so the classifier must fail safe to
    application rather than invent one.
    """

    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    classifier = workflow[
        workflow.index("  changes:\n") : workflow.index("  python-environment:\n")
    ]
    # Assert against what actually runs. The comment explaining why this
    # fallback was removed necessarily names the command it removed.
    executed = "\n".join(
        line for line in classifier.splitlines() if not line.strip().startswith("#")
    )

    assert "--max-parents=0" not in executed

    zero_sha_guard = '[ "$base" = "0000000000000000000000000000000000000000" ]'
    assert zero_sha_guard in executed

    # The no-base branch must short-circuit to application before any git
    # command consumes "$base"; running everything is the safe default.
    tail = executed[executed.index(zero_sha_guard) :]
    assert tail.index("application=true") < tail.index('git cat-file -e "$base')


def test_production_dependency_group_excludes_ci_tools() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "poetry.lock").read_text(encoding="utf-8"))
    dev_tools = {
        "bandit",
        "import-linter",
        "mypy",
        "playwright",
        "pre-commit",
        "pytest",
        "pytest-asyncio",
        "pytest-cov",
        "pytest-timeout",
        "pytest-xdist",
        "ruff",
        "vulture",
    }

    assert "dev" not in project["project"].get("optional-dependencies", {})
    locked_groups = {
        package["name"]: set(package["groups"])
        for package in lock["package"]
        if package["name"] in dev_tools
    }
    assert locked_groups
    assert all("main" not in groups for groups in locked_groups.values())


def test_ci_retains_pre_merge_and_promotion_postgresql_gate() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert workflow.count("branches: [main, dev]") == 2
    assert "make test-integration" in workflow
    assert "poetry run alembic upgrade head" in workflow
