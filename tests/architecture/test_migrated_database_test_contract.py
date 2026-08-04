"""Prevent metadata-built databases being reported as integration evidence."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _calls_create_all(nodes: list[ast.stmt]) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_all"
        for statement in nodes
        for node in ast.walk(statement)
    )


def test_postgresql_fixture_never_builds_schema_from_model_metadata() -> None:
    tree = ast.parse((ROOT / "tests/conftest.py").read_text(encoding="utf-8"))
    engine_function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "engine"
    )
    database_branch = next(
        node
        for node in engine_function.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "database_url"
    )

    assert not _calls_create_all(database_branch.body), (
        "PostgreSQL integration fixtures must consume the Alembic-built schema"
    )
    assert _calls_create_all(database_branch.orelse), (
        "the explicitly non-authoritative SQLite unit lane still needs metadata"
    )


def test_makefile_owns_migration_and_integration_execution_order() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("test-integration:", 1)[1].split("\n\n", 1)[0]

    prepare = "python -m scripts.ci.migrated_test_database"
    pytest = "pytest tests/integration/"
    assert prepare in target
    assert pytest in target
    assert target.index(prepare) < target.index(pytest)


def test_ci_delegates_migration_and_tests_to_makefile_owner() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "run: make test-integration" in workflow
    assert "run: poetry run alembic upgrade head" not in workflow


def test_integration_package_cannot_silently_skip_to_sqlite() -> None:
    source = (ROOT / "tests/integration/conftest.py").read_text(encoding="utf-8")

    assert "pytest.skip" not in source
    assert "pytest.UsageError" in source
