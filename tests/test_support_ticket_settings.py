from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.ticket_workflow import TicketAssignmentRule
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services import web_support_tickets as web_support_tickets_service


def _native_team(db_session, *, name: str, team_id=None) -> ServiceTeam:
    team = ServiceTeam(
        id=team_id or uuid4(),
        name=name,
        team_type=ServiceTeamType.support.value,
        is_active=True,
    )
    db_session.add(team)
    db_session.commit()
    return team


def test_ticket_settings_defaults_loaded_without_db_rows(db_session):
    assert support_ticket_settings_service.list_status_options(db_session)
    assert support_ticket_settings_service.list_priority_options(db_session)
    assert support_ticket_settings_service.list_ticket_type_options(db_session)
    assert (
        support_ticket_settings_service.auto_assign_max_open_tickets(db_session) is None
    )


def test_ticket_settings_drive_support_ticket_form_context(db_session):
    team = _native_team(db_session, name="Field Ops")
    team_id = str(team.id)
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open", "pending"],
        priorities=["normal", "critical"],
        ticket_types=["incident", "network audit"],
        regions=["lagos", "abuja"],
    )

    context = web_support_tickets_service.build_ticket_form_context(db_session)

    assert context["all_statuses"] == ["open", "pending"]
    assert context["all_priorities"] == ["normal", "critical"]
    assert context["ticket_type_options"] == ["incident", "network audit"]
    assert context["region_options"] == ["abuja", "lagos"]
    assert context["service_team_options"] == [{"id": team_id, "label": "Field Ops"}]
    assert context["prefill"]["status"] == "open"
    assert context["prefill"]["priority"] == "normal"


def test_ticket_settings_reject_statuses_outside_lifecycle_vocabulary(db_session):
    with pytest.raises(
        support_ticket_settings_service.SupportTicketConfigurationError,
        match="Unsupported ticket status: needs_vendor",
    ):
        support_ticket_settings_service.update_options(
            db_session,
            statuses=["open", "needs_vendor"],
            priorities=["normal"],
            ticket_types=["incident"],
        )


def test_ticket_settings_persist_routing_and_sla(db_session):
    team = _native_team(db_session, name="Core Network")
    team_id = str(team.id)
    tech_id = str(uuid4())
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open", "pending"],
        priorities=["normal"],
        ticket_types=["incident", "core link disconnection"],
        regions=["north"],
        auto_assign=True,
        auto_assign_max_open_tickets="3",
        routing_regions=["north"],
        routing_technician_person_ids=[tech_id],
        routing_service_team_ids=[team_id],
        sla_priorities=["normal"],
        sla_response_hours=["2"],
        sla_resolution_hours=["12"],
        sla_aging_hours=["6"],
        sla_ticket_types=["incident", "core link disconnection"],
        sla_ticket_type_resolution_hours=["0", "48"],
    )

    assert support_ticket_settings_service.auto_assign_enabled(db_session) is True
    assert support_ticket_settings_service.auto_assign_max_open_tickets(db_session) == 3
    assert support_ticket_settings_service.region_assignment_rules(db_session) == {
        "north": {
            "ticket_manager_person_id": None,
            "site_coordinator_person_id": None,
            "technician_person_id": tech_id,
            "service_team_id": team_id,
            "assignee_person_ids": [],
        }
    }
    assert support_ticket_settings_service.sla_policy(db_session)["normal"] == {
        "response_hours": 2,
        "resolution_hours": 12,
        "aging_hours": 6,
    }
    assert support_ticket_settings_service.ticket_type_sla_policy(db_session) == {
        "incident": 0,
        "core link disconnection": 48,
    }
    db_session.commit()
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open", "pending"],
        priorities=["normal"],
        ticket_types=["incident"],
        auto_assign_max_open_tickets="",
    )
    assert (
        support_ticket_settings_service.auto_assign_max_open_tickets(db_session) is None
    )


def test_ticket_settings_projects_active_native_teams_without_writing_them(db_session):
    active = _native_team(db_session, name="Field Operations")
    inactive = _native_team(db_session, name="Retired Team")
    inactive.is_active = False
    db_session.commit()

    assert support_ticket_settings_service.list_service_teams(db_session) == [
        {"id": str(active.id), "label": "Field Operations"}
    ]


@pytest.mark.parametrize("submitted", (None, "", "forged", "NORTH"))
def test_portal_region_validation_rejects_noncanonical_values(db_session, submitted):
    assert (
        support_ticket_settings_service.canonical_region_option(db_session, submitted)
        is None
    )


def test_portal_region_validation_returns_current_canonical_value(db_session):
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open"],
        priorities=["normal"],
        ticket_types=["incident"],
        regions=["lagos", "abuja"],
    )

    assert support_ticket_settings_service.list_canonical_region_options(
        db_session
    ) == ["abuja", "lagos"]
    assert (
        support_ticket_settings_service.canonical_region_option(db_session, "lagos")
        == "lagos"
    )


def test_portal_team_routing_prefers_active_customer_experience(db_session):
    expected = _native_team(db_session, name="customer EXPERIENCE")
    _native_team(db_session, name="System Admin")

    resolution = support_ticket_settings_service.resolve_portal_ticket_team_routing(
        db_session
    )

    assert resolution.service_team_id == expected.id
    assert resolution.service_team_name == expected.name
    assert resolution.source.value == "customer_experience"


def test_portal_team_routing_falls_back_to_active_system_admin(db_session):
    inactive = _native_team(db_session, name="Customer Experience")
    inactive.is_active = False
    db_session.commit()
    expected = _native_team(db_session, name="SYSTEM admin")

    resolution = support_ticket_settings_service.resolve_portal_ticket_team_routing(
        db_session
    )

    assert resolution.service_team_id == expected.id
    assert resolution.source.value == "system_admin"


def test_portal_team_routing_is_unassigned_without_an_exact_active_match(db_session):
    _native_team(db_session, name="Admin")
    inactive = _native_team(db_session, name="System Admin")
    inactive.is_active = False
    db_session.commit()

    resolution = support_ticket_settings_service.resolve_portal_ticket_team_routing(
        db_session
    )

    assert resolution.service_team_id is None
    assert resolution.service_team_name is None
    assert resolution.source.value == "unassigned"


def test_assignment_rule_create_and_delete(db_session):
    team = _native_team(db_session, name="Support")
    team_id = str(team.id)

    rule = support_ticket_settings_service.create_assignment_rule(
        db_session,
        name="North incidents",
        priority="50",
        strategy="least_loaded",
        team_id=team_id,
        ticket_types=["incident"],
        regions=["North"],
        assignee_person_id=None,
        assignment_target="technician",
        is_active=True,
    )

    rows = support_ticket_settings_service.list_assignment_rules(db_session)
    assert rows == [
        {
            "id": str(rule.id),
            "name": "North incidents",
            "priority": 50,
            "is_active": True,
            "strategy": "least_loaded",
            "team_id": team_id,
            "team_label": "Support",
            "assignment_target": "technician",
            "assignee_person_id": "",
            "ticket_types": ["incident"],
            "regions": ["north"],
        }
    ]

    db_session.commit()
    support_ticket_settings_service.delete_assignment_rule(db_session, str(rule.id))
    assert db_session.query(TicketAssignmentRule).count() == 0
