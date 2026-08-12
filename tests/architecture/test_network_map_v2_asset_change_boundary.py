from __future__ import annotations

import ast
from pathlib import Path

from fastapi.routing import APIRoute

from app.web.admin import network as web_network

ROOT = Path(__file__).resolve().parents[2]


def _route(path: str, method: str) -> APIRoute:
    for route in web_network.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"{method} route not found: {path}")


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _contains(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, (tuple, list, set)):
        return any(_contains(item, expected) for item in value)
    if isinstance(value, dict):
        return any(_contains(item, expected) for item in value.values())
    return False


def _has_permission(route: APIRoute, expected: str) -> bool:
    return any(
        any(
            _contains(cell.cell_contents, expected)
            for cell in (getattr(dependency.call, "__closure__", None) or ())
        )
        for dependency in route.dependant.dependencies
    )


def test_governed_write_routes_exist_only_under_network_map_v2():
    proposals = _route("/network/map-v2/proposals", "GET")
    submit = _route("/network/map-v2/proposals", "POST")
    approve = _route("/network/map-v2/proposals/{proposal_id}/approve", "POST")
    reject = _route("/network/map-v2/proposals/{proposal_id}/reject", "POST")

    assert all(
        route.response_model is None for route in (proposals, submit, approve, reject)
    )
    assert _has_permission(submit, "network:fiber:write")
    assert _has_permission(approve, "network:fiber:review")
    assert _has_permission(reject, "network:fiber:review")

    original_paths = {
        route.path
        for route in web_network.router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/network/map/")
    }
    assert "/network/map/proposals" not in original_paths


def test_coordinator_delegates_canonical_writes_to_the_existing_owner():
    coordinator = _source("app/services/network_map_asset_changes.py")
    canonical_owner = _source("app/services/fiber_change_requests.py")

    assert "fiber_change_requests.apply_governed_map_asset_change" in coordinator
    assert "execute_owner_command" in coordinator
    assert "def apply_governed_map_asset_change" in canonical_owner
    participant = (
        ast.get_source_segment(
            canonical_owner,
            next(
                node
                for node in ast.parse(canonical_owner).body
                if isinstance(node, ast.FunctionDef)
                and node.name == "apply_governed_map_asset_change"
            ),
        )
        or ""
    )
    assert "db.commit(" not in participant


def test_movement_blockers_use_explicit_relationships_not_proximity():
    source = _source("app/services/network_map_asset_changes.py")
    tree = ast.parse(source)
    blocker = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_movement_blockers"
    )
    segment = ast.get_source_segment(source, blocker) or ""

    assert "FiberTerminationPoint.ref_id == asset_id" in segment
    assert "FiberSegment.from_point_id" in segment
    assert "FiberSegment.to_point_id" in segment
    assert "latitude" not in segment
    assert "longitude" not in segment
    assert "distance" not in segment.lower()
    assert "haversine" not in segment.lower()


def test_v2_javascript_never_mutates_fibre_geometry():
    source = _source("static/js/admin/network_map_v2.js")

    assert "Proposed movement only" in source
    assert "topology: false" in source
    assert "/admin/network/map-v2/proposals" in source
    assert "/admin/network/map/proposals" not in source
