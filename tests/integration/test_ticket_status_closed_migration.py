"""PostgreSQL evidence for the legacy resolved-to-closed data repair."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select

from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
    Ticket,
    TicketAssignee,
    TicketAutomationRule,
    TicketComment,
)
from app.services import support_ticket_settings

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic/versions/517_close_legacy_resolved_tickets.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "close_legacy_resolved_tickets", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_is_exact_preserving_and_idempotent(
    db_session, subscriber, monkeypatch
) -> None:
    migration = _load_migration()
    assert migration.down_revision == "516_material_request_erp_submission"

    opened_at = datetime.now(UTC) - timedelta(days=3)
    updated_at = datetime.now(UTC) - timedelta(days=1)
    resolved_at = datetime.now(UTC) - timedelta(days=2)
    assignee_id = uuid4()
    ticket = Ticket(
        subscriber_id=subscriber.id,
        assigned_to_person_id=assignee_id,
        title="Preserve every non-status field",
        description="Historical description",
        status="resolved",
        priority="high",
        tags=["resolved", "customer-impact"],
        metadata_={"legacy": True},
        attachments=[{"file_name": "evidence.txt", "storage_key": "kept"}],
        resolved_at=resolved_at,
        closed_at=None,
        created_at=opened_at,
        updated_at=updated_at,
    )
    db_session.add(ticket)
    db_session.flush()
    db_session.add(TicketAssignee(ticket_id=ticket.id, person_id=assignee_id))
    comment = TicketComment(
        ticket_id=ticket.id,
        body="Keep the official timeline",
        attachments=[{"file_name": "comment.txt", "storage_key": "kept-comment"}],
    )
    db_session.add(comment)
    rule = TicketAutomationRule(
        name="Legacy status rule",
        trigger=AutomationTrigger.status_changed,
        conditions={"status": "resolved", "priority": "high"},
        action_type=AutomationActionType.set_status,
        action_value={"status": "resolved", "unrelated": "kept"},
    )
    db_session.add(rule)
    db_session.flush()

    configured = db_session.scalar(
        select(DomainSetting).where(
            DomainSetting.key == support_ticket_settings.STATUS_OPTIONS_KEY
        )
    )
    if configured is None:
        configured = DomainSetting(
            domain=SettingDomain.workflow,
            key=support_ticket_settings.STATUS_OPTIONS_KEY,
            value_type=SettingValueType.json,
            value_json=["open", "resolved", "closed"],
        )
        db_session.add(configured)
    else:
        configured.value_json = ["open", "resolved", "closed"]
    db_session.flush()

    context = MigrationContext.configure(db_session.connection())
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()
    migration.upgrade()
    db_session.expire_all()

    repaired = db_session.get(Ticket, ticket.id)
    assert repaired is not None
    assert repaired.status == "closed"
    assert repaired.subscriber_id == subscriber.id
    assert repaired.assigned_to_person_id == assignee_id
    assert repaired.title == "Preserve every non-status field"
    assert repaired.description == "Historical description"
    assert repaired.priority == "high"
    assert repaired.tags == ["resolved", "customer-impact"]
    assert repaired.metadata_ == {"legacy": True}
    assert repaired.attachments == [
        {"file_name": "evidence.txt", "storage_key": "kept"}
    ]
    assert repaired.resolved_at == resolved_at
    assert repaired.closed_at is None
    assert repaired.created_at == opened_at
    assert repaired.updated_at == updated_at
    assert (
        db_session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.status == "resolved")
        )
        == 0
    )

    kept_comment = db_session.get(TicketComment, comment.id)
    assert kept_comment is not None
    assert kept_comment.body == "Keep the official timeline"
    assert kept_comment.attachments == [
        {"file_name": "comment.txt", "storage_key": "kept-comment"}
    ]
    assert db_session.get(TicketAssignee, (ticket.id, assignee_id)) is not None

    repaired_rule = db_session.get(TicketAutomationRule, rule.id)
    assert repaired_rule is not None
    assert repaired_rule.conditions == {"status": "closed", "priority": "high"}
    assert repaired_rule.action_value == {"status": "closed", "unrelated": "kept"}
    repaired_configuration = db_session.scalar(
        select(DomainSetting).where(
            DomainSetting.key == support_ticket_settings.STATUS_OPTIONS_KEY
        )
    )
    assert repaired_configuration is not None
    assert repaired_configuration.value_json == ["open", "closed"]
