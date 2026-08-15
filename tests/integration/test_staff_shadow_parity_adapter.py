"""Migrated-PostgreSQL proof that the staff shadow-parity adapter can actually run.

The adapter asks for a REPEATABLE READ, READ ONLY snapshot. It previously did
that with `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`, which is
only legal as the FIRST statement in a transaction — and it never is, because the
operator-tenant hook installs `app.current_tenant` via `set_config` on
`after_begin`. Every PostgreSQL run therefore raised

    psycopg.errors.ActiveSqlTransaction:
        SET TRANSACTION ISOLATION LEVEL must be called before any query

while SQLite unit coverage stayed green, because it skips that branch entirely.
The report was merged, deployed, and had never once executed against a real
database.

So this module exists specifically to run the thing, on the schema the migration
chain builds, and to pin all three properties together. Fixing the isolation
level while quietly dropping READ ONLY would leave a report capable of writing to
the database it measures, and no unit test could tell.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, InternalError
from sqlalchemy.orm import Session, sessionmaker

from app import db as app_db
from app.services.operator_tenant import OPERATOR_TENANT_ID
from scripts.migration import staff_authentication_shadow_parity

pytestmark = pytest.mark.integration


@pytest.fixture()
def operator_session_factory(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> sessionmaker[Session]:
    if engine.dialect.name != "postgresql":
        pytest.fail("the snapshot contract requires migrated PostgreSQL")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    return factory


@pytest.fixture()
def snapshot(
    operator_session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    del operator_session_factory
    with app_db.read_only_snapshot_session() as session:
        yield session


def test_the_report_entry_point_runs_against_the_migrated_schema(
    operator_session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run the real adapter and shared seam, not a reconstructed proxy."""

    del operator_session_factory
    result = staff_authentication_shadow_parity.main(["--report-only"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["credentials"] >= 0
    assert isinstance(payload["blocking_reasons"], list)


def test_the_snapshot_is_repeatable_read(snapshot: Session) -> None:
    level = snapshot.execute(text("SHOW transaction_isolation")).scalar_one()

    assert level == "repeatable read"


def test_the_snapshot_is_read_only(snapshot: Session) -> None:
    """READ ONLY must survive the isolation fix, not be traded away for it."""

    assert snapshot.execute(text("SHOW transaction_read_only")).scalar_one() == "on"


def test_a_write_is_rejected(snapshot: Session) -> None:
    """Proven by attempting one, not by trusting the setting.

    A report that measures a production database must be structurally incapable
    of changing it.
    """

    with pytest.raises((DBAPIError, InternalError)) as excinfo:
        snapshot.execute(
            text(
                "INSERT INTO roles (id, name, is_active) "
                "VALUES (gen_random_uuid(), 'shadow-parity-canary', true)"
            )
        )

    assert "read-only" in str(excinfo.value).lower()


def test_the_operator_tenant_guc_is_still_installed(snapshot: Session) -> None:
    """The read-only snapshot must not cost the tenant scope.

    `app.current_tenant` is installed by an `after_begin` hook. If requesting
    the snapshot options changed when or whether the transaction begins, the GUC
    would silently vanish and every tenant-scoped read would narrow to nothing
    once FORCE RLS is enabled.
    """

    installed = snapshot.execute(
        text("SELECT current_setting('app.current_tenant', true)")
    ).scalar_one()

    assert installed == str(OPERATOR_TENANT_ID)
