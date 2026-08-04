"""Fast contract tests for the authoritative database-test adapter."""

from __future__ import annotations

from dataclasses import fields

import pytest
from sqlalchemy import create_engine, text

from scripts.ci.migrated_test_database import (
    DatabaseContractError,
    DatabaseRefusal,
    migrated_schema_state,
    parse_test_database_target,
    repository_heads,
    require_migrated_schema,
)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_target_requires_an_explicit_url(raw: str | None) -> None:
    with pytest.raises(DatabaseContractError) as excinfo:
        parse_test_database_target(raw)

    assert excinfo.value.code is DatabaseRefusal.missing_url


def test_target_requires_postgresql() -> None:
    with pytest.raises(DatabaseContractError) as excinfo:
        parse_test_database_target("sqlite:///dotmac_sub_test")

    assert excinfo.value.code is DatabaseRefusal.non_postgresql


def test_target_rejects_a_malformed_url_without_echoing_it() -> None:
    secret_bearing_input = "not a url containing do-not-log-this"

    with pytest.raises(DatabaseContractError) as excinfo:
        parse_test_database_target(secret_bearing_input)

    assert excinfo.value.code is DatabaseRefusal.invalid_url
    assert "do-not-log-this" not in str(excinfo.value)


@pytest.mark.parametrize(
    "database_name",
    ["dotmac_sub", "production", "customer_data", "dotmac_sub_staging"],
)
def test_target_refuses_names_that_do_not_prove_disposability(
    database_name: str,
) -> None:
    with pytest.raises(DatabaseContractError) as excinfo:
        parse_test_database_target(
            f"postgresql+psycopg://user:secret@db.example/{database_name}"
        )

    assert excinfo.value.code is DatabaseRefusal.unsafe_database_name


@pytest.mark.parametrize(
    "database_name",
    ["dotmac_sub_test", "dotmac_pytest_1", "ci_sub", "dotmac_sub_e2e"],
)
def test_target_accepts_explicit_disposable_postgresql_names(
    database_name: str,
) -> None:
    target = parse_test_database_target(
        f"postgresql+psycopg://user:secret@db.example/{database_name}"
    )

    assert target.database_name == database_name
    assert "secret" not in target.display_url
    assert "secret" not in repr(target)
    assert fields(target)[0].repr is False


def test_metadata_only_database_is_unversioned() -> None:
    engine = create_engine("sqlite+pysqlite://")
    try:
        with pytest.raises(DatabaseContractError) as excinfo:
            require_migrated_schema(engine)
    finally:
        engine.dispose()

    assert excinfo.value.code is DatabaseRefusal.schema_unversioned


def test_revision_state_requires_exact_repository_heads() -> None:
    engine = create_engine("sqlite+pysqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(255))")
            )
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('stale')")
            )

        state = migrated_schema_state(engine)
        assert state.actual_heads == frozenset({"stale"})
        assert state.expected_heads == repository_heads()
        assert state.current is False

        with pytest.raises(DatabaseContractError) as excinfo:
            require_migrated_schema(engine)
    finally:
        engine.dispose()

    assert excinfo.value.code is DatabaseRefusal.schema_not_at_head
