"""Local self-serve quote mirror service: reconcile, read, request, webhooks."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.models.audit import AuditEvent
from app.models.quote_mirror import QuoteMirror, QuoteSyncState
from app.models.subscriber import Subscriber
from app.services import quotes_mirror


def _refusals(db, action: str) -> list[AuditEvent]:
    return db.query(AuditEvent).filter(AuditEvent.action == action).all()


def _subscriber(db, crm_id: uuid.UUID | None = None) -> Subscriber:
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        crm_subscriber_id=crm_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _crm_quote(**kw):
    item = {
        "id": "q1",
        "status": "draft",
        "currency": "NGN",
        "total": "75000.00",
        "deposit_percent": 50,
        "deposit_amount": "37500.00",
        "deposit_paid": False,
        "feasibility": {
            "coverage": "covered",
            "feasible": True,
            "distance_meters": 800.0,
        },
        "estimate_provisional": False,
        "address": "12 Test St, Wuse",
        "latitude": 9.07,
        "longitude": 7.49,
        "line_items": [
            {"description": "Fiber installation (base)", "unit_price": "50000.00"}
        ],
        "created_at": "2026-06-29T10:00:00+00:00",
    }
    item.update(kw)
    return item


def test_webhook_accepted_upserts_and_pushes(db_session):
    sub = _subscriber(db_session)
    with patch("app.services.push.send_push") as push:
        out = quotes_mirror.apply_webhook(
            db_session,
            "quote.accepted",
            {"subscriber_id": str(sub.id), "quote_id": "q9", "status": "accepted"},
        )
    assert out["status"] == "ok"
    push.assert_called_once()
    assert push.call_args.kwargs["intent"].intent_code == "quote.accepted"
    assert push.call_args.kwargs["intent"].subject_id == "q9"
    row = db_session.query(QuoteMirror).filter_by(crm_quote_id="q9").one()
    assert row.status == "accepted"


def test_webhook_unmapped_ignored(db_session):
    out = quotes_mirror.apply_webhook(
        db_session,
        "quote.created",
        {"subscriber_id": str(uuid.uuid4()), "quote_id": "qX"},
    )
    assert out["reason"] == "unmapped_subscriber"


def test_webhook_unknown_event_ignored(db_session):
    sub = _subscriber(db_session)
    out = quotes_mirror.apply_webhook(
        db_session,
        "quote.archived",
        {"subscriber_id": str(sub.id), "quote_id": "q9"},
    )
    assert out["status"] == "ignored"


# ---------------------------------------------------------------------------
# Portal quote COMMANDS fail closed
#
# CRM/Omni was decommissioned on 2026-08-29. These two commands are the last
# Sub -> CRM business writes and they sit on a customer-money path, so a dead
# transport must produce an explicit, audited refusal -- never a hang, never a
# 200 with an empty body, and never a mirrored row for a command nobody
# acknowledged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("binding_state", ["absent", "disabled", "enabled"])
def test_retirement_refuses_commands_even_if_binding_was_left_enabled(
    db_session, binding_state
):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    with patch(
        "app.services.integrations.installations.require_enabled_capability_binding"
    ) as lookup:
        lookup.return_value = MagicMock(state=binding_state)
        with pytest.raises(quotes_mirror.PortalQuoteCommandError) as error:
            quotes_mirror.request_quote(
                db_session, str(sub.id), latitude=9.0, longitude=7.0
            )
        assert error.value.code == "sales.portal_quote.retired"
        with pytest.raises(quotes_mirror.PortalQuoteCommandError):
            quotes_mirror.accept_quote(
                db_session,
                str(sub.id),
                "q1",
                deposit_reference="test",
                deposit_amount="1",
            )
        with pytest.raises(quotes_mirror.PortalQuoteCommandError):
            quotes_mirror.ensure_portal_quote_commands_available(db_session)
        lookup.assert_not_called()
    assert db_session.query(QuoteMirror).count() == 0
    assert _refusals(db_session, quotes_mirror.PORTAL_QUOTE_REQUEST_REFUSED)


def test_historical_read_preserves_payload_and_sync_timestamp_without_refresh(
    db_session,
):
    from datetime import UTC, datetime

    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    quotes_mirror._upsert_row(db_session, subscriber_id=sub.id, item=_crm_quote())
    original = datetime(2026, 1, 1, tzinfo=UTC)
    db_session.add(QuoteSyncState(subscriber_id=sub.id, synced_at=original))
    db_session.commit()
    with patch.object(quotes_mirror, "reconcile_subscriber") as refresh:
        result = quotes_mirror.read_for_subscriber_result(db_session, str(sub.id))
        refresh.assert_not_called()
    assert result.state == quotes_mirror.QuoteReadState.retired
    assert result.payload["actions_available"] is False
    assert result.payload["total"] == 1
    assert result.payload["quotes"][0]["deposit_amount"] == "37500.00"
    assert result.payload["quotes"][0]["id"] == "q1"
    assert (
        db_session.get(QuoteSyncState, sub.id).synced_at.replace(tzinfo=UTC) == original
    )
    assert not db_session.dirty


def test_cold_historical_read_is_retired_without_creating_sync_state(db_session):
    sub = _subscriber(db_session)
    result = quotes_mirror.read_for_subscriber_result(db_session, str(sub.id))
    assert result.state == quotes_mirror.QuoteReadState.retired
    assert result.payload["quotes"] == []
    assert db_session.get(QuoteSyncState, sub.id) is None


def test_queued_quote_jobs_and_compatibility_helpers_do_no_database_work():
    from app.services.quote_retirement import retirement_outcome
    from app.tasks.quotes import (
        reconcile_quote_mirror,
        refresh_quote_mirror_for_subscriber,
    )

    with patch(
        "app.services.db_session_adapter.db_session_adapter.create_session"
    ) as session:
        expected = retirement_outcome().model_dump(mode="json")
        assert reconcile_quote_mirror() == expected
        assert refresh_quote_mirror_for_subscriber("obsolete-id") == expected
        session.assert_not_called()
    db = MagicMock()
    assert quotes_mirror.reconcile_all(db) == 0
    assert quotes_mirror.reconcile_subscriber(db, "obsolete-id") is False
    assert db.mock_calls == []


def test_persisted_quote_scheduler_aliases_are_disabled_without_touching_erp(
    db_session,
):
    from app.models.scheduler import ScheduledTask
    from app.services.scheduler_config import _retire_scheduled_task

    task_name = "app.tasks.quotes.reconcile_quote_mirror"
    rows = [
        ScheduledTask(
            name=f"retired-quote-{uuid.uuid4()}", task_name=task_name, enabled=True
        )
        for _ in range(2)
    ]
    erp = ScheduledTask(
        name=f"erp-{uuid.uuid4()}",
        task_name="app.tasks.dotmac_erp_outbox.sync_erp_operational_domains",
        enabled=True,
    )
    db_session.add_all([*rows, erp])
    db_session.commit()
    _retire_scheduled_task(db_session, task_name)
    assert all(not row.enabled for row in rows)
    assert erp.enabled
    _retire_scheduled_task(db_session, task_name)
    assert all(not row.enabled for row in rows)


def test_retired_portal_copy_preserves_history_without_inviting_live_actions():
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    root = Path(__file__).resolve().parents[1] / "templates"
    source = (root / "customer/quotes/index.html").read_text(encoding="utf-8")
    # Render the content block to avoid unrelated application branding globals.
    source = source.replace('{% extends "layouts/customer.html" %}', "")
    rendered = (
        Environment(loader=FileSystemLoader(root), autoescape=True)
        .from_string(source)
        .render(
            quote_read_state="retired",
            quotes={"quotes": [], "total": 0, "open": 0},
        )
    )
    assert "Historical quote information" in rendered
    assert "no longer updated" in rendered
    assert "pin your installation address" not in rendered
    assert "Request a new quote and pay" not in rendered
