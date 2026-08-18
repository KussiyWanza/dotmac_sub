from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.operational_escalation import (
    OperationalEscalationDelivery,
    OperationalEscalationEvent,
    OperationalEscalationPolicy,
)
from app.models.system_user import SystemUser
from app.services import operational_escalation
from app.services import web_notifications_sla_policies as web_sla_policies
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _create_policy(db_session, **values):
    db_session_adapter.release_read_transaction(db_session)
    return web_sla_policies.create_policy(
        db_session,
        context=CommandContext.system(
            actor="test:operational-sla-policy",
            scope="operational-sla-policy",
            reason="test policy command",
        ),
        **values,
    )


def test_ui_can_configure_a_non_billing_operational_event(db_session) -> None:
    policy = _create_policy(
        db_session,
        name="Unowned work order escalation",
        entity_type="work_order",
        trigger="work_order.unowned",
        level=2,
        delay_minutes=23,
        channels=["push", "email"],
        min_severity="high",
        min_affected_customers=None,
        near_breach_minutes=45,
        notes="Escalate to the operational audience if ownership is still missing.",
        is_active=True,
    )

    assert policy.entity_type == "work_order"
    assert policy.trigger == "work_order.unowned"
    assert policy.unresolved_after_seconds == 23 * 60
    assert policy.channels == ["push", "email"]
    assert policy.cooldown_seconds == 0
    assert policy.metadata_["notes"].startswith("Escalate")
    assert policy.metadata_["near_breach_seconds"] == 45 * 60
    assert (
        web_sla_policies.form_data(db_session, policy_id=policy.id)[
            "near_breach_minutes"
        ]
        == "45"
    )


def test_ui_rejects_duplicate_active_level_for_the_same_event(db_session) -> None:
    values = {
        "name": "Ticket response L1",
        "entity_type": "ticket",
        "trigger": "ticket.response_due",
        "level": 1,
        "delay_minutes": 10,
        "channels": ["email"],
        "min_severity": None,
        "min_affected_customers": None,
        "notes": None,
        "is_active": True,
    }
    _create_policy(db_session, **values)

    with pytest.raises(ValueError, match="already owns"):
        _create_policy(
            db_session,
            **{**values, "name": "Duplicate ticket response L1"},
        )


def test_database_rejects_duplicate_active_level_for_the_same_event(
    db_session,
) -> None:
    _create_policy(
        db_session,
        name="Ticket response L1",
        entity_type="ticket",
        trigger="ticket.response_due",
        level=1,
        delay_minutes=10,
        channels=["email"],
        min_severity=None,
        min_affected_customers=None,
        notes=None,
        is_active=True,
    )
    db_session.add(
        OperationalEscalationPolicy(
            name="Concurrent duplicate",
            entity_type="ticket",
            trigger="ticket.response_due",
            level=1,
            channels=["push"],
            unresolved_after_seconds=60,
            is_active=True,
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_ui_accepts_custom_events_for_every_supported_operational_entity(
    db_session,
) -> None:
    for index, entity_type in enumerate(
        operational_escalation.OPERATIONAL_ENTITY_TYPES
    ):
        policy = _create_policy(
            db_session,
            name=f"{entity_type} policy {uuid4()}",
            entity_type=entity_type,
            trigger=f"{entity_type}.operator_attention",
            level=index + 1,
            delay_minutes=index,
            channels=["web"],
            min_severity=None,
            min_affected_customers=None,
            notes=None,
            is_active=True,
        )
        assert policy.entity_type == entity_type

    assert db_session.query(OperationalEscalationPolicy).count() == len(
        operational_escalation.OPERATIONAL_ENTITY_TYPES
    )


def test_ui_rejects_unknown_entity_and_non_dotted_event_keys(db_session) -> None:
    base = {
        "name": "Bad policy",
        "level": 1,
        "delay_minutes": 5,
        "channels": ["email"],
        "min_severity": None,
        "min_affected_customers": None,
        "notes": None,
        "is_active": True,
    }
    with pytest.raises(ValueError, match="Unsupported operational entity"):
        _create_policy(
            db_session,
            entity_type="unknown",
            trigger="unknown.created",
            **base,
        )
    with pytest.raises(ValueError, match="dotted lower-case"):
        _create_policy(
            db_session,
            entity_type="ticket",
            trigger="FIVE_MINUTE_SLA",
            **base,
        )


def test_policy_matching_is_event_scoped_and_not_billing_specific(db_session) -> None:
    ticket_policy = _create_policy(
        db_session,
        name="Ticket stale owner",
        entity_type="ticket",
        trigger="ticket.owner_stale",
        level=1,
        delay_minutes=30,
        channels=["push"],
        min_severity=None,
        min_affected_customers=None,
        notes=None,
        is_active=True,
    )
    _create_policy(
        db_session,
        name="Project stale owner",
        entity_type="project",
        trigger="project.owner_stale",
        level=1,
        delay_minutes=45,
        channels=["email"],
        min_severity=None,
        min_affected_customers=None,
        notes=None,
        is_active=True,
    )

    assert operational_escalation.matching_policies(
        db_session,
        entity_type="ticket",
        trigger="ticket.owner_stale",
    ) == [ticket_policy]


def test_non_billing_owner_can_emit_a_fact_through_the_generic_sla_owner(
    db_session,
) -> None:
    policy = _create_policy(
        db_session,
        name="Work order unowned",
        entity_type="work_order",
        trigger="work_order.unowned",
        level=1,
        delay_minutes=9,
        channels=["email"],
        min_severity=None,
        min_affected_customers=None,
        notes=None,
        is_active=True,
    )
    operator = SystemUser(
        first_name="Field",
        last_name="Lead",
        email="field-lead@example.com",
        is_active=True,
    )
    db_session.add(operator)
    db_session.flush()
    work_order_id = uuid4()
    operational_escalation.add_watcher(
        db_session,
        entity_type="work_order",
        entity_id=work_order_id,
        person_id=operator.id,
    )

    result = operational_escalation.emit_sla_event(
        db_session,
        entity_type="work_order",
        entity_id=work_order_id,
        trigger="work_order.unowned",
        metadata={"title": "Work order needs an owner"},
    )

    assert result.policy_count == 1
    assert result.events == (db_session.query(OperationalEscalationEvent).one(),)
    assert result.deliveries == (db_session.query(OperationalEscalationDelivery).one(),)
    assert result.events[0].policy_id == policy.id
    assert result.deliveries[0].recipient_id == str(operator.id)
    assert (
        result.deliveries[0].cooldown_until - result.deliveries[0].created_at
    ).total_seconds() == pytest.approx(9 * 60, abs=2)


def test_sla_policy_templates_compile() -> None:
    from app.web.admin.notifications import templates

    templates.get_template("admin/notifications/sla_policies.html")
    templates.get_template("admin/notifications/sla_policy_form.html")
