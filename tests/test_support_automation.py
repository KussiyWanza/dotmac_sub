"""Focused tests for app.services.support_automation core logic."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
    Ticket,
    TicketChannel,
    TicketPriority,
    TicketStatus,
)
from app.services import support_automation, support_automation_rules
from app.services import support_ticket_settings as support_ticket_settings_service
from app.web.admin import support_automation as admin_support_automation

# ---------------------------------------------------------------------------
# Pure-Python helpers — no DB required.
# ---------------------------------------------------------------------------


def test_clean_conditions_drops_unknown_keys_and_empty_values():
    raw = {
        "status": "open",
        "priority": "",
        "channel": None,
        "ticket_type": "incident",
        "made_up_key": "x",
    }
    cleaned = support_automation_rules.TicketAutomationConditions.from_mapping(
        raw
    ).as_dict()
    assert cleaned == {"status": "open", "ticket_type": "incident"}


def test_automation_configuration_canonicalizes_legacy_resolved_status():
    conditions = support_automation_rules.TicketAutomationConditions.from_mapping(
        {"status": "resolved"}
    )
    action = support_automation_rules.TicketAutomationAction.from_mapping(
        AutomationActionType.set_status,
        {"status": "resolved"},
    )

    assert conditions.status is TicketStatus.closed
    assert conditions.as_dict() == {"status": "closed"}
    assert action.status is TicketStatus.closed
    assert action.as_dict() == {"status": "closed"}


def test_conditions_match_empty_dict_matches_anything():
    ticket = SimpleNamespace(status="open", priority="high")
    assert support_automation._conditions_match({}, ticket) is True


def test_conditions_match_exact_strings():
    ticket = SimpleNamespace(status="open", priority="urgent", region="lagos")
    assert (
        support_automation._conditions_match(
            {"status": "open", "region": "lagos"}, ticket
        )
        is True
    )
    assert (
        support_automation._conditions_match(
            {"status": "open", "region": "abuja"}, ticket
        )
        is False
    )


def test_conditions_match_unwraps_enum_value():
    """Enum-typed ticket fields should compare by `.value`, not by enum object."""
    ticket = SimpleNamespace(status=TicketStatus.open, priority=TicketPriority.high)
    assert (
        support_automation._conditions_match(
            {"status": "open", "priority": "high"}, ticket
        )
        is True
    )


# ---------------------------------------------------------------------------
# Action application — requires a Ticket-shaped object but no DB.
# ---------------------------------------------------------------------------


def _fake_rule(action_type, action_value):
    return SimpleNamespace(
        id=uuid4(), name="Test rule", action_type=action_type, action_value=action_value
    )


def test_proposal_sets_priority_without_a_ticket_write():
    rule = _fake_rule(AutomationActionType.set_priority, {"priority": "high"})
    proposal = support_automation._proposal_for_rule(rule)
    assert proposal is not None and proposal.priority == "high"


def test_status_proposal_strips_whitespace():
    rule = _fake_rule(AutomationActionType.set_status, {"status": "  resolved  "})
    proposal = support_automation._proposal_for_rule(rule)
    assert proposal is not None and proposal.status is TicketStatus.closed


def test_tag_proposal_carries_only_the_requested_tag():
    rule = _fake_rule(AutomationActionType.add_tag, {"tag": "vip"})
    proposal = support_automation._proposal_for_rule(rule)
    assert proposal is not None and proposal.tag == "vip"


def test_due_proposal_carries_relative_hours_not_a_wall_clock_decision():
    rule = _fake_rule(AutomationActionType.set_due_in_hours, {"hours": 4})
    proposal = support_automation._proposal_for_rule(rule)
    assert proposal is not None and proposal.due_in_hours == 4


def test_team_proposal_with_invalid_uuid_raises():
    rule = _fake_rule(
        AutomationActionType.assign_team, {"service_team_id": "not-a-uuid"}
    )
    with pytest.raises(ValueError):
        support_automation._proposal_for_rule(rule)


def test_missing_action_value_produces_no_proposal():
    rule = _fake_rule(AutomationActionType.set_priority, {})
    assert support_automation._proposal_for_rule(rule) is None


def test_admin_action_value_validation_rejects_mismatched_payload(db_session):
    support_ticket_settings_service.update_ticket_configuration(
        db_session,
        support_ticket_settings_service.TicketConfigurationUpdate(
            statuses=("open",),
            priorities=("normal",),
            ticket_types=("incident",),
        ),
    )

    with pytest.raises(ValueError, match="priority"):
        admin_support_automation._validate_action_value(
            db_session,
            AutomationActionType.set_priority,
            {"status": "open"},
        )


# ---------------------------------------------------------------------------
# Service CRUD + apply_rules (needs db_session fixture).
# ---------------------------------------------------------------------------


def _make_subscriber(db_session):
    from app.models.subscriber import Subscriber

    s = Subscriber(
        first_name="Auto",
        last_name="Tester",
        email=f"auto-{uuid4().hex[:8]}@example.invalid",
    )
    db_session.add(s)
    db_session.flush()
    return s


def test_set_rule_active_is_idempotent(db_session):
    subscriber = _make_subscriber(db_session)
    del subscriber  # only used to ensure the DB has at least one subscriber row
    db_session.commit()
    rule = support_automation_rules.create_rule(
        db_session,
        name="Idempotent rule",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.add_tag,
        action_value=support_automation_rules.TicketAutomationAction(tag="noop"),
        is_active=True,
    )
    db_session.commit()

    initial_updated = rule.updated_at
    # Same state -> no flush, no updated_at change
    support_automation_rules.set_rule_active(db_session, str(rule.id), is_active=True)
    db_session.refresh(rule)
    assert rule.updated_at == initial_updated

    # Flip
    support_automation_rules.set_rule_active(db_session, str(rule.id), is_active=False)
    db_session.commit()
    db_session.refresh(rule)
    assert rule.is_active is False

    # Setting to the same flipped state again must remain idempotent
    second_flipped_updated = rule.updated_at
    support_automation_rules.set_rule_active(db_session, str(rule.id), is_active=False)
    db_session.refresh(rule)
    assert rule.updated_at == second_flipped_updated


def test_get_rule_raises_domain_error_for_missing(db_session):
    with pytest.raises(support_automation_rules.TicketAutomationRuleError) as exc:
        support_automation_rules.get_rule(db_session, str(uuid4()))
    assert exc.value.code == "automation_rule_not_found"


def test_automation_evaluation_returns_proposal_without_mutating_ticket(db_session):
    rule = support_automation_rules.create_rule(
        db_session,
        name="Set high on created",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.set_priority,
        action_value=support_automation_rules.TicketAutomationAction(priority="high"),
        is_active=True,
    )
    db_session.commit()

    ticket = Ticket(
        title="Automation proposal test",
        description="",
        channel=TicketChannel.web,
        priority="normal",
    )
    db_session.add(ticket)
    db_session.commit()

    proposals = support_automation.evaluate_rules(
        db_session, ticket, AutomationTrigger.ticket_created
    )

    assert len(proposals) == 1
    assert proposals[0].priority == "high"
    assert ticket.priority == "normal"
    assert rule.last_fired_at is None
