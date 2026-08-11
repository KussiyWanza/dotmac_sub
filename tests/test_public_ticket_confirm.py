from __future__ import annotations

from types import SimpleNamespace

from app.models.support import TicketStatus
from app.schemas.support import TicketCreate
from app.services import support as support_service
from app.web.public import router as public_router
from app.web.public import ticket_confirm as ticket_confirm_routes


def _ticket_payload(subscriber_id):
    return TicketCreate(
        title="Internet unstable",
        description="Packet loss observed",
        subscriber_id=subscriber_id,
        customer_account_id=subscriber_id,
        channel="web",
        priority="normal",
    )


def _capture_template(monkeypatch):
    captured = {}

    def fake_template(name, context, status_code=200):
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return captured

    monkeypatch.setattr(
        ticket_confirm_routes.templates,
        "TemplateResponse",
        fake_template,
    )
    return captured


def test_ticket_confirm_public_routes_registered():
    paths = {getattr(route, "path", "") for route in public_router.routes}
    assert "/ticket-confirm/{token}" in paths
    assert "/ticket-confirm/{token}/confirm" in paths
    assert "/ticket-confirm/{token}/dispute" in paths


def test_ticket_access_token_urls_point_to_public_page(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("APP_URL", "https://selfcare.example.test")
    ticket = support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )
    token_row = support_service.ticket_access_tokens.mint(db_session, ticket)
    raw_token = token_row.raw_token
    assert raw_token

    urls = support_service.ticket_access_tokens.action_urls(token_row)

    expected = f"https://selfcare.example.test/ticket-confirm/{raw_token}"
    assert urls == {"confirm_url": expected, "dispute_url": expected}


def test_ticket_confirm_page_marks_token_accessed(db_session, subscriber, monkeypatch):
    captured = _capture_template(monkeypatch)
    ticket = support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )
    token_row = support_service.ticket_access_tokens.mint(db_session, ticket)
    raw_token = token_row.raw_token
    assert raw_token
    db_session.commit()

    response = ticket_confirm_routes.confirmation_page(
        SimpleNamespace(),
        raw_token,
        db_session,
    )

    db_session.refresh(token_row)
    assert response is captured
    assert captured["name"] == "public/ticket_confirm.html"
    assert captured["context"]["state"] == "ok"
    assert captured["context"]["ticket"]["ticket_ref"] == ticket.number
    assert token_row.accessed_at is not None


def test_ticket_confirm_page_never_projects_legacy_resolved_status(
    db_session, subscriber, monkeypatch
):
    captured = _capture_template(monkeypatch)
    ticket = support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )
    ticket.status = "resolved"
    token_row = support_service.ticket_access_tokens.mint(db_session, ticket)
    raw_token = token_row.raw_token
    assert raw_token
    db_session.commit()

    ticket_confirm_routes.confirmation_page(
        SimpleNamespace(),
        raw_token,
        db_session,
    )

    assert captured["context"]["ticket"]["status"] == "closed"


def test_ticket_confirm_page_confirm_action_closes_ticket(
    db_session, subscriber, monkeypatch
):
    captured = _capture_template(monkeypatch)
    ticket = support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )
    _, token_row = support_service.tickets.request_resolution_confirmation(
        db_session,
        str(ticket.id),
        actor_id=str(subscriber.id),
    )
    raw_token = token_row.raw_token
    assert raw_token

    response = ticket_confirm_routes.confirm_resolution_page(
        SimpleNamespace(),
        raw_token,
        db_session,
    )

    db_session.refresh(ticket)
    assert response is captured
    assert captured["context"]["state"] == "confirmed"
    assert ticket.status == TicketStatus.closed.value


def test_ticket_confirm_template_links_to_real_support_page():
    from pathlib import Path

    template = Path("templates/public/ticket_confirm.html").read_text()
    assert 'href="/portal/auth/support-info"' in template
    assert 'href="/support"' not in template
