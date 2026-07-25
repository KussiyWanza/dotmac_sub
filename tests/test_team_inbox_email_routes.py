"""Mailbox routing: the table that decides which team owns an inbound address.

`TeamInboxEmailRoute` had a model and a consumer
(`build_email_team_routing_plan`) but no writer outside direct SQL, which is
why production ran six live mailboxes against zero rows. This covers the CRUD
that closes that gap.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §3 and §5 slice 6.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.services import team_inbox_commands, team_inbox_routing

ROUTES_TEMPLATE = Path("templates/admin/inbox/email_routes.html").read_text()
ROUTES_MODULE = Path("app/web/admin/inbox.py").read_text()


def _team(db_session, name):
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    captured = team.id
    db_session.commit()
    return captured


def test_routing_a_mailbox_makes_it_resolvable(db_session):
    team_id = _team(db_session, "Support")

    team_inbox_commands.create_email_route(
        db_session,
        service_team_id=team_id,
        email_address="Support@Dotmac.NG",
        is_primary=True,
    )

    rows = team_inbox_routing.list_email_routes(db_session)
    assert len(rows) == 1
    # Addresses normalise, so casing in the form cannot create a second route.
    assert rows[0].email_address == "support@dotmac.ng"
    assert rows[0].service_team_name == "Support"
    assert rows[0].is_primary is True


def test_the_plan_builder_sees_the_new_route(db_session):
    """The point of the table: inbound mail resolves to a team."""
    team_id = _team(db_session, "Support")
    team_inbox_commands.create_email_route(
        db_session, service_team_id=team_id, email_address="support@dotmac.ng"
    )

    plan = team_inbox_routing.build_email_team_routing_plan(
        db_session, to_addresses=["support@dotmac.ng"], cc_addresses=[]
    )

    assert plan.primary_service_team_id == str(team_id)
    assert plan.unmatched_recipients == []


def test_a_duplicate_address_for_the_same_team_is_refused(db_session):
    team_id = _team(db_session, "Support")
    team_inbox_commands.create_email_route(
        db_session, service_team_id=team_id, email_address="support@dotmac.ng"
    )

    with pytest.raises(team_inbox_routing.EmailRouteError) as exc:
        team_inbox_commands.create_email_route(
            db_session, service_team_id=team_id, email_address="support@dotmac.ng"
        )
    assert "already routed" in str(exc.value)


def test_only_one_primary_survives_per_team(db_session):
    """Two primaries would make team selection ambiguous."""
    team_id = _team(db_session, "Support")
    team_inbox_commands.create_email_route(
        db_session,
        service_team_id=team_id,
        email_address="first@dotmac.ng",
        is_primary=True,
    )
    team_inbox_commands.create_email_route(
        db_session,
        service_team_id=team_id,
        email_address="second@dotmac.ng",
        is_primary=True,
    )

    primaries = [
        r for r in team_inbox_routing.list_email_routes(db_session) if r.is_primary
    ]
    assert [r.email_address for r in primaries] == ["second@dotmac.ng"]


def test_deactivating_a_route_clears_primary_and_stops_resolution(db_session):
    team_id = _team(db_session, "Support")
    # The command returns the id so no read is needed between commands: a read
    # would leave the session in a transaction and the next command refuses it.
    route_id = team_inbox_commands.create_email_route(
        db_session,
        service_team_id=team_id,
        email_address="support@dotmac.ng",
        is_primary=True,
    )

    team_inbox_commands.delete_email_route(db_session, route_id=route_id)

    row = team_inbox_routing.list_email_routes(db_session)[0]
    assert row.is_active is False
    assert row.is_primary is False
    plan = team_inbox_routing.build_email_team_routing_plan(
        db_session, to_addresses=["support@dotmac.ng"], cc_addresses=[]
    )
    assert plan.primary_service_team_id is None
    assert plan.unmatched_recipients == ["support@dotmac.ng"]


def test_a_deactivated_route_can_be_reactivated(db_session):
    team_id = _team(db_session, "Support")
    route_id = team_inbox_commands.create_email_route(
        db_session, service_team_id=team_id, email_address="support@dotmac.ng"
    )
    team_inbox_commands.delete_email_route(db_session, route_id=route_id)

    team_inbox_commands.update_email_route(
        db_session, route_id=route_id, is_active=True
    )

    assert team_inbox_routing.list_email_routes(db_session)[0].is_active is True


def test_an_unknown_team_is_refused(db_session):
    import uuid

    with pytest.raises(team_inbox_routing.EmailRouteError):
        team_inbox_commands.create_email_route(
            db_session,
            service_team_id=uuid.uuid4(),
            email_address="support@dotmac.ng",
        )


def test_a_blank_address_is_refused(db_session):
    team_id = _team(db_session, "Support")
    with pytest.raises(team_inbox_routing.EmailRouteError):
        team_inbox_commands.create_email_route(
            db_session, service_team_id=team_id, email_address="   "
        )


def test_the_admin_surface_is_a_thin_adapter():
    assert "team_inbox_commands.create_email_route" in ROUTES_MODULE
    assert "team_inbox_commands.update_email_route" in ROUTES_MODULE
    assert "team_inbox_commands.delete_email_route" in ROUTES_MODULE
    # The route must not touch the ORM directly.
    assert "TeamInboxEmailRoute(" not in ROUTES_MODULE


def test_the_page_states_what_it_does_not_control():
    """Routing decides where forwarded mail lands; it does not forward it."""
    assert "forwards to it" in ROUTES_TEMPLATE
    assert "components/forms/csrf_input.html" in ROUTES_TEMPLATE
