"""Focused tests for the ticket assignment-rule admin service and form helpers."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.ticket_workflow import TicketAssignmentStrategy
from app.services.ticket_assignment import admin as assignment_admin
from app.web.admin import support_assignment_rules as admin_assignment_rules

# ---------------------------------------------------------------------------
# Pure-Python form helpers — no DB required.
# ---------------------------------------------------------------------------


def _build_config(**overrides):
    base = {
        "entity_types": [],
        "priorities": [],
        "ticket_types": [],
        "project_types_csv": None,
        "regions": [],
        "sources": [],
        "service_team_ids": [],
        "tags_any_csv": None,
        "assignment_target": "technician",
        "assignee_person_id": None,
    }
    base.update(overrides)
    return admin_assignment_rules._build_match_config(**base)


def test_build_match_config_drops_empty_fields():
    assert _build_config() == {}


def test_build_match_config_keeps_structured_lists_and_splits_csv():
    config = _build_config(
        entity_types=["ticket"],
        priorities=["high", "urgent", " ", "high"],
        ticket_types=["incident"],
        project_types_csv="installation, survey",
        regions=["lagos"],
        sources=["web"],
        tags_any_csv="vip, outage , vip",
    )
    assert config == {
        "entity_types": ["ticket"],
        "priorities": ["high", "urgent"],
        "ticket_types": ["incident"],
        "project_types": ["installation", "survey"],
        "regions": ["lagos"],
        "sources": ["web"],
        "tags_any": ["vip", "outage"],
    }


def test_build_match_config_direct_assignee_records_target():
    person_id = str(uuid4())
    config = _build_config(
        assignee_person_id=person_id, assignment_target="technical_supervisor"
    )
    assert config == {
        "assignee_person_id": person_id,
        "assignment_target": "technical_supervisor",
    }


def test_build_match_config_target_ignored_without_assignee():
    assert _build_config(assignment_target="technical_supervisor") == {}


def test_build_match_config_rejects_invalid_assignee_uuid():
    with pytest.raises(ValueError, match="assignee_person_id"):
        _build_config(assignee_person_id="not-a-uuid")


def test_build_match_config_rejects_invalid_target():
    with pytest.raises(ValueError, match="assignment_target"):
        _build_config(assignee_person_id=str(uuid4()), assignment_target="ghost")


def test_build_match_config_rejects_invalid_service_team_uuid():
    with pytest.raises(ValueError, match="service_team_ids"):
        _build_config(service_team_ids=["not-a-uuid"])


# ---------------------------------------------------------------------------
# Service CRUD (needs db_session fixture).
# ---------------------------------------------------------------------------


def _team(db_session, name: str = "Dispatch") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def test_create_update_and_list_rules_in_engine_order(db_session):
    team = _team(db_session)
    team_id = team.id
    db_session.commit()
    low = assignment_admin.create_rule(
        db_session,
        name="Catch-all",
        priority=10,
        strategy=TicketAssignmentStrategy.round_robin.value,
        match_config=assignment_admin.TicketAssignmentRuleMatch(),
        team_id=team_id,
    )
    high = assignment_admin.create_rule(
        db_session,
        name="Urgent Lagos",
        priority=100,
        strategy=TicketAssignmentStrategy.least_loaded.value,
        match_config=assignment_admin.TicketAssignmentRuleMatch(
            priorities=("urgent",), regions=("lagos",)
        ),
        team_id=str(team_id),
        assign_manager=True,
    )
    db_session.commit()

    rules = assignment_admin.list_rules(db_session)
    assert [rule.id for rule in rules] == [high.id, low.id]
    assert high.assign_manager is True
    assert high.match_config == {"priorities": ["urgent"], "regions": ["lagos"]}
    db_session.commit()

    updated = assignment_admin.update_rule(
        db_session,
        str(low.id),
        name="Catch-all v2",
        priority=5,
        strategy=TicketAssignmentStrategy.least_loaded.value,
        match_config=assignment_admin.TicketAssignmentRuleMatch(
            ticket_types=("incident",)
        ),
        team_id=None,
        assign_manager=False,
        assign_spc=True,
        is_active=False,
    )
    db_session.commit()
    assert updated.name == "Catch-all v2"
    assert updated.team_id is None
    assert updated.assign_spc is True
    assert updated.is_active is False
    assert updated.match_config == {"ticket_types": ["incident"]}


def test_create_rule_validates_name_and_strategy(db_session):
    with pytest.raises(ValueError, match="name"):
        assignment_admin.create_rule(db_session, name="  ")
    with pytest.raises(ValueError, match="strategy"):
        assignment_admin.create_rule(db_session, name="Bad", strategy="random")
    with pytest.raises(ValueError, match="team_id"):
        assignment_admin.create_rule(db_session, name="Bad team", team_id="nope")


def test_set_rule_active_is_idempotent(db_session):
    rule = assignment_admin.create_rule(db_session, name="Idempotent rule")
    db_session.commit()

    initial_updated = rule.updated_at
    assignment_admin.set_rule_active(db_session, str(rule.id), is_active=True)
    db_session.refresh(rule)
    assert rule.updated_at == initial_updated

    assignment_admin.set_rule_active(db_session, str(rule.id), is_active=False)
    db_session.commit()
    db_session.refresh(rule)
    assert rule.is_active is False

    second_flipped_updated = rule.updated_at
    assignment_admin.set_rule_active(db_session, str(rule.id), is_active=False)
    db_session.refresh(rule)
    assert rule.updated_at == second_flipped_updated


def test_delete_rule_removes_row(db_session):
    rule = assignment_admin.create_rule(db_session, name="Doomed")
    db_session.commit()
    assignment_admin.delete_rule(db_session, str(rule.id))
    db_session.commit()
    assert assignment_admin.list_rules(db_session) == []


def test_get_rule_raises_domain_error_for_missing(db_session):
    with pytest.raises(assignment_admin.TicketAssignmentRuleError) as exc:
        assignment_admin.get_rule(db_session, str(uuid4()))
    assert exc.value.code == "assignment_rule_not_found"


def test_list_team_options_only_returns_active_teams(db_session):
    active = _team(db_session, "Field Ops")
    inactive = _team(db_session, "Retired")
    inactive.is_active = False
    db_session.commit()

    options = assignment_admin.list_team_options(db_session)
    assert {"id": str(active.id), "label": "Field Ops"} in options
    assert all(option["id"] != str(inactive.id) for option in options)


def test_validate_team_id_requires_configured_team(db_session):
    team = _team(db_session, "Configured")
    db_session.commit()

    assert admin_assignment_rules._validate_team_id(db_session, None) is None
    assert admin_assignment_rules._validate_team_id(db_session, "") is None
    assert admin_assignment_rules._validate_team_id(db_session, str(team.id)) == str(
        team.id
    )
    with pytest.raises(ValueError, match="valid UUID"):
        admin_assignment_rules._validate_team_id(db_session, "not-a-uuid")
    with pytest.raises(ValueError, match="configured service team"):
        admin_assignment_rules._validate_team_id(db_session, str(uuid4()))
