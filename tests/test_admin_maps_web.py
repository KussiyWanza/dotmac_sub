"""Tests for the ported admin map pages (maps §C).

Covers route registration + permission guards, the field-map JSON context
builders, the vendor-route GeoJSON service shape, and a Jinja compile smoke
test for the new templates.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from app.models.dispatch import TechnicianProfile
from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
)
from app.models.field_location import FieldTechPresence
from app.models.field_movement import FieldWorkOrderMovement
from app.models.project import Project
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProposedRouteRevision,
    Vendor,
)
from app.models.work_order import WorkOrder
from app.services import field_maps as field_maps_service
from app.services import vendor_routes_api
from app.web.admin import field_maps as web_field_maps
from app.web.admin import vendor_routes as web_vendor_routes

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
        "/dispatch/movement-playback",
        "/dispatch/movement-playback/feed",
    } <= paths


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

    feed = field_maps_service.list_technician_positions(db_session)
    assert feed["count"] == 1
    assert feed["live_count"] == 1
    item = feed["items"][0]
    assert item["label"] == "Ada Field"
    assert item["latitude"] == 6.5244
    assert item["longitude"] == 3.3792
    assert item["is_live"] is True


def test_technician_positions_marks_stale(db_session):
    user = _user(db_session)
    profile = _technician(db_session, user)
    db_session.add(
        FieldTechPresence(
            technician_id=profile.id,
            person_id=user.id,
            status="on_shift",
            last_latitude=6.5,
            last_longitude=3.3,
            last_location_at=datetime.now(UTC) - timedelta(minutes=30),
        )
    )
    db_session.flush()

    feed = field_maps_service.list_technician_positions(
        db_session, stale_after_seconds=120
    )
    assert feed["count"] == 1
    assert feed["live_count"] == 0
    assert feed["items"][0]["is_live"] is False


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
        db_session, crm_work_order_id="wo-map-1"
    )
    assert feed["leg_count"] == 1
    assert feed["point_count"] == 2
    assert feed["points"][0]["kind"] == "start"
    assert feed["points"][1]["kind"] == "arrival"
    assert feed["points"][1]["latitude"] == 6.52

    picker = field_maps_service.list_movement_work_orders(db_session)
    assert {"public_id": "wo-map-1", "label": "Install fiber drop"} in picker


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

    geojson = vendor_routes_api.build_project_route_geojson(
        db_session, str(install.id)
    )
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
