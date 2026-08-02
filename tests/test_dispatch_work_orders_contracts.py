"""Dispatch work-order queue tiles and row actions are UI projection contracts.

The summary tiles come back as ``Kpi`` objects whose ``cohort_url`` filters the
work-order list to exactly the rows the tile counts (KPI-parity), and the
per-row queue button is an ``Action`` whose eligibility is owned by the
assignment transition command — never re-derived from the status string in the
template.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services import web_dispatch_work_orders as service
from app.services.ui_contracts import Action, Kpi, StateValue
from app.web.admin.dispatch_work_orders import _redirect

_COUNTS = {
    "total": 12,
    "active": 7,
    "scheduled": 3,
    "in_progress": 2,
    "completed": 5,
}


def test_summary_tiles_are_kpi_contracts_carrying_state():
    kpis = service._work_order_kpis(_COUNTS)

    assert set(kpis) == {"total", "active", "scheduled", "in_progress", "completed"}
    assert all(isinstance(kpi, Kpi) for kpi in kpis.values())
    assert all(isinstance(kpi.value, StateValue) for kpi in kpis.values())
    # The headline shows the counted figure, never a zero standing in for it.
    assert kpis["active"].value.value == 7
    assert kpis["active"].value.is_present


def test_each_tile_drills_into_exactly_the_cohort_it_counts():
    kpis = service._work_order_kpis(_COUNTS)

    for kpi in kpis.values():
        assert kpi.cohort_url.startswith("/admin/dispatch/work-orders")

    # Total is the whole list; the status tiles narrow by a single status; the
    # open-work tile narrows to non-terminal rows via the active filter.
    assert kpis["total"].cohort_url == "/admin/dispatch/work-orders"
    assert (
        kpis["scheduled"].cohort_url == "/admin/dispatch/work-orders?status=scheduled"
    )
    assert (
        kpis["in_progress"].cohort_url
        == "/admin/dispatch/work-orders?status=in_progress"
    )
    assert (
        kpis["completed"].cohort_url == "/admin/dispatch/work-orders?status=completed"
    )
    assert kpis["active"].cohort_url == "/admin/dispatch/work-orders?active=1"


def test_queue_action_is_allowed_for_open_work_orders():
    action = service._queue_action(SimpleNamespace(is_active=True, status="scheduled"))

    assert isinstance(action, Action)
    assert action.key == "queue"
    assert action.allowed is True
    assert action.reason is None
    assert action.permission == "operations:dispatch:assign"


def test_queue_action_is_blocked_with_a_reason_for_terminal_or_inactive():
    terminal = service._queue_action(
        SimpleNamespace(is_active=True, status="completed")
    )
    canceled = service._queue_action(SimpleNamespace(is_active=True, status="canceled"))
    inactive = service._queue_action(
        SimpleNamespace(is_active=False, status="scheduled")
    )

    for action in (terminal, canceled, inactive):
        assert action.allowed is False
        assert action.reason  # blocked actions must carry a non-empty reason
    assert "completed" in terminal.reason
    assert inactive.reason == "Work order is inactive"


def test_action_contract_rejects_an_allowed_action_that_carries_a_reason():
    with pytest.raises(ValueError):
        Action(key="queue", label="Queue", allowed=True, reason="nope")


def test_list_page_exposes_kpi_and_action_contracts(db_session):
    state = service.list_page(db_session)

    assert set(state["kpis"]) == {
        "total",
        "active",
        "scheduled",
        "in_progress",
        "completed",
    }
    assert all(isinstance(kpi, Kpi) for kpi in state["kpis"].values())
    assert all(isinstance(item["actions"]["queue"], Action) for item in state["items"])


def test_active_filter_narrows_the_list_to_non_terminal_work_orders(db_session):
    # The Active tile's cohort_url (?active=1) must resolve to a real filter so
    # the headline and the list it links to can never disagree.
    everything = service.list_page(db_session)
    active_only = service.list_page(db_session, active=True)

    assert active_only["active_filter"] is True
    assert all(
        item["work_order"].status not in ("completed", "canceled", "cancelled")
        for item in active_only["items"]
    )
    assert active_only["total"] <= everything["total"]


def test_template_consumes_the_kpi_and_action_contracts():
    source = (
        Path(__file__).resolve().parents[1]
        / "templates/admin/dispatch/work_orders.html"
    ).read_text(encoding="utf-8")

    # Tiles render the StateValue and deep-link to the owner's cohort URL.
    assert "kpis.total.value.value" in source
    assert "href=kpis.active.cohort_url" in source
    assert "href=kpis.completed.cohort_url" in source
    # Task-originated creation locks the validated subscriber/project/task scope.
    assert 'name="project_task_id"' in source
    assert "create_prefill.project_task_id" in source
    assert 'href="#create-work-order"' in source
    assert 'href="/admin/dispatch/work-orders/{{ wo.public_id }}"' in source
    assert 'name="assigned_technician_id"' not in source


def test_detail_template_owns_the_visible_assignment_next_action():
    source = (
        Path(__file__).resolve().parents[1]
        / "templates/admin/dispatch/work_order_detail.html"
    ).read_text(encoding="utf-8")

    assert "Linked context" in source
    assert 'name="assigned_technician_id"' in source
    assert "action_permitted(request, queue_action)" in source
    assert "/admin/support/tickets/{{ origin_ticket.id }}" in source


def test_post_create_redirect_identifies_the_new_work_order():
    response = _redirect(
        notice="Work order sub-new-work created",
        q="sub-new-work",
    )
    query = parse_qs(urlsplit(response.headers["location"]).query)

    assert query == {
        "notice": ["Work order sub-new-work created"],
        "q": ["sub-new-work"],
    }
