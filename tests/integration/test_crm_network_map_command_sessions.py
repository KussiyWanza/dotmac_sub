"""PostgreSQL proof that CRM migration write commands receive a writer session."""

from __future__ import annotations

import importlib
from argparse import Namespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from scripts.network import crm_network_map_point_migration as command

pytestmark = pytest.mark.integration


class _Outcome:
    def to_dict(self) -> dict[str, object]:
        return {"status": "persisted"}


def test_proposal_command_enters_postgresql_transaction_free_and_read_write(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if engine.dialect.name != "postgresql":
        pytest.fail("CRM migration command-session proof requires migrated PostgreSQL")
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session_adapter_module = importlib.import_module("app.services.db_session_adapter")
    monkeypatch.setattr(session_adapter_module, "SessionLocal", session_factory)
    role_id = uuid4()
    role_name = f"crm-map-command-session-{role_id.hex[:12]}"
    observed: dict[str, object] = {}

    def propose(db: Session, **kwargs: object) -> _Outcome:
        observed["transaction_free_at_entry"] = not db.in_transaction()
        read_only = db.execute(text("SHOW transaction_read_only")).scalar_one()
        observed["read_only"] = str(read_only)
        db.execute(
            text(
                "INSERT INTO roles (id, name, is_active) "
                "VALUES (:role_id, :role_name, true)"
            ),
            {"role_id": role_id, "role_name": role_name},
        )
        db.commit()
        return _Outcome()

    monkeypatch.setattr(command, "propose_crm_point_identity_proposals", propose)
    args = Namespace(
        command="propose-batch",
        expected_archive_sha256="a" * 64,
        actor="postgresql-canary",
        reason="Prove the CRM migration writer session",
    )

    try:
        result = command._run_write_command(args)
        with engine.connect() as connection:
            persisted = connection.scalar(
                text("SELECT count(*) FROM roles WHERE id = :role_id"),
                {"role_id": role_id},
            )
        assert result == {"status": "persisted"}
        assert observed == {
            "transaction_free_at_entry": True,
            "read_only": "off",
        }
        assert persisted == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM roles WHERE id = :role_id"),
                {"role_id": role_id},
            )
