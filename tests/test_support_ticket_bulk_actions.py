from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.support import Ticket, TicketChannel
from app.services import web_support_ticket_bulk, web_support_ticket_bulk_actions
from app.services.domain_errors import DomainError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _ticket(**overrides) -> Ticket:
    values = {
        "title": f"Bulk ticket {uuid4().hex[:8]}",
        "status": "open",
        "priority": "normal",
        "channel": TicketChannel.web,
        "is_active": True,
    }
    values.update(overrides)
    return Ticket(**values)


def _selection(*ticket_ids: str) -> dict[str, object]:
    return {"mode": "selected", "ids": list(ticket_ids)}


def test_support_ticket_bulk_projection_omits_unauthorized_actions_and_rows(
    db_session, monkeypatch
):
    eligible = _ticket()
    merged = _ticket(status="merged")
    db_session.add_all([eligible, merged])
    db_session.commit()
    monkeypatch.setattr(
        web_support_ticket_bulk_actions,
        "has_permission",
        lambda auth, _db, permission: permission in auth.get("permissions", []),
    )

    contract = (
        web_support_ticket_bulk_actions.build_support_ticket_bulk_action_contract(
            db_session,
            auth={"permissions": ["support:ticket:update"]},
            tickets=[eligible, merged],
        )
    )
    denied = web_support_ticket_bulk_actions.build_support_ticket_bulk_action_contract(
        db_session,
        auth={"permissions": []},
        tickets=[eligible],
    )

    assert contract["selection_enabled"] is True
    assert contract["filtered_selection_supported"] is False
    action = contract["actions"][0]
    assert action["key"] == "update"
    assert action["eligible_ids"] == [str(eligible.id)]
    assert action["ineligible_reasons"][str(merged.id)] == (
        "Merged source tickets cannot be updated"
    )
    assert denied["selection_enabled"] is False
    assert denied["actions"] == []


def test_support_ticket_bulk_preview_is_side_effect_free_and_reports_eligibility(
    db_session,
):
    eligible = _ticket(priority="normal")
    already_high = _ticket(priority="high")
    merged = _ticket(priority="normal", status="merged")
    db_session.add_all([eligible, already_high, merged])
    db_session.commit()

    preview = web_support_ticket_bulk.preview_support_ticket_bulk_update(
        db_session,
        {
            "selection": _selection(
                str(eligible.id),
                str(already_high.id),
                str(merged.id),
                "not-a-uuid",
            ),
            "updates": {"priority": "high"},
        },
    )

    db_session.refresh(eligible)
    assert eligible.priority == "normal"
    assert preview.resolved_ids == (
        str(eligible.id),
        str(already_high.id),
        str(merged.id),
    )
    assert preview.eligible_ids == (str(eligible.id),)
    reasons = {item["id"]: item["reason"] for item in preview.skipped}
    assert reasons[str(already_high.id)] == (
        "Ticket already matches the requested values"
    )
    assert reasons[str(merged.id)] == "Merged source tickets cannot be updated"
    assert reasons["not-a-uuid"] == "Ticket not found"


def test_support_ticket_bulk_canonicalizes_legacy_resolved_status(db_session):
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    preview = web_support_ticket_bulk.preview_support_ticket_bulk_update(
        db_session,
        {
            "selection": _selection(str(ticket.id)),
            "updates": {"status": "resolved"},
        },
    )

    assert preview.changes.status == "closed"
    assert "resolved" not in preview.changes.as_dict().values()


def test_support_ticket_bulk_confirmation_binds_changes_and_eligibility(db_session):
    ticket = _ticket(priority="normal")
    db_session.add(ticket)
    db_session.commit()
    payload = {
        "selection": _selection(str(ticket.id)),
        "updates": {"priority": "high"},
    }
    preview = web_support_ticket_bulk.preview_support_ticket_bulk_update(
        db_session, payload
    )
    confirmed_selection = {
        **payload["selection"],
        "expected_count": len(preview.resolved_ids),
        "expected_scope_token": preview.scope_token,
    }

    with pytest.raises(DomainError) as changed_update:
        web_support_ticket_bulk.require_support_ticket_bulk_confirmation(
            db_session,
            {
                "selection": confirmed_selection,
                "updates": {"priority": "urgent"},
            },
        )
    assert changed_update.value.code == "support_ticket_bulk_scope_drift"

    ticket.priority = "high"
    db_session.commit()
    with pytest.raises(DomainError) as changed_eligibility:
        web_support_ticket_bulk.require_support_ticket_bulk_confirmation(
            db_session,
            {"selection": confirmed_selection, "updates": payload["updates"]},
        )
    assert changed_eligibility.value.code == "support_ticket_bulk_scope_drift"


def test_support_ticket_bulk_execute_uses_canonical_update_and_structured_outcome(
    db_session,
):
    ticket = _ticket(priority="normal")
    unchanged = _ticket(priority="high")
    db_session.add_all([ticket, unchanged])
    db_session.commit()
    payload = {
        "selection": _selection(str(ticket.id), str(unchanged.id)),
        "updates": {"priority": "high"},
    }
    preview = web_support_ticket_bulk.preview_support_ticket_bulk_update(
        db_session, payload
    )

    result = web_support_ticket_bulk.execute_support_ticket_bulk_update(
        db_session,
        {
            **payload,
            "selection": {
                **payload["selection"],
                "expected_count": len(preview.resolved_ids),
                "expected_scope_token": preview.scope_token,
            },
            "confirmed": True,
        },
        actor_id=None,
    )

    db_session.refresh(ticket)
    assert ticket.priority == "high"
    assert result["selected_count"] == 2
    assert result["processed_count"] == 1
    assert result["skipped_count"] == 1
    assert result["processed_ids"] == [str(ticket.id)]
    actions = {
        row.action
        for row in db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "support_ticket")
        .all()
    }
    assert {"update", "priority_change", "bulk_update"} <= actions


def test_support_ticket_bulk_rejects_implicit_scope_and_invalid_updates(db_session):
    with pytest.raises(DomainError, match="Select at least one record"):
        web_support_ticket_bulk.preview_support_ticket_bulk_update(
            db_session,
            {"selection": {"mode": "selected", "ids": []}, "updates": {}},
        )
    with pytest.raises(DomainError, match="Filtered bulk selection is not supported"):
        web_support_ticket_bulk.preview_support_ticket_bulk_update(
            db_session,
            {"selection": {"mode": "filtered", "filters": {}}, "updates": {}},
        )
    with pytest.raises(DomainError, match="configured ticket priority"):
        web_support_ticket_bulk.preview_support_ticket_bulk_update(
            db_session,
            {
                "selection": _selection(str(uuid4())),
                "updates": {"priority": "invented"},
            },
        )
    with pytest.raises(DomainError, match="merge workflow"):
        web_support_ticket_bulk.preview_support_ticket_bulk_update(
            db_session,
            {
                "selection": _selection(str(uuid4())),
                "updates": {"status": "merged"},
            },
        )


def test_support_ticket_bulk_routes_are_thin_preview_and_execute_adapters():
    route_path = PROJECT_ROOT / "app/web/admin/support_tickets.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
    preview_route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "tickets_bulk_preview"
    )
    update_route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "tickets_bulk_update"
    )
    preview_calls = {
        ast.unparse(node.func)
        for node in ast.walk(preview_route)
        if isinstance(node, ast.Call)
    }
    update_calls = {
        ast.unparse(node.func)
        for node in ast.walk(update_route)
        if isinstance(node, ast.Call)
    }

    assert (
        "support_ticket_bulk_service.preview_support_ticket_bulk_update"
        in preview_calls
    )
    assert (
        "support_ticket_bulk_service.execute_support_ticket_bulk_update" in update_calls
    )


def test_legacy_bulk_method_delegates_each_mutation_to_ticket_update_owner():
    service_path = PROJECT_ROOT / "app/services/support.py"
    tree = ast.parse(service_path.read_text(encoding="utf-8"))
    tickets_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Tickets"
    )
    bulk_method = next(
        node
        for node in tickets_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "bulk_update"
    )
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(bulk_method)
        if isinstance(node, ast.Call)
    }
    assigned_attributes = {
        target.attr
        for node in ast.walk(bulk_method)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Attribute)
    }

    assert "Tickets.update" in calls
    assert not {"status", "priority", "assigned_to_person_id"} & assigned_attributes


def test_support_ticket_bulk_ui_uses_page_selection_and_in_modal_preview():
    page = (PROJECT_ROOT / "templates/admin/support/tickets/index.html").read_text(
        encoding="utf-8"
    )
    table = (PROJECT_ROOT / "templates/admin/support/tickets/_table.html").read_text(
        encoding="utf-8"
    )

    assert "support_ticket_bulk_action_contract.actions" in page
    assert 'x-show="selectedIds.length > 0"' in page
    assert 'role="status" aria-live="polite"' in page
    assert 'role="dialog"' in page
    assert 'x-trap.inert.noscroll="updateModal"' in page
    assert "Impact preview" in page
    assert "/admin/support/tickets/bulk/preview" in page
    assert "/admin/support/tickets/bulk/update" in page
    assert "expected_count" in page
    assert "expected_scope_token" in page
    assert "window.confirm" not in page
    assert "data-support-ticket-bulk-action" in page
    assert "data-bulk-contract" in table
    assert "support_ticket_bulk_action_contract.selection_enabled" in table
    assert 'aria-label="Select all tickets on this page"' in table
    assert ':indeterminate.prop="selectedIds.length > 0' in table
    assert 'aria-label="Select ticket {{ ticket.number or ticket.id }}"' in table
    assert "htmx:afterSwap" in page
    assert "this.clearSelection();" in page
    assert "selection: { mode: 'selected'" in page
    assert "mode: 'filtered'" not in page
