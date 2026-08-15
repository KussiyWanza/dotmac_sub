"""Executable PostgreSQL proofs for the operator transaction seams.

These tests drive the real adapters on the real migrated schema. SQLite and
mocked statement assertions cannot prove transaction ordering, PostgreSQL
read-only enforcement, or the operator-tenant GUC lifetime.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import db as app_db
from app.services import party_identity_backfill
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.party_identity_adjudication import PartyIdentityDecision
from app.services.party_identity_audit import SubscriberIdentityAudit
from app.services.party_identity_backfill import (
    PartyBackfillExecutionApproval,
    PartyBackfillExecutionOutcome,
)
from scripts.migration import kernel_lineage_rehearsal_evidence

pytestmark = pytest.mark.integration


@pytest.fixture()
def operator_session_factory(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> sessionmaker[Session]:
    if engine.dialect.name != "postgresql":
        pytest.fail("operator transaction proofs require migrated PostgreSQL")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    return factory


def test_kernel_lineage_evidence_entry_point_runs_on_postgresql(
    operator_session_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The minimized evidence adapter must execute through the shared seam."""

    del operator_session_factory
    result = kernel_lineage_rehearsal_evidence.main([])

    payload = json.loads(capsys.readouterr().out)
    evidence = kernel_lineage_rehearsal_evidence.KernelLineageRehearsalEvidence(
        **payload
    )
    assert result == 0
    assert evidence.schema_version == 1
    assert {contract.table_name for contract in evidence.tables} == set(
        kernel_lineage_rehearsal_evidence.LINEAGE_TABLES
    )


def test_serializable_backfill_owner_can_write_and_keeps_operator_scope(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the real transaction owner through SERIALIZABLE, READ WRITE."""

    role_id = uuid4()
    role_name = f"serializable-write-canary-{role_id.hex[:12]}"
    observed: dict[str, str] = {}

    def execute_canary_plan(
        db: Session,
        *,
        audit: SubscriberIdentityAudit,
        decisions: tuple[PartyIdentityDecision, ...],
        approval: PartyBackfillExecutionApproval,
        decision_file_sha256: str,
        plan_file_sha256: str,
        approval_file_sha256: str,
        executed_at: datetime | None = None,
    ) -> PartyBackfillExecutionOutcome:
        del (
            audit,
            decisions,
            approval,
            decision_file_sha256,
            plan_file_sha256,
            approval_file_sha256,
            executed_at,
        )
        isolation, read_only, tenant_id = db.execute(
            text(
                "SELECT current_setting('transaction_isolation'), "
                "current_setting('transaction_read_only'), "
                "current_setting('app.current_tenant', true)"
            )
        ).one()
        observed.update(
            isolation=str(isolation),
            read_only=str(read_only),
            tenant_id=str(tenant_id),
        )
        db.execute(
            text(
                "INSERT INTO roles (id, name, is_active) "
                "VALUES (:role_id, :role_name, true)"
            ),
            {"role_id": role_id, "role_name": role_name},
        )
        return PartyBackfillExecutionOutcome(
            receipt_id=uuid4(),
            plan_digest="0" * 64,
            parties_created=0,
            bindings_created=0,
            replayed=False,
        )

    monkeypatch.setattr(
        party_identity_backfill,
        "execute_party_backfill_plan",
        execute_canary_plan,
    )
    now = datetime.now(UTC)
    approval = PartyBackfillExecutionApproval(
        plan_digest="0" * 64,
        audit_digest="1" * 64,
        decision_file_sha256="2" * 64,
        plan_file_sha256="3" * 64,
        approved_by="postgresql-canary",
        approved_at=now,
        expires_at=now + timedelta(minutes=5),
        reason="Prove the SERIALIZABLE writer transaction contract",
        maximum_parties=0,
        maximum_bindings=0,
    )

    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        outcome = party_identity_backfill.execute_party_backfill_transaction(
            session,
            decisions=(),
            approval=approval,
            decision_file_sha256="2" * 64,
            plan_file_sha256="3" * 64,
            approval_file_sha256="4" * 64,
        )
    finally:
        session.close()

    try:
        with engine.connect() as connection:
            persisted = connection.scalar(
                text("SELECT count(*) FROM roles WHERE id = :role_id"),
                {"role_id": role_id},
            )
        assert outcome.plan_digest == "0" * 64
        assert observed == {
            "isolation": "serializable",
            "read_only": "off",
            "tenant_id": str(OPERATOR_TENANT_ID),
        }
        assert persisted == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM roles WHERE id = :role_id"),
                {"role_id": role_id},
            )
