from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.models.support import Ticket, TicketChannel, TicketStatus
from app.services import support as support_service
from app.services import web_support_tickets
from app.services.list_query import PageMeta
from app.web.admin import support_tickets as admin_support_tickets

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _ticket(**overrides) -> Ticket:
    defaults = {
        "title": f"Ticket {uuid4().hex[:8]}",
        "status": "open",
        "priority": "normal",
        "channel": TicketChannel.web,
        "is_active": True,
    }
    defaults.update(overrides)
    return Ticket(**defaults)


def _query(**overrides):
    values = {
        "search": None,
        "status": None,
        "ticket_type": None,
        "region": None,
        "assigned_to_me": False,
        "project_manager_person_id": None,
        "site_coordinator_person_id": None,
        "subscriber_id": None,
        "filters": None,
        "sort_by": "created_at",
        "sort_dir": "desc",
        "page": 1,
        "per_page": 25,
    }
    values.update(overrides)
    return web_support_tickets.build_ticket_list_query(**values)


def test_ticket_route_delegates_list_and_export_scope_to_projection_owner():
    route_path = PROJECT_ROOT / "app/web/admin/support_tickets.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
    list_route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "tickets_list"
    )
    export_route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "tickets_export_csv"
    )
    list_calls = {
        ast.unparse(node.func)
        for node in ast.walk(list_route)
        if isinstance(node, ast.Call)
    }
    export_calls = {
        ast.unparse(node.func)
        for node in ast.walk(export_route)
        if isinstance(node, ast.Call)
    }
    list_args = {arg.arg: ast.unparse(arg.annotation) for arg in list_route.args.args}
    export_args = {arg.arg for arg in export_route.args.args}

    assert "support_web_service.build_ticket_list_query" in list_calls
    assert "support_web_service.build_tickets_list_context" in list_calls
    assert (
        "support_ticket_bulk_actions_service.build_support_ticket_bulk_action_contract"
    ) in list_calls
    assert "support_web_service.build_ticket_list_query" in export_calls
    assert "support_web_service.render_tickets_csv" in export_calls
    assert list_args["per_page"] == "str | None"
    assert "filters" in export_args
    assert "region" in list_args
    assert "region" in export_args


def test_ticket_query_normalizes_declared_state_and_rejects_unknown_values():
    manager_id = uuid4()
    filters = json.dumps([["Ticket", "priority", "=", "high"]])
    query = _query(
        search=" TKT-100 ",
        status=" OPEN ",
        ticket_type=" billing ",
        region=" north ",
        assigned_to_me=True,
        project_manager_person_id=f" {manager_id} ",
        filters=filters,
        sort_by="number",
        sort_dir="asc",
        page=2,
        per_page="50",
    )

    assert query.search == "TKT-100"
    assert query.filter_value("status") == "open"
    assert query.filter_value("ticket_type") == "billing"
    assert query.filter_value("region") == "north"
    assert query.filter_value("assigned_to_me") == "true"
    assert query.filter_value("project_manager_person_id") == str(manager_id)
    assert query.filter_value("filters") == ('[["Ticket","priority","=","high"]]')
    assert query.sort_by == "number"
    assert query.sort_dir == "asc"
    assert query.page == 2
    assert query.per_page == 50

    not_closed = _query(status=" NOT_CLOSED ")
    assert not_closed.filter_value("status") == "not_closed"

    with pytest.raises(ValueError, match="Unsupported ticket status"):
        _query(status="invented")
    with pytest.raises(ValueError, match="must be a valid UUID"):
        _query(subscriber_id="not-a-uuid")
    with pytest.raises(ValueError, match="Unsupported sort field"):
        _query(sort_by="description")
    with pytest.raises(ValueError, match="not filterable"):
        _query(filters='[["Ticket","metadata","=","private"]]')


def test_not_closed_status_scope_excludes_only_closed_and_aligns_paging(
    db_session,
):
    tickets_per_status = 3
    db_session.add_all(
        _ticket(
            title=f"{status.value} ticket {index}",
            status=status.value,
        )
        for status in TicketStatus
        for index in range(tickets_per_status)
    )
    db_session.commit()

    expected_statuses = {
        status.value for status in TicketStatus if status is not TicketStatus.closed
    }

    context = web_support_tickets.build_tickets_list_context(
        db_session,
        list_query=_query(status="not_closed", page=1, per_page=25),
        actor_id=None,
        visible_columns_cookie=None,
    )

    assert context["status"] == "not_closed"
    assert context["total"] == len(expected_statuses) * tickets_per_status
    assert context["total_pages"] == 2
    assert len(context["tickets"]) == 25
    assert {ticket.status for ticket in context["tickets"]}.isdisjoint({"closed"})
    assert "status=not_closed" in context["list_query"].url(
        "/admin/support/tickets/export.csv"
    )

    exported = web_support_tickets.list_tickets_for_scope(
        db_session,
        list_query=_query(status="not_closed"),
        actor_id=None,
    )
    assert {ticket.status for ticket in exported} == expected_statuses

    csv_output = web_support_tickets.render_tickets_csv(
        db_session,
        list_query=_query(status="not_closed"),
        actor_id=None,
        visible_columns_cookie="status",
    )
    assert set(csv_output.splitlines()[1:]) == expected_statuses


def test_ticket_context_uses_exact_count_clamps_page_and_aligns_status_links(
    db_session,
):
    db_session.add_all(
        [
            _ticket(title="Open ticket", status="open"),
            _ticket(title="Closed ticket", status="closed"),
        ]
    )
    db_session.commit()

    context = web_support_tickets.build_tickets_list_context(
        db_session,
        list_query=_query(page=99),
        actor_id=None,
        visible_columns_cookie=None,
    )

    assert context["total"] == 2
    assert context["page"] == 1
    assert context["page_meta"].start_item == 1
    assert context["page_meta"].end_item == 2
    assert context["list_query"].page == 1
    cards = {card["value"]: card for card in context["status_summary_cards"]}
    assert cards[""]["count"] == 2
    assert cards[""]["active"] is True
    assert cards["open"]["count"] == 1
    assert cards["closed"]["count"] == 1
    assert cards["canceled"]["count"] == 0
    assert "status=open" in cards["open"]["href"]
    assert "page=1" in cards["open"]["href"]


def test_ticket_query_filters_before_paging_and_uses_stable_id_tie_breaker(
    db_session,
):
    opened_at = datetime(2026, 1, 1, tzinfo=UTC)
    first_id = UUID(int=1)
    second_id = UUID(int=2)
    db_session.add_all(
        [
            _ticket(
                id=second_id,
                title="Second high",
                priority="high",
                created_at=opened_at,
            ),
            _ticket(
                id=first_id,
                title="First high",
                priority="high",
                created_at=opened_at,
            ),
            _ticket(title="Filtered low", priority="low", created_at=opened_at),
        ]
    )
    db_session.commit()
    filters = '[["Ticket","priority","=","high"]]'

    context = web_support_tickets.build_tickets_list_context(
        db_session,
        list_query=_query(filters=filters, per_page=10),
        actor_id=None,
        visible_columns_cookie=None,
    )

    assert context["total"] == 2
    assert [ticket.id for ticket in context["tickets"]] == [first_id, second_id]


def test_ticket_region_filter_aligns_rows_counts_options_and_status_links(
    db_session,
):
    db_session.add_all(
        [
            _ticket(title="North open", status="open", region="north"),
            _ticket(title="South closed", status="closed", region="south"),
        ]
    )
    db_session.commit()

    context = web_support_tickets.build_tickets_list_context(
        db_session,
        list_query=_query(region="north"),
        actor_id=None,
        visible_columns_cookie=None,
    )

    assert context["region"] == "north"
    assert context["total"] == 1
    assert [ticket.title for ticket in context["tickets"]] == ["North open"]
    assert {"north", "south"}.issubset(set(context["region_options"]))
    cards = {card["value"]: card for card in context["status_summary_cards"]}
    assert cards[""]["count"] == 1
    assert cards["open"]["count"] == 1
    assert cards["closed"]["count"] == 0
    assert "region=north" in cards["open"]["href"]


def test_ticket_complete_scope_explicitly_disables_the_page_limit(monkeypatch):
    captured = {}

    def _list(_db, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(support_service.tickets, "list", _list)

    assert (
        web_support_tickets.list_tickets_for_scope(
            object(),
            list_query=_query(region="north", page=3, per_page=10),
            actor_id=None,
        )
        == []
    )
    assert captured["limit"] is None
    assert captured["offset"] == 0
    assert captured["region"] == "north"


def test_ticket_number_search_renders_htmx_list_response(db_session, monkeypatch):
    ticket_number = "TKT-SEARCH-2048"
    db_session.add_all(
        [
            _ticket(number=ticket_number, title="Matching ticket"),
            _ticket(number="TKT-OTHER-4096", title="Different ticket"),
        ]
    )
    db_session.commit()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/support/tickets",
            "query_string": f"search={ticket_number}".encode(),
            "headers": [(b"hx-request", b"true")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )
    monkeypatch.setattr(
        admin_support_tickets,
        "_ctx",
        lambda request, db: {"request": request},
    )
    monkeypatch.setattr(admin_support_tickets, "_actor_id", lambda request: None)

    response = admin_support_tickets.tickets_list(
        request=request,
        search=ticket_number,
        status=None,
        ticket_type=None,
        region=None,
        assigned_to_me=False,
        project_manager_person_id=None,
        site_coordinator_person_id=None,
        subscriber_id=None,
        filters=None,
        sort=None,
        direction=None,
        order_by=None,
        order_dir=None,
        page=1,
        per_page="25",
        db=db_session,
    )

    html = response.body.decode()
    assert response.status_code == 200
    assert ticket_number in html
    assert "TKT-OTHER-4096" not in html


def test_ticket_full_and_htmx_views_share_canonical_accessible_partials():
    page = (PROJECT_ROOT / "templates/admin/support/tickets/index.html").read_text(
        encoding="utf-8"
    )
    list_partial = (
        PROJECT_ROOT / "templates/admin/support/tickets/_list.html"
    ).read_text(encoding="utf-8")
    table = (PROJECT_ROOT / "templates/admin/support/tickets/_table.html").read_text(
        encoding="utf-8"
    )

    assert '{% include "admin/support/tickets/_list.html" %}' in page
    assert '{% include "admin/support/tickets/_table.html" %}' in list_partial
    assert 'hx-push-url="true"' in list_partial
    assert 'aria-current="page"' in list_partial
    assert 'x-bind:aria-expanded="open.toString()"' in list_partial
    assert 'id="ticket-column-toggle"' in list_partial
    assert 'aria-labelledby="ticket-column-toggle"' in list_partial
    assert (
        '@click.window.capture="if (!$el.contains($event.target)) open = false"'
        in list_partial
    )
    assert '@keydown.escape.window="open = false"' in list_partial
    assert "@click.outside" not in list_partial
    assert 'name="region" data-auto-submit' in list_partial
    assert "All Regions" in list_partial
    assert "{% for option in region_options %}" in list_partial
    assert '<option value="not_closed"' in list_partial
    assert ">Not closed</option>" in list_partial
    assert 'id="ticket-filter-apply"' in list_partial
    assert 'aria-label="Apply ticket filters"' in list_partial
    assert 'hx-include="#ticket-filter-form"' in list_partial
    assert 'hx-sync="#ticket-filter-form:replace"' in list_partial
    assert 'name="sort" value="{{ list_query.sort_by }}"' in list_partial
    assert "list_query.url('/admin/support/tickets'" in table
    assert 'aria-sort="' in table
    assert 'aria-current="page"' in table
    assert 'role="status"' in table
    assert 'aria-live="polite"' in table
    assert "/admin/support/tickets?page=" not in table
    assert "range(1, total_pages + 1)" not in table
    assert "page_meta.start_item" in table
    assert "TICKET_EXPORT_LIMIT" not in (
        PROJECT_ROOT / "app/services/web_support_tickets.py"
    ).read_text(encoding="utf-8")
    for literal_color in (
        "amber-",
        "orange-",
        "red-",
        "green-",
        "blue-",
        "yellow-",
        "purple-",
        "indigo-",
        "emerald-",
    ):
        assert literal_color not in page
        assert literal_color not in list_partial
        assert literal_color not in table


def test_ticket_table_contract_renders_with_empty_results():
    list_query = _query(search="missing", sort_by="number", sort_dir="asc")
    page_meta = PageMeta.from_query(list_query, total_items=0)
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

    html = templates.env.get_template("admin/support/tickets/_table.html").render(
        tickets=[],
        list_query=list_query,
        page_meta=page_meta,
        status_presentations={},
        staff_lookup={},
        subscriber_lookup={},
        sla_states={},
    )

    assert "No tickets found for the current filters." in html
    assert 'aria-sort="ascending"' in html
    assert "Showing tickets 0 to 0 of 0." in html
