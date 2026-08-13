"""Prove Sub installs its operator-tenant GUC on every root transaction.

Kernel revision 0001 enables and forces row-level security in the same atomic
revision that creates its tenant function and policies.  Sub must have a
working session contract in the predecessor release: otherwise the migration
can turn populated roles, credentials and audit tables into silent empty reads
before the new application image starts.

This canary deliberately tests a plain SQLAlchemy ``Session`` rather than a web
dependency.  Sub has task, worker, CLI and one-off entry points that construct
sessions outside the request cycle; the global root-transaction hook is the
only boundary shared by all of them.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.services.operator_tenant import OPERATOR_TENANT_ID


def _session_scope(session: Session) -> str | None:
    value = session.scalar(text("SELECT current_setting('app.current_tenant', true)"))
    return None if value is None else str(value)


def _connection_scope(connection: Connection) -> str | None:
    value = connection.scalar(
        text("SELECT current_setting('app.current_tenant', true)")
    )
    return None if value is None else str(value)


def test_scope_is_reapplied_after_commit_and_rollback_without_pool_leak(
    engine: Engine,
) -> None:
    expected = str(OPERATOR_TENANT_ID)

    with engine.connect() as connection:
        with Session(bind=connection) as session:
            assert _session_scope(session) == expected

            session.commit()
            assert _session_scope(session) == expected

            session.rollback()
            assert _session_scope(session) == expected

        # ``set_config(..., true)`` is transaction-local.  Closing the last
        # Session transaction must return the same physical connection without
        # a tenant setting for its next borrower.
        with connection.begin():
            assert _connection_scope(connection) in {None, ""}
