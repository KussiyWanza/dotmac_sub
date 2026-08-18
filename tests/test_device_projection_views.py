"""Tests for the device-projection read model + network-device list contract.

See app/services/device_projection_views.py (SQL read of device_projections) and
the ui.network_device_list_projection contract in
web_network_core_devices_inventory.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute

from app.models.network_monitoring import DeviceProjection
from app.services import device_projection_views
from app.services import web_network_core_devices_inventory as inventory
from app.web.admin import network as network_routes


def _proj(
    db,
    source_id,
    *,
    name,
    device_type="core",
    status="working",
    vendor="acme",
    **kw,
):
    row = DeviceProjection(
        device_type=device_type,
        source_id=source_id,
        name=name,
        operational_status=status,
        vendor=vendor,
        refreshed_at=kw.pop("refreshed_at", datetime.now(UTC)),
        **kw,
    )
    db.add(row)
    db.commit()
    return row


# --- Contract ---


def test_network_device_list_definition_capabilities():
    d = inventory.NETWORK_DEVICE_LIST_DEFINITION
    assert d.filterable_keys == ("type", "status", "vendor", "lifecycle")
    assert d.sortable_keys == ("name", "last_seen")
    assert d.default_sort == "name"
    assert d.default_per_page == 25


def test_build_network_device_list_query_normalizes_and_rejects():
    q = inventory.build_network_device_list_query(
        device_type="olt", status=" ", search="edge", page=2
    )
    assert q.filter_value("type") == "olt"
    assert q.filter_value("status") is None  # blank dropped
    assert q.search == "edge"
    assert q.sort_by == "name"
    with pytest.raises(ValueError):
        inventory.build_network_device_list_query(sort_by="vendor")  # not sortable
    with pytest.raises(ValueError):
        inventory.build_network_device_list_query(per_page=30)  # not allowed


def test_network_devices_route_binds_list_handler_without_required_filters():
    routes = [
        route
        for route in network_routes.router.routes
        if isinstance(route, APIRoute)
        and route.path == "/network/devices"
        and "GET" in route.methods
    ]

    assert len(routes) == 1
    route = routes[0]
    assert route.endpoint is network_routes.devices_list
    assert not [param.name for param in route.dependant.query_params if param.required]
    assert any(
        cell.cell_contents == "network:device:read"
        for dependency in route.dependant.dependencies
        for cell in (getattr(dependency.call, "__closure__", None) or ())
    )


# --- Read owner ---


def test_query_filters_sorts_and_paginates(db_session):
    _proj(db_session, "1", name="alpha", device_type="core", status="working")
    _proj(db_session, "2", name="bravo", device_type="olt", status="not_working")
    _proj(db_session, "3", name="charlie", device_type="core", status="working")

    # type filter
    rows, total = device_projection_views.query_device_projections(
        db_session, device_type="core"
    )
    assert total == 2
    assert [r["name"] for r in rows] == ["alpha", "charlie"]

    # sort desc
    rows, _ = device_projection_views.query_device_projections(
        db_session, sort_dir="desc"
    )
    assert [r["name"] for r in rows] == ["charlie", "bravo", "alpha"]

    # pagination
    rows, total = device_projection_views.query_device_projections(
        db_session, limit=2, offset=0
    )
    assert total == 3 and len(rows) == 2

    # status filter + search
    rows, total = device_projection_views.query_device_projections(
        db_session, status="not_working"
    )
    assert total == 1 and rows[0]["name"] == "bravo"
    rows, total = device_projection_views.query_device_projections(
        db_session, search="charl"
    )
    assert total == 1 and rows[0]["name"] == "charlie"

    # each row carries a projected status presentation (tone from the owner)
    assert rows[0]["status_presentation"].value == "working"
    assert rows[0]["status_presentation"].label == "Online"

    rows, total = device_projection_views.query_device_projections(
        db_session, status="not_working"
    )
    assert total == 1
    assert rows[0]["status_presentation"].value == "not_working"
    assert rows[0]["status_presentation"].label == "Offline"


def test_archived_rows_are_hidden_by_default_and_queryable_explicitly(db_session):
    _proj(db_session, "1", name="current", lifecycle_state="active")
    _proj(
        db_session,
        "2",
        name="retired",
        status="not_working",
        lifecycle_state="archived",
    )

    current, current_total = device_projection_views.query_device_projections(
        db_session
    )
    assert current_total == 1
    assert [row["name"] for row in current] == ["current"]

    archived, archived_total = device_projection_views.query_device_projections(
        db_session, lifecycle="archived"
    )
    assert archived_total == 1
    assert archived[0]["name"] == "retired"
    assert archived[0]["is_archived"] is True


def test_stats_and_repair_evidence(db_session):
    early = datetime.now(UTC) - timedelta(minutes=5)
    _proj(db_session, "1", name="a", device_type="core", status="working")
    _proj(
        db_session,
        "2",
        name="b",
        device_type="olt",
        status="not_working",
        refreshed_at=early,
    )
    _proj(db_session, "3", name="c", device_type="ont", status="working")

    stats = device_projection_views.device_projection_stats(db_session)
    assert stats["total"] == 3
    assert stats["core"] == 1 and stats["olt"] == 1 and stats["ont"] == 1
    assert stats["working"] == 2 and stats["not_working"] == 1

    # Repair evidence remains available for diagnostics, not as a UI state.
    latest = device_projection_views.latest_refreshed_at(db_session)
    assert latest is not None


def test_devices_list_page_data_reads_projection_with_pagination(db_session):
    for i in range(30):
        _proj(db_session, str(i), name=f"dev-{i:02d}", device_type="core")

    query = inventory.build_network_device_list_query(per_page=25)
    payload = inventory.devices_list_page_data(db_session, query)

    assert payload["total"] == 30
    assert len(payload["devices"]) == 25  # one page
    assert payload["pagination"] is True  # >1 page → controls render
    assert payload["stats"]["total"] == 30
    assert "devices_refreshed_at" not in payload
    assert payload["htmx_url"] == "/admin/network/devices/filter"


# --- Boundary: the list read owner must not derive live status per request ---


def test_list_read_does_not_call_collect_devices(db_session, monkeypatch):
    _proj(db_session, "1", name="alpha")

    def _boom(*_a, **_k):
        raise AssertionError(
            "devices_list_page_data must read the projection, "
            "not derive via collect_devices"
        )

    monkeypatch.setattr(inventory, "collect_devices", _boom)
    query = inventory.build_network_device_list_query()
    payload = inventory.devices_list_page_data(db_session, query)
    assert payload["stats"]["total"] == 1
