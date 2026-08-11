from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.datastructures import FormData

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.support import Ticket, TicketChannel
from app.models.ticket_workflow import TicketAssignmentRule
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services import web_support_tickets as web_support_tickets_service
from app.web.admin import system as admin_system
from tests.staff_identity_fixtures import add_bound_staff_user


def _configuration_command(**overrides):
    values = {
        "statuses": ("open", "pending"),
        "priorities": ("normal",),
        "ticket_types": ("incident",),
    }
    values.update(overrides)
    return support_ticket_settings_service.TicketConfigurationUpdate(**values)


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
    statuses = support_ticket_settings_service.list_status_options(db_session)
    assert "closed" in statuses
    assert "resolved" not in statuses
    assert support_ticket_settings_service.list_priority_options(db_session)
    assert support_ticket_settings_service.list_ticket_type_options(db_session)
    assert (
        support_ticket_settings_service.auto_assign_max_open_tickets(db_session) is None
    )


def test_ticket_settings_drive_support_ticket_form_context(db_session):
    team = _native_team(db_session, name="Field Ops")
    team_id = str(team.id)
    support_ticket_settings_service.update_ticket_configuration(
        db_session,
        _configuration_command(
            priorities=("normal", "critical"),
            ticket_types=("incident", "network audit"),
            regions=("lagos", "abuja"),
        ),
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
        support_ticket_settings_service.update_ticket_configuration(
            db_session,
            _configuration_command(statuses=("open", "needs_vendor")),
        )


def test_ticket_settings_canonicalize_legacy_resolved_to_closed(db_session):
    support_ticket_settings_service.update_ticket_configuration(
        db_session,
        _configuration_command(
            statuses=("open", "resolved", "closed"),
            priorities=("normal",),
            ticket_types=("incident",),
        ),
    )

    assert support_ticket_settings_service.list_status_options(db_session) == [
        "open",
        "closed",
    ]


def test_ticket_settings_persist_routing_and_sla(db_session):
    team = _native_team(db_session, name="Core Network")
    technician, _person = add_bound_staff_user(db_session)
    support_ticket_settings_service.update_ticket_configuration(
        db_session,
        _configuration_command(
            ticket_types=("incident", "core link disconnection"),
            regions=("North",),
            auto_assign=True,
            auto_assign_max_open_tickets=3,
            replace_auto_assign_max_open_tickets=True,
            routing_rules=(
                support_ticket_settings_service.RegionRoutingRuleUpdate(
                    region="North",
                    technician_person_id=technician.id,
                    service_team_id=team.id,
                ),
            ),
            sla_policy=(
                support_ticket_settings_service.TicketSlaPolicyUpdate(
                    priority="normal",
                    response_hours=2,
                    resolution_hours=12,
                    aging_hours=6,
                ),
            ),
            ticket_type_sla_policy=(
                support_ticket_settings_service.TicketTypeSlaPolicyUpdate(
                    ticket_type="incident", resolution_hours=0
                ),
                support_ticket_settings_service.TicketTypeSlaPolicyUpdate(
                    ticket_type="core link disconnection", resolution_hours=48
                ),
            ),
        ),
    )

    assert support_ticket_settings_service.auto_assign_enabled(db_session) is True
    assert support_ticket_settings_service.auto_assign_max_open_tickets(db_session) == 3
    assert support_ticket_settings_service.region_assignment_rules(db_session) == {
        "north": support_ticket_settings_service.RegionAssignmentRule(
            region="north",
            technician_person_id=technician.id,
            service_team_id=team.id,
        )
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
    support_ticket_settings_service.update_ticket_configuration(
        db_session,
        _configuration_command(
            auto_assign_max_open_tickets=None,
            replace_auto_assign_max_open_tickets=True,
        ),
    )
    assert (
        support_ticket_settings_service.auto_assign_max_open_tickets(db_session) is None
    )


def test_ticket_settings_reload_projects_saved_region_manager_and_team(db_session):
    team = _native_team(db_session, name="Test Team")
    manager, _person = add_bound_staff_user(db_session)
    support_ticket_settings_service.update_ticket_configuration(
        db_session,
        _configuration_command(
            regions=("Test-Region",),
            routing_rules=(
                support_ticket_settings_service.RegionRoutingRuleUpdate(
                    region="Test-Region",
                    ticket_manager_person_id=manager.id,
                    service_team_id=team.id,
                ),
            ),
        ),
    )

    context = admin_system._ticket_settings_page_values(db_session)

    assert context["routing_region_options"] == ["test-region"]
    assert context["routing_rule_rows"] == [
        {
            "region": "test-region",
            "ticket_manager_person_id": str(manager.id),
            "technician_person_id": "",
            "service_team_id": str(team.id),
            "assignee_person_ids": "",
        }
    ]
    assert {item["id"] for item in context["routing_service_team_options"]} == {
        str(team.id)
    }

    request = SimpleNamespace(
        state=SimpleNamespace(csrf_token="test-csrf-token"),
        query_params={},
        headers={},
        cookies={},
        url=SimpleNamespace(path="/admin/system/ticket-settings"),
        session={},
        client=None,
        scope={},
        url_for=lambda *_args, **_kwargs: "/",
    )
    html = admin_system.templates.env.get_template(
        "admin/system/ticket_settings.html"
    ).render(
        request=request,
        csrf_token="test-csrf-token",
        current_user={"name": "Test Admin", "email": "admin@example.com"},
        sidebar_stats={},
        active_menu="system",
        **context,
    )
    assert '<option value="test-region">test-region</option>' in html
    assert f'<option value="{team.id}">Test Team</option>' in html
    assert f'"ticket_manager_person_id": "{manager.id}"' in html


def test_ticket_settings_reject_assignments_without_region(db_session):
    manager, _person = add_bound_staff_user(db_session)

    with pytest.raises(
        support_ticket_settings_service.SupportTicketConfigurationError,
        match="Routing assignments require a region",
    ):
        support_ticket_settings_service.update_ticket_configuration(
            db_session,
            _configuration_command(
                routing_rules=(
                    support_ticket_settings_service.RegionRoutingRuleUpdate(
                        ticket_manager_person_id=manager.id
                    ),
                ),
            ),
        )

    assert support_ticket_settings_service.region_assignment_rules(db_session) == {}


def test_ticket_settings_route_returns_error_for_assignment_without_region(
    db_session, monkeypatch
):
    manager, _person = add_bound_staff_user(db_session)
    rendered: dict[str, object] = {}

    def render(_template, context, status_code=200):
        rendered.update(context)
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(admin_system.templates, "TemplateResponse", render)
    monkeypatch.setattr(
        admin_system,
        "_config_context",
        lambda _request, _db, context: context,
    )
    form = FormData(
        [
            ("status_values", "open"),
            ("priority_values", "normal"),
            ("ticket_type_values", "incident"),
            ("region_values", "test-region"),
            ("routing_regions", ""),
            ("routing_ticket_manager_person_ids", str(manager.id)),
            ("routing_technician_person_ids", ""),
            ("routing_service_team_ids", ""),
            ("routing_assignee_person_ids", ""),
            ("sla_priorities", "normal"),
            ("sla_response_hours", "1"),
            ("sla_resolution_hours", "8"),
            ("sla_aging_hours", "4"),
            ("sla_ticket_types", "incident"),
            ("sla_ticket_type_resolution_hours", "0"),
        ]
    )

    response = admin_system.ticket_settings_update(
        request=SimpleNamespace(), form=form, db=db_session
    )

    assert response.status_code == 400
    assert rendered["routing_rule_rows"][0]["ticket_manager_person_id"] == str(
        manager.id
    )
    assert "Routing assignments require a region" in rendered["errors"][0]


def test_ticket_settings_ignore_completely_blank_routing_row(db_session):
    support_ticket_settings_service.update_ticket_configuration(
        db_session,
        _configuration_command(
            routing_rules=(support_ticket_settings_service.RegionRoutingRuleUpdate(),),
        ),
    )

    assert support_ticket_settings_service.region_assignment_rules(db_session) == {}


def test_inactive_saved_assignment_is_visible_but_not_previewed(db_session):
    manager, _person = add_bound_staff_user(db_session)
    team = _native_team(db_session, name="Retired Test Team")
    support_ticket_settings_service.update_ticket_configuration(
        db_session,
        _configuration_command(
            regions=("test-region",),
            routing_rules=(
                support_ticket_settings_service.RegionRoutingRuleUpdate(
                    region="test-region",
                    ticket_manager_person_id=manager.id,
                    service_team_id=team.id,
                ),
            ),
        ),
    )
    manager.is_active = False
    team.is_active = False
    db_session.commit()

    context = admin_system._ticket_settings_page_values(db_session)

    saved_option = next(
        item for item in context["staff_options"] if item["id"] == str(manager.id)
    )
    assert saved_option["label"].endswith("(Inactive)")
    saved_team_option = next(
        item
        for item in context["routing_service_team_options"]
        if item["id"] == str(team.id)
    )
    assert saved_team_option["label"] == "Retired Test Team (Inactive)"
    assert context["routing_rule_rows"][0]["service_team_id"] == str(team.id)
    assert (
        support_ticket_settings_service.region_manager_routing_preview(db_session) == ()
    )


def test_ticket_settings_projects_active_native_teams_without_writing_them(db_session):
    active = _native_team(db_session, name="Field Operations")
    inactive = _native_team(db_session, name="Retired Team")
    inactive.is_active = False
    db_session.commit()

    assert support_ticket_settings_service.list_service_teams(db_session) == [
        {"id": str(active.id), "label": "Field Operations"}
    ]


@pytest.mark.parametrize("submitted", (None, "", "forged"))
def test_portal_region_validation_rejects_noncanonical_values(db_session, submitted):
    assert (
        support_ticket_settings_service.canonical_region_option(db_session, submitted)
        is None
    )


def test_portal_region_validation_returns_current_canonical_value(db_session):
    db_session.add_all(
        [
            Ticket(
                title="Zaria ticket",
                status="open",
                priority="normal",
                channel=TicketChannel.web,
                region="zaria",
                is_active=True,
            ),
            Ticket(
                title="Legacy Lagos ticket",
                status="open",
                priority="normal",
                channel=TicketChannel.web,
                region=" Lagos ",
                is_active=True,
            ),
            Ticket(
                title="Inactive Adamawa ticket",
                status="open",
                priority="normal",
                channel=TicketChannel.web,
                region="adamawa",
                is_active=False,
            ),
        ]
    )
    db_session.flush()
    support_ticket_settings_service.update_ticket_configuration(
        db_session,
        _configuration_command(statuses=("open",), regions=("lagos", "abuja")),
    )

    assert support_ticket_settings_service.list_canonical_region_options(
        db_session
    ) == ["abuja", "lagos", "zaria"]
    assert (
        support_ticket_settings_service.canonical_region_option(db_session, " LAGOS ")
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
