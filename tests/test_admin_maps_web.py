"""Tests for the ported admin map pages (maps §C).

Covers route registration + permission guards, the field-map JSON context
builders, the vendor-route GeoJSON service shape, and a Jinja compile smoke
test for the new templates.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.models.dispatch import TechnicianProfile
from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
)
from app.models.field_location import FieldTechPresence
from app.models.field_movement import FieldWorkOrderMovement
from app.models.project import Project
from app.models.subscriber import Address, AddressType, Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProposedRouteRevision,
    Vendor,
)
from app.models.work_order import WorkOrder
from app.schemas.field import (
    FieldLiveMapFeedQuery,
    FieldLiveMapSearchQuery,
    FieldMovementPlaybackFeed,
    FieldMovementPlaybackQuery,
)
from app.services import field_maps as field_maps_service
from app.services import vendor_routes_api
from app.web.admin import field_maps as web_field_maps
from app.web.admin import network as web_network
from app.web.admin import vendor_routes as web_vendor_routes

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Route registration + permission guards
# ---------------------------------------------------------------------------


def _get_route(router, path: str, method: str) -> APIRoute:
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"Route not found: {method} {path}")


def _contains_value(value, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, (tuple, list, set)):
        return any(_contains_value(item, expected) for item in value)
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    return False


def _route_has_permission(router, path: str, method: str, expected: str) -> bool:
    route = _get_route(router, path, method)
    for dependency in route.dependant.dependencies:
        closure = getattr(dependency.call, "__closure__", None) or ()
        for cell in closure:
            if _contains_value(cell.cell_contents, expected):
                return True
    return False


def test_field_map_routes_registered():
    paths = {
        route.path
        for route in web_field_maps.router.routes
        if isinstance(route, APIRoute)
    }
    assert {
        "/dispatch/live-map",
        "/dispatch/live-map/feed",
        "/dispatch/live-map/search",
        "/dispatch/movement-playback",
        "/dispatch/movement-playback/feed",
    } <= paths


def test_plant_data_requires_network_map_permission_while_dispatch_stays_dispatch_only():
    assert _route_has_permission(
        web_network.router, "/network/map/plant-data", "GET", "network:map:read"
    )
    assert not _route_has_permission(
        web_field_maps.router, "/dispatch/live-map", "GET", "network:map:read"
    )


def test_movement_playback_context_uses_public_work_order_id_and_technician_id():
    live_map = (PROJECT_ROOT / "templates/admin/dispatch/live_map.html").read_text(
        encoding="utf-8"
    )
    playback = (
        PROJECT_ROOT / "templates/admin/dispatch/movement_playback.html"
    ).read_text(encoding="utf-8")

    assert "?technician_id=' + encodeURIComponent(item.technician_id)" in live_map
    assert "?work_order=' + encodeURIComponent(item.id)" in live_map
    assert "params.set('work_order', wo)" in playback
    assert "params.set('technician_id', selectedTechnicianId)" in playback


def test_movement_playback_page_preserves_direct_technician_context(
    db_session, monkeypatch
):
    technician_id = uuid4()
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/admin/dispatch/movement-playback",
            "raw_path": b"/admin/dispatch/movement-playback",
            "query_string": f"technician_id={technician_id}".encode(),
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )
    monkeypatch.setattr(
        web_field_maps,
        "_ctx",
        lambda request, db, active_page: {
            "request": request,
            "active_page": active_page,
        },
    )
    captured: dict[str, object] = {}

    def capture_template(name, context):
        captured.update({"name": name, "context": context})
        return captured

    monkeypatch.setattr(
        web_field_maps.templates,
        "TemplateResponse",
        capture_template,
    )

    response = web_field_maps.field_movement_playback(
        request=request,
        work_order=None,
        technician_id=technician_id,
        db=db_session,
    )

    assert response["name"] == "admin/dispatch/movement_playback.html"
    context = response["context"]
    assert context["selected_work_order"] is None
    assert context["selected_technician_id"] == str(technician_id)


def test_invalid_technician_id_is_rejected_by_typed_query_contract():
    with pytest.raises(ValidationError):
        FieldMovementPlaybackQuery(technician_id="not-a-uuid")


def test_movement_feed_adapter_passes_exact_technician_id(db_session, monkeypatch):
    technician_id = uuid4()
    captured: dict[str, object] = {}

    def capture_feed(*, db, filters):
        captured.update({"db": db, "filters": filters})
        return FieldMovementPlaybackFeed(leg_count=0, point_count=0, points=[])

    monkeypatch.setattr(
        field_maps_service,
        "list_movement_points",
        capture_feed,
    )

    response = web_field_maps.field_movement_playback_feed(
        work_order=None,
        technician_id=technician_id,
        since=None,
        until=None,
        limit=1000,
        db=db_session,
    )

    assert response.point_count == 0
    assert captured["db"] is db_session
    filters = captured["filters"]
    assert filters.technician_id == technician_id
    assert filters.work_order_public_id is None


def test_vendor_route_routes_registered():
    paths = {
        route.path
        for route in web_vendor_routes.router.routes
        if isinstance(route, APIRoute)
    }
    assert {"/vendors/routes", "/vendors/routes/{project_id}"} <= paths


@pytest.mark.parametrize(
    "path",
    [
        "/dispatch/live-map",
        "/dispatch/live-map/feed",
        "/dispatch/live-map/search",
        "/dispatch/movement-playback",
        "/dispatch/movement-playback/feed",
    ],
)
def test_field_map_routes_require_dispatch_permission(path):
    # Granular dispatch RBAC (#1329): live-map/playback reads require
    # operations:dispatch:read (was the coarse operations:dispatch).
    assert _route_has_permission(
        web_field_maps.router, path, "GET", "operations:dispatch:read"
    )


@pytest.mark.parametrize("path", ["/vendors/routes", "/vendors/routes/{project_id}"])
def test_vendor_route_routes_require_fiber_permission(path):
    assert _route_has_permission(
        web_vendor_routes.router, path, "GET", "network:fiber:read"
    )


def test_vendor_routes_geojson_api_registered():
    from app.api import vendor_routes as api_vendor_routes
    from app.main import _DEFERRED_API_ROUTER_SPECS

    paths = {
        route.path
        for route in api_vendor_routes.router.routes
        if isinstance(route, APIRoute)
    }
    assert "/vendor-routes/projects/{project_id}/geojson" in paths
    assert (
        "app.api.vendor_routes",
        "router",
        "api",
        "perm:network:fiber",
    ) in _DEFERRED_API_ROUTER_SPECS


# ---------------------------------------------------------------------------
# Fixtures / seeding
# ---------------------------------------------------------------------------


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Ada",
        last_name="Field",
        display_name="Ada Field",
        email=f"tech-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _technician(db_session, user: SystemUser) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=f"crm-{uuid4().hex[:8]}",
        title="Installer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Chika",
        last_name="Customer",
        email=f"cust-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


# ---------------------------------------------------------------------------
# Field live-map feed (context builder)
# ---------------------------------------------------------------------------


def test_technician_positions_feed_shape(db_session):
    user = _user(db_session)
    profile = _technician(db_session, user)
    db_session.add(
        FieldTechPresence(
            technician_id=profile.id,
            person_id=user.id,
            status="on_shift",
            location_sharing_enabled=True,
            last_latitude=6.5244,
            last_longitude=3.3792,
            last_location_accuracy_m=8.0,
            last_location_at=datetime.now(UTC),
        )
    )
    # A technician with no fix must be excluded from the map feed.
    other = _technician(db_session, _user(db_session))
    db_session.add(FieldTechPresence(technician_id=other.id, person_id=other.person_id))
    db_session.flush()

    feed = field_maps_service.list_technician_positions(
        db_session,
        FieldLiveMapFeedQuery(),
    )
    assert feed.count == 1
    assert feed.live_count == 1
    item = feed.items[0]
    assert item.label == "Ada Field"
    assert item.latitude == 6.5244
    assert item.longitude == 3.3792
    assert item.is_live is True


def test_technician_positions_marks_stale(db_session):
    user = _user(db_session)
    profile = _technician(db_session, user)
    db_session.add(
        FieldTechPresence(
            technician_id=profile.id,
            person_id=user.id,
            status="on_shift",
            location_sharing_enabled=True,
            last_latitude=6.5,
            last_longitude=3.3,
            last_location_at=datetime.now(UTC) - timedelta(minutes=30),
        )
    )
    db_session.flush()

    feed = field_maps_service.list_technician_positions(
        db_session,
        FieldLiveMapFeedQuery(stale_after_seconds=120),
    )
    assert feed.count == 1
    assert feed.live_count == 0
    assert feed.items[0].is_live is False


def test_technician_positions_excludes_disabled_location_sharing(db_session):
    user = _user(db_session)
    profile = _technician(db_session, user)
    db_session.add(
        FieldTechPresence(
            technician_id=profile.id,
            person_id=user.id,
            status="on_shift",
            location_sharing_enabled=False,
            last_latitude=6.5,
            last_longitude=3.3,
            last_location_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    feed = field_maps_service.list_technician_positions(
        db_session,
        FieldLiveMapFeedQuery(),
    )

    assert feed.count == 0
    assert feed.items == []


def test_live_map_search_matches_canonical_service_street(db_session):
    subscriber = _subscriber(db_session)
    db_session.add(
        Address(
            subscriber_id=subscriber.id,
            address_type=AddressType.service,
            is_primary=True,
            address_line1="14 Ahmadu Bello Way",
            city="Abuja",
            region="FCT",
            latitude=9.0765,
            longitude=7.3986,
        )
    )
    db_session.add(
        WorkOrder(
            public_id="sub-street-search",
            subscriber_id=subscriber.id,
            title="Restore subscriber service",
            status="dispatched",
        )
    )
    db_session.flush()

    result = field_maps_service.search_live_map(
        db_session,
        FieldLiveMapSearchQuery(query="Ahmadu Bello", limit=20),
    )

    assert result.count == 1
    item = result.items[0]
    assert item.kind == "work_order"
    assert item.id == "sub-street-search"
    assert item.detail == "14 Ahmadu Bello Way, Abuja, FCT"
    assert item.latitude == 9.0765
    assert item.longitude == 7.3986


def test_live_map_search_excludes_technicians_not_sharing_location(db_session):
    user = _user(db_session)
    profile = _technician(db_session, user)
    db_session.add(
        FieldTechPresence(
            technician_id=profile.id,
            person_id=user.id,
            status="on_shift",
            location_sharing_enabled=False,
            last_latitude=6.5,
            last_longitude=3.3,
            last_location_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    result = field_maps_service.search_live_map(
        db_session,
        FieldLiveMapSearchQuery(query="Ada Field", limit=20),
    )

    assert result.items == []


# ---------------------------------------------------------------------------
# Movement playback feed (context builder)
# ---------------------------------------------------------------------------


def test_movement_points_feed_shape(db_session):
    user = _user(db_session)
    profile = _technician(db_session, user)
    subscriber = _subscriber(db_session)
    mirror = WorkOrder(
        crm_work_order_id="wo-map-1",
        subscriber_id=subscriber.id,
        title="Install fiber drop",
        status="dispatched",
        scheduled_start=datetime.now(UTC),
    )
    db_session.add(mirror)
    db_session.flush()

    start = datetime.now(UTC)
    db_session.add(
        FieldWorkOrderMovement(
            work_order_mirror_id=mirror.id,
            actor_technician_id=profile.id,
            actor_person_id=user.id,
            destination_type="site",
            destination_label="Customer premises",
            started_at=start,
            arrived_at=start + timedelta(minutes=20),
            start_latitude=6.50,
            start_longitude=3.30,
            arrival_latitude=6.52,
            arrival_longitude=3.38,
            status="arrived",
        )
    )
    db_session.flush()

    feed = field_maps_service.list_movement_points(
        db=db_session,
        filters=FieldMovementPlaybackQuery(work_order_public_id="wo-map-1"),
    )
    assert feed.leg_count == 1
    assert feed.point_count == 2
    assert feed.points[0].kind == "start"
    assert feed.points[1].kind == "arrival"
    assert feed.points[1].latitude == 6.52

    picker = field_maps_service.list_movement_work_orders(db_session)
    assert any(
        item.public_id == "wo-map-1" and item.label == "Install fiber drop"
        for item in picker
    )


def test_technician_movement_histories_are_isolated(db_session):
    first_user = _user(db_session)
    first = _technician(db_session, first_user)
    second_user = _user(db_session)
    second = _technician(db_session, second_user)
    subscriber = _subscriber(db_session)
    mirror = WorkOrder(
        public_id=f"movement-isolation-{uuid4().hex}",
        subscriber_id=subscriber.id,
        title="Technician isolation",
        status="dispatched",
    )
    db_session.add(mirror)
    db_session.flush()
    started_at = datetime.now(UTC)
    db_session.add_all(
        [
            FieldWorkOrderMovement(
                work_order_mirror_id=mirror.id,
                actor_technician_id=first.id,
                actor_person_id=first_user.id,
                destination_type="site",
                destination_label="First technician site",
                started_at=started_at,
                start_latitude=6.51,
                start_longitude=3.31,
                status="en_route",
            ),
            FieldWorkOrderMovement(
                work_order_mirror_id=mirror.id,
                actor_technician_id=second.id,
                actor_person_id=second_user.id,
                destination_type="site",
                destination_label="Second technician site",
                started_at=started_at + timedelta(minutes=1),
                start_latitude=9.01,
                start_longitude=7.41,
                status="en_route",
            ),
        ]
    )
    db_session.flush()

    first_feed = field_maps_service.list_movement_points(
        db=db_session,
        filters=FieldMovementPlaybackQuery(technician_id=first.id),
    )
    second_feed = field_maps_service.list_movement_points(
        db=db_session,
        filters=FieldMovementPlaybackQuery(technician_id=second.id),
    )

    assert first_feed.leg_count == 1
    assert first_feed.points[0].label == "First technician site"
    assert first_feed.points[0].latitude == 6.51
    assert second_feed.leg_count == 1
    assert second_feed.points[0].label == "Second technician site"
    assert second_feed.points[0].latitude == 9.01


def test_unknown_technician_history_is_empty(db_session):
    feed = field_maps_service.list_movement_points(
        db=db_session,
        filters=FieldMovementPlaybackQuery(technician_id=uuid4()),
    )

    assert feed.leg_count == 0
    assert feed.point_count == 0
    assert feed.points == []


# ---------------------------------------------------------------------------
# Vendor route GeoJSON service (ST_AsGeoJSON pattern, sqlite-shimmed)
# ---------------------------------------------------------------------------


def _register_st_asgeojson(db_session) -> None:
    """Register a passthrough ST_AsGeoJSON on the sqlite connection.

    In the test suite geometry columns are stored/returned verbatim, so we seed
    ``route_geom`` with a GeoJSON string and let ST_AsGeoJSON echo it back —
    exercising the service's ``json.loads`` + FeatureCollection assembly.
    """
    raw = db_session.connection().connection
    sqlite_conn = getattr(raw, "driver_connection", raw)
    # GeoAlchemy2's sqlite compiler rewrites ``ST_AsGeoJSON`` -> ``AsGeoJSON``.
    sqlite_conn.create_function("AsGeoJSON", 1, lambda value: value)
    sqlite_conn.create_function("ST_AsGeoJSON", 1, lambda value: value)


def _seed_route_project(db_session):
    subscriber = _subscriber(db_session)
    project = Project(name="Fiber install — route test", subscriber_id=subscriber.id)
    db_session.add(project)
    db_session.flush()
    vendor = Vendor(name="Skyline Fiber Ltd", code=f"SKY-{uuid4().hex[:4]}")
    db_session.add(vendor)
    db_session.flush()
    install = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        assigned_vendor_id=vendor.id,
    )
    db_session.add(install)
    db_session.flush()
    quote = ProjectQuote(project_id=install.id, vendor_id=vendor.id)
    db_session.add(quote)
    db_session.flush()
    geojson = json.dumps(
        {"type": "LineString", "coordinates": [[3.37, 6.52], [3.38, 6.53]]}
    )
    db_session.add(
        ProposedRouteRevision(
            quote_id=quote.id,
            revision_number=1,
            route_geom=geojson,
            length_meters=1450.0,
        )
    )
    db_session.flush()
    return install


def test_build_project_route_geojson_shape(db_session):
    _register_st_asgeojson(db_session)
    install = _seed_route_project(db_session)

    fc = vendor_routes_api.build_project_route_geojson(db_session, str(install.id))
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    feature = fc["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "LineString"
    assert feature["properties"]["kind"] == "proposed"
    assert feature["properties"]["revision_number"] == 1


def test_list_route_projects_lists_projects_with_geometry(db_session):
    _register_st_asgeojson(db_session)
    install = _seed_route_project(db_session)

    projects = vendor_routes_api.list_route_projects(db_session)
    ids = {item["id"] for item in projects}
    assert str(install.id) in ids
    entry = next(item for item in projects if item["id"] == str(install.id))
    assert entry["has_proposed"] is True
    assert entry["vendor"] == "Skyline Fiber Ltd"

    summary = vendor_routes_api.get_route_project(db_session, str(install.id))
    assert summary is not None
    assert summary["label"] == "Fiber install — route test"


def test_closure_proposals_appear_on_route_map_and_project_list(db_session):
    subscriber = _subscriber(db_session)
    project = Project(name="Closure-only route", subscriber_id=subscriber.id)
    vendor = Vendor(name="Closure Vendor", code=f"CV-{uuid4().hex[:4]}")
    db_session.add_all([project, vendor])
    db_session.flush()
    install = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        assigned_vendor_id=vendor.id,
    )
    work_order = WorkOrder(
        subscriber_id=subscriber.id,
        project_id=project.id,
        public_id=f"sub-{uuid4().hex}",
    )
    db_session.add_all([install, work_order])
    db_session.flush()
    change_request = FiberChangeRequest(
        asset_type="splice_closure",
        operation=FiberChangeRequestOperation.create,
        requested_by_vendor_id=vendor.id,
        payload={
            "name": "Vendor closure A",
            "latitude": 9.08,
            "longitude": 7.49,
            "provenance": {
                "work_order_id": str(work_order.id),
                "work_order_public_id": work_order.public_id,
            },
        },
    )
    db_session.add(change_request)
    db_session.commit()

    geojson = vendor_routes_api.build_project_route_geojson(db_session, str(install.id))
    feature = next(
        item
        for item in geojson["features"]
        if item["properties"]["kind"] == "closure_proposal"
    )
    assert feature["geometry"]["coordinates"] == [7.49, 9.08]
    assert feature["properties"]["status"] == "pending"
    assert feature["properties"]["review_url"].endswith(str(change_request.id))

    projects = vendor_routes_api.list_route_projects(db_session)
    entry = next(item for item in projects if item["id"] == str(install.id))
    assert entry["has_closure_proposals"] is True

    vendor_geojson = vendor_routes_api.build_vendor_project_route_geojson(
        db_session, str(install.id), str(vendor.id)
    )
    assert any(
        item["properties"]["id"] == str(change_request.id)
        for item in vendor_geojson["features"]
    )


def test_get_route_project_missing_returns_none(db_session):
    assert vendor_routes_api.get_route_project(db_session, str(uuid4())) is None


# ---------------------------------------------------------------------------
# Template compile smoke
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template_name",
    [
        "admin/dispatch/live_map.html",
        "admin/dispatch/movement_playback.html",
        "admin/vendors/routes.html",
        "admin/vendors/route_view.html",
    ],
)
def test_map_templates_compile(template_name):
    # get_template parses + compiles the template source (Jinja syntax check).
    assert web_field_maps.templates.env.get_template(template_name) is not None


def test_live_map_template_exposes_street_search_and_focus_behavior():
    source = web_field_maps.templates.env.loader.get_source(
        web_field_maps.templates.env,
        "admin/dispatch/live_map.html",
    )[0]

    assert 'id="map-search"' in source
    assert "street" in source.lower()
    assert "/admin/dispatch/live-map/search" in source
    assert "map.setView(latlng, 16)" in source


def test_live_map_plant_legend_counts_geometry_categories_not_layer_totals():
    source = web_field_maps.templates.env.loader.get_source(
        web_field_maps.templates.env,
        "admin/dispatch/live_map.html",
    )[0]

    assert "fiberLineCount += 1" in source
    assert "group === plantGroups.osp" in source
    assert "group === plantGroups.sites" in source
    assert "(counts.osp || 0) + (counts.backbone || 0)" not in source
