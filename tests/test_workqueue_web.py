from pathlib import Path
from uuid import uuid4

from fastapi.routing import APIRoute

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.support import Ticket, TicketStatus
from app.services.workqueue import ItemKind, WorkqueuePrincipal
from app.services.workqueue.web import build_page
from app.web.admin import workqueue as workqueue_web
from tests.staff_identity_fixtures import add_bound_staff_user


def test_workqueue_web_replaces_all_seven_crm_route_behaviors():
    routes = {
        (route.path, frozenset(route.methods))
        for route in workqueue_web.router.routes
        if isinstance(route, APIRoute)
    }

    assert ("/workqueue", frozenset({"GET"})) in routes
    assert ("/workqueue/_right-now", frozenset({"GET"})) in routes
    assert ("/workqueue/_section/{kind}", frozenset({"GET"})) in routes
    assert ("/workqueue/snooze", frozenset({"POST"})) in routes
    assert ("/workqueue/snooze/clear", frozenset({"POST"})) in routes
    assert ("/workqueue/claim", frozenset({"POST"})) in routes
    assert ("/workqueue/complete", frozenset({"POST"})) in routes


def test_workqueue_forms_keep_csrf_and_idempotency_evidence():
    root = Path("templates/admin/workqueue")
    row = (root / "_row.html").read_text(encoding="utf-8")
    index = (root / "index.html").read_text(encoding="utf-8")
    action_form = Path("templates/components/forms/action_form.html").read_text(
        encoding="utf-8"
    )

    assert row.count("components/forms/csrf_input.html") == 2
    assert row.count('name="request_id"') == 2
    assert 'action="/admin/workqueue/snooze"' in row
    assert 'action="/admin/workqueue/snooze/clear"' in row
    assert (
        'from "components/forms/action_form.html" import action_form with context'
        in row
    )
    assert "action_form(row.claim_action)" in row
    assert "action_form(row.complete_action)" in row
    assert "components/forms/csrf_input.html" in action_form
    assert 'name="{{ hidden.key }}"' in action_form
    assert 'name="confirmed"' in action_form
    assert "workqueue-refresh from:body" in index
    assert "/api/v1/workqueue/events" in index
    assert 'addEventListener("workqueue_changed"' in index
    assert "Generated {{ projection.generated_at_label }}" in (
        root / "_right_now.html"
    ).read_text(encoding="utf-8")


def test_web_projection_exposes_identity_state_urgency_owner_hint_and_next_action(
    db_session,
):
    actor_id = uuid4()
    user, person = add_bound_staff_user(db_session, system_user_id=actor_id)
    team = ServiceTeam(name="Support")
    db_session.add(team)
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=person.id))
    ticket = Ticket(
        title="Customer offline",
        status=TicketStatus.open.value,
        priority="urgent",
        service_team_id=team.id,
    )
    db_session.add(ticket)
    db_session.flush()
    principal = WorkqueuePrincipal(
        person_id=user.id,
        roles=frozenset(),
        scopes=frozenset(),
        can_view=True,
        can_act=True,
    )

    projection = build_page(db_session, principal)
    row = next(
        row
        for section in projection.sections
        for row in section.rows
        if row.item_id == ticket.id
    )

    assert row.title == "Customer offline"
    assert row.status_label == "Open"
    assert row.urgency_label
    assert row.reason_label
    assert row.url == f"/admin/support/tickets/{ticket.id}"
    assert row.can_claim is True
    assert row.claim_action is not None
    assert row.complete_action is not None
    claim_hidden = {value.key: value.value for value in row.claim_action.hidden_values}
    assert claim_hidden["item_kind"] == ItemKind.ticket.value
    assert claim_hidden["item_id"] == str(ticket.id)
    assert claim_hidden["request_id"]
    assert claim_hidden["state_fingerprint"]
    assert row.complete_action.confirmation is not None
    assert projection.generated_at_label.endswith("WAT")
