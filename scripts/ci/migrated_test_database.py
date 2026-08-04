"""Typed contract for authoritative migrated test databases.

The deployment schema is owned by Alembic, not SQLAlchemy ``MetaData``.
Integration tests therefore accept only an explicitly named disposable
PostgreSQL/PostGIS target that has been upgraded to the exact repository head.

The module is also executable.  ``make test-integration`` invokes it once per
test environment before pytest starts; individual tests then use transactions
and savepoints rather than replaying the migration chain.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from alembic import command

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG_PATH = REPOSITORY_ROOT / "alembic.ini"
_DISPOSABLE_DATABASE_TOKEN = re.compile(
    r"(?:^|_)(?:test|pytest|ci|e2e|migration)(?:_|$)", re.IGNORECASE
)


class DatabaseRefusal(StrEnum):
    """Stable refusal codes for the test-database adapter."""

    missing_url = "test_database_url_required"
    invalid_url = "test_database_url_invalid"
    non_postgresql = "test_database_postgresql_required"
    unsafe_database_name = "test_database_disposable_name_required"
    schema_unversioned = "test_database_alembic_version_missing"
    schema_not_at_head = "test_database_not_at_alembic_head"


class DatabaseContractError(RuntimeError):
    """The requested database cannot provide deployed-schema evidence."""

    def __init__(self, message: str, *, code: DatabaseRefusal) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DatabaseTarget:
    """Validated disposable PostgreSQL target.

    The URL is deliberately excluded from ``repr`` so credentials cannot leak
    through an assertion, log line, or CLI error.  ``display_url`` is the only
    representation intended for operator output.
    """

    url: URL = field(repr=False)
    database_name: str
    display_url: str


@dataclass(frozen=True)
class MigratedSchemaState:
    """Exact repository and database revision heads."""

    expected_heads: frozenset[str]
    actual_heads: frozenset[str]

    @property
    def current(self) -> bool:
        return self.actual_heads == self.expected_heads


def parse_test_database_target(raw_url: str | None) -> DatabaseTarget:
    """Validate the explicit disposable PostgreSQL integration target."""

    if not raw_url or not raw_url.strip():
        raise DatabaseContractError(
            "TEST_DATABASE_URL is required for authoritative integration tests.",
            code=DatabaseRefusal.missing_url,
        )
    try:
        url = make_url(raw_url.strip())
    except ArgumentError as exc:
        raise DatabaseContractError(
            "TEST_DATABASE_URL is not a valid SQLAlchemy database URL.",
            code=DatabaseRefusal.invalid_url,
        ) from exc
    if not url.drivername.startswith("postgresql"):
        raise DatabaseContractError(
            "TEST_DATABASE_URL must use PostgreSQL/PostGIS; metadata and SQLite "
            "databases are not deployed-schema evidence.",
            code=DatabaseRefusal.non_postgresql,
        )
    database_name = url.database or ""
    if not _DISPOSABLE_DATABASE_TOKEN.search(database_name):
        raise DatabaseContractError(
            "TEST_DATABASE_URL must name an explicitly disposable database "
            "containing test, pytest, ci, e2e, or migration.",
            code=DatabaseRefusal.unsafe_database_name,
        )
    return DatabaseTarget(
        url=url,
        database_name=database_name,
        display_url=url.render_as_string(hide_password=True),
    )


def repository_heads() -> frozenset[str]:
    """Return the exact checked-in Alembic heads."""

    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


def migrated_schema_state(engine: Engine) -> MigratedSchemaState:
    """Read the target's migration identity without mutating its schema."""

    inspector = inspect(engine)
    if not inspector.has_table("alembic_version"):
        raise DatabaseContractError(
            "The test database has no alembic_version table. Run the real "
            "migration chain; Base.metadata.create_all() is not accepted.",
            code=DatabaseRefusal.schema_unversioned,
        )
    with engine.connect() as connection:
        actual_heads = frozenset(
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars()
        )
    return MigratedSchemaState(
        expected_heads=repository_heads(),
        actual_heads=actual_heads,
    )


def require_migrated_schema(engine: Engine) -> MigratedSchemaState:
    """Fail closed unless the database is at the exact repository head."""

    state = migrated_schema_state(engine)
    if not state.current:
        raise DatabaseContractError(
            "The test database is not at the exact Alembic head: "
            f"actual={sorted(state.actual_heads)!r}, "
            f"expected={sorted(state.expected_heads)!r}.",
            code=DatabaseRefusal.schema_not_at_head,
        )
    return state


def migrate_test_database(target: DatabaseTarget) -> MigratedSchemaState:
    """Apply the real chain once, then verify its exact resulting head."""

    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = target.url.render_as_string(hide_password=False)
    try:
        config = Config(str(ALEMBIC_CONFIG_PATH))
        config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
        command.upgrade(config, "head")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url

    engine = create_engine(target.url)
    try:
        return require_migrated_schema(engine)
    finally:
        engine.dispose()


def main() -> int:
    """Prepare the target selected only through TEST_DATABASE_URL."""

    try:
        target = parse_test_database_target(os.getenv("TEST_DATABASE_URL"))
        state = migrate_test_database(target)
    except DatabaseContractError as exc:
        print(f"REFUSED [{exc.code.value}] {exc}", file=sys.stderr)
        return 2
    print(
        "migrated test database ready: "
        f"{target.display_url} heads={sorted(state.actual_heads)!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
