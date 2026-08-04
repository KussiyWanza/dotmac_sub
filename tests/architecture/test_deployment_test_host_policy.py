"""Keep full validation off staging and production service hosts."""

from __future__ import annotations

from pathlib import Path

from scripts.testing.host_test_policy import (
    DeploymentHostKind,
    HostTestPolicyError,
    classify_test_host,
    decide_host_test,
    enforce_pytest_host_policy,
    parse_pytest_invocation,
    require_full_suite_host,
)

ROOT = Path(__file__).resolve().parents[2]


def _write_identity(root: Path, *, app_env: str, server_name: str) -> None:
    (root / ".env").write_text(
        f"APP_ENV={app_env}\nSERVER_NAME={server_name}\nIGNORED_SECRET=not-read\n",
        encoding="utf-8",
    )


def test_ci_without_deployment_markers_may_run_full_suite(tmp_path: Path) -> None:
    identity = classify_test_host(repo_root=tmp_path, environ={})

    assert identity.kind is DeploymentHostKind.NON_DEPLOYMENT
    assert decide_host_test(
        identity=identity,
        invocation=None,
        full_suite_owner=True,
    ).allowed


def test_production_refuses_even_a_focused_pytest_file(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        app_env="production",
        server_name="dotmac-sub-prod",
    )
    focused = tmp_path / "test_focused.py"
    focused.write_text("def test_focused(): pass\n", encoding="utf-8")

    try:
        enforce_pytest_host_policy(
            repo_root=tmp_path,
            argv=(str(focused),),
            environ={},
        )
    except HostTestPolicyError as exc:
        assert "forbidden on the production host" in str(exc)
    else:
        raise AssertionError("production pytest invocation was not refused")


def test_staging_refuses_full_suite_directory(tmp_path: Path) -> None:
    _write_identity(tmp_path, app_env="staging", server_name="dotmac-sub-staging")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    try:
        enforce_pytest_host_policy(
            repo_root=tmp_path,
            argv=(str(tests_dir),),
            environ={},
        )
    except HostTestPolicyError as exc:
        assert "at most 10 explicitly named" in str(exc)
    else:
        raise AssertionError("staging full-suite invocation was not refused")


def test_staging_allows_focused_serial_files(tmp_path: Path) -> None:
    _write_identity(tmp_path, app_env="staging", server_name="dotmac-sub-staging")
    focused = tmp_path / "test_focused.py"
    focused.write_text("def test_focused(): pass\n", encoding="utf-8")

    invocation = parse_pytest_invocation(
        repo_root=tmp_path,
        argv=(f"{focused}::test_focused", "-q"),
        environ={},
    )
    identity = classify_test_host(repo_root=tmp_path, environ={})

    assert decide_host_test(identity=identity, invocation=invocation).allowed


def test_staging_refuses_parallel_workers_from_cli_or_addopts(tmp_path: Path) -> None:
    _write_identity(tmp_path, app_env="staging", server_name="dotmac-sub-staging")
    focused = tmp_path / "test_focused.py"
    focused.write_text("def test_focused(): pass\n", encoding="utf-8")

    for argv, environ in (
        ((str(focused), "-n", "2"), {}),
        ((str(focused),), {"PYTEST_ADDOPTS": "-n auto"}),
    ):
        try:
            enforce_pytest_host_policy(
                repo_root=tmp_path,
                argv=argv,
                environ=environ,
            )
        except HostTestPolicyError as exc:
            assert "parallel pytest workers are forbidden" in str(exc)
        else:
            raise AssertionError("parallel staging pytest invocation was not refused")


def test_staging_refuses_more_than_ten_explicit_test_files(tmp_path: Path) -> None:
    _write_identity(tmp_path, app_env="staging", server_name="dotmac-sub-staging")
    selected: list[str] = []
    for index in range(11):
        test_file = tmp_path / f"test_focused_{index}.py"
        test_file.write_text("def test_focused(): pass\n", encoding="utf-8")
        selected.append(str(test_file))

    try:
        enforce_pytest_host_policy(
            repo_root=tmp_path,
            argv=tuple(selected),
            environ={},
        )
    except HostTestPolicyError as exc:
        assert "at most 10 explicitly named" in str(exc)
    else:
        raise AssertionError("oversized staging test selection was not refused")


def test_unknown_or_conflicting_deployment_identity_fails_closed(
    tmp_path: Path,
) -> None:
    _write_identity(tmp_path, app_env="staging", server_name="dotmac-sub-staging")

    identity = classify_test_host(
        repo_root=tmp_path,
        environ={"APP_ENV": "production"},
    )

    assert identity.kind is DeploymentHostKind.UNKNOWN_DEPLOYMENT
    try:
        require_full_suite_host(repo_root=tmp_path, environ={"APP_ENV": "production"})
    except HostTestPolicyError as exc:
        assert "do not identify an approved host pair" in str(exc)
    else:
        raise AssertionError("conflicting deployment identity did not fail closed")


def test_make_and_pytest_adapters_enforce_policy_before_heavy_work() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    conftest = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")

    for target in (
        "test",
        "test-v",
        "test-cov",
        "test-ci",
        "test-ci-shard",
        "test-fast",
        "test-integration",
        "test-architecture",
        "test-architecture-serial",
        "test-e2e",
    ):
        assert f"{target}: assert-full-test-host" in makefile
    assert "enforce_pytest_host_policy(" in conftest
    assert conftest.index("enforce_pytest_host_policy(") < conftest.index(
        "from app.web.brand_globals"
    )
