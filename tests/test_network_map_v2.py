from __future__ import annotations

import hashlib
from pathlib import Path
from typing import get_type_hints
from uuid import uuid4

from fastapi.routing import APIRoute

from app.models.network import FiberSegment, FiberSegmentType, FiberTerminationPoint
from app.services import network_map
from app.services.network_map_contracts import (
    NetworkMapV2GeometryStatus,
    NetworkMapV2Projection,
    NetworkMapV2TopologyStatus,
)
from app.web.admin import network as web_network
from app.web.templates import templates

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_MAP_NORMALIZED_SHA256 = (
    "2e6316233fb7f66a95c1dc961044e0530c090a2a114b40644e40867e85d0bacb"
)


def _route(path: str) -> APIRoute:
    for route in web_network.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and "GET" in route.methods
        ):
            return route
    raise AssertionError(f"GET route not found: {path}")


def _contains(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, (tuple, list, set)):
        return any(_contains(item, expected) for item in value)
    if isinstance(value, dict):
        return any(_contains(item, expected) for item in value.values())
    return False


def _has_permission(route: APIRoute, expected: str) -> bool:
    for dependency in route.dependant.dependencies:
        closure = getattr(dependency.call, "__closure__", None) or ()
        if any(_contains(cell.cell_contents, expected) for cell in closure):
            return True
    return False


def _endpoint(*, latitude: float, longitude: float, reference: bool = False):
    return FiberTerminationPoint(
        id=uuid4(),
        name="Termination",
        ref_id=uuid4() if reference else None,
        latitude=latitude,
        longitude=longitude,
        is_active=True,
    )


def _segment(
    *,
    name: str,
    start: FiberTerminationPoint,
    end: FiberTerminationPoint,
    geometry: object,
):
    return FiberSegment(
        id=uuid4(),
        name=name,
        segment_type=FiberSegmentType.distribution,
        from_point_id=start.id,
        to_point_id=end.id,
        from_point=start,
        to_point=end,
        route_geom=geometry,
        fiber_count=12,
        is_active=True,
    )


def test_v2_route_is_isolated_and_uses_the_original_map_permission():
    original = _route("/network/map")
    v2 = _route("/network/map-v2")

    assert _has_permission(original, "network:map:read")
    assert _has_permission(v2, "network:map:read")
    templates.env.get_template("admin/network/map_v2.html")

    v2_template = (PROJECT_ROOT / "templates/admin/network/map_v2.html").read_text(
        encoding="utf-8"
    )
    assert '{% extends "admin/network/map.html" %}' in v2_template
    assert "/static/js/admin/network_map_v2.js" in v2_template
    assert (
        get_type_hints(network_map.build_network_map_v2_projection)["return"]
        is NetworkMapV2Projection
    )


def test_original_network_map_template_content_remains_unchanged():
    original = (PROJECT_ROOT / "templates/admin/network/map.html").read_bytes()
    normalized = original.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    assert hashlib.sha256(normalized).hexdigest() == ORIGINAL_MAP_NORMALIZED_SHA256


def test_nearby_unrelated_endpoints_are_not_inferred_as_connected():
    first_start = _endpoint(latitude=9.000000, longitude=7.000000)
    first_end = _endpoint(latitude=9.010000, longitude=7.010000)
    nearby_start = _endpoint(latitude=9.000001, longitude=7.000001)
    nearby_end = _endpoint(latitude=9.020000, longitude=7.020000)
    first = _segment(name="First", start=first_start, end=first_end, geometry="stored")
    nearby = _segment(
        name="Nearby", start=nearby_start, end=nearby_end, geometry="stored"
    )

    topology = network_map._v2_segment_topology(
        segments=(first, nearby),
        rendered_route_ids={first.id, nearby.id},
        geometry_available=True,
    )

    assert topology[0].topology_status is NetworkMapV2TopologyStatus.disconnected
    assert topology[1].topology_status is NetworkMapV2TopologyStatus.disconnected
    assert topology[0].from_endpoint.id != topology[1].from_endpoint.id
    assert topology[0].from_endpoint.attached_segment_count == 1
    assert topology[1].from_endpoint.attached_segment_count == 1


def test_explicit_endpoint_references_produce_connected_topology():
    start = _endpoint(latitude=9.0, longitude=7.0, reference=True)
    end = _endpoint(latitude=9.1, longitude=7.1, reference=True)
    segment = _segment(name="Explicit", start=start, end=end, geometry="stored")

    result = network_map._v2_segment_topology(
        segments=(segment,),
        rendered_route_ids={segment.id},
        geometry_available=True,
    )[0]

    assert result.geometry_status is NetworkMapV2GeometryStatus.stored_valid
    assert result.topology_status is NetworkMapV2TopologyStatus.connected
    assert result.from_endpoint.has_explicit_connection is True
    assert result.to_endpoint.has_explicit_connection is True


def test_missing_geometry_is_incomplete_and_serializes_no_fallback_line():
    start = _endpoint(latitude=9.0, longitude=7.0, reference=True)
    end = _endpoint(latitude=9.1, longitude=7.1, reference=True)
    segment = _segment(name="Missing route", start=start, end=end, geometry=None)

    result = network_map._v2_segment_topology(
        segments=(segment,),
        rendered_route_ids=set(),
        geometry_available=True,
    )[0]
    transport = result.to_transport()

    assert result.geometry_status is NetworkMapV2GeometryStatus.missing
    assert result.topology_status is NetworkMapV2TopologyStatus.incomplete
    assert "geometry" not in transport
    assert transport["from_endpoint"]["id"] == str(start.id)
    assert transport["to_endpoint"]["id"] == str(end.id)


def test_v2_frontend_labels_measurements_and_never_builds_endpoint_route_lines():
    source = (PROJECT_ROOT / "static/js/admin/network_map_v2.js").read_text(
        encoding="utf-8"
    )

    assert "Measurement only — not network topology" in source
    assert "No fallback line was drawn" in source
    assert "Endpoint markers never create connectivity by proximity" in source
    assert "L.polyline([segment.from_endpoint" not in source
    assert "L.polyline([segment.to_endpoint" not in source
