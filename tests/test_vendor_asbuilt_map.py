"""The vendor as-built capture is a map, not a raw GeoJSON textarea.

Field techs trace the route they built by tapping the map or dropping their GPS
position; the drawn line is serialized to the existing ``geojson`` submit field.
The detail route feeds the proposed route as tracing context.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

from app.services.vendor_portal_operations import _serialize_project
from app.web import vendor_portal

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates/vendor/project_detail.html"
).read_text(encoding="utf-8")


def test_asbuilt_uses_an_interactive_map_not_a_geojson_textarea():
    # The raw GeoJSON textarea the vendor used to paste into is gone.
    assert 'name="geojson" required rows=' not in TEMPLATE
    assert '{"type":"LineString","coordinates"' not in TEMPLATE  # old placeholder
    # Replaced by a leaflet map + a hidden geojson field the JS populates.
    assert 'id="asbuilt-map"' in TEMPLATE
    assert 'id="ab-geojson"' in TEMPLATE
    assert "/static/vendor/leaflet/leaflet.js" in TEMPLATE
    assert "/static/vendor/leaflet/leaflet.css" in TEMPLATE


def test_asbuilt_offers_gps_capture_and_serializes_a_linestring():
    # GPS "add my location" for on-site capture.
    assert 'id="ab-locate"' in TEMPLATE
    assert "navigator.geolocation" in TEMPLATE
    # Drawn vertices [lat, lng] are emitted as GeoJSON LineString [lng, lat].
    assert "type: 'LineString'" in TEMPLATE
    # Submit stays gated until a real line exists.
    assert "points.length < 2" in TEMPLATE
    assert "map.on('click'" in TEMPLATE
    assert 'role="region" aria-label="As-built route map"' in TEMPLATE
    assert 'role="status" aria-live="polite"' in TEMPLATE


def test_asbuilt_action_is_owned_and_only_allows_the_assigned_vendor():
    project = SimpleNamespace(
        id="p1",
        project_id="native-p1",
        project=SimpleNamespace(code="PRJ-1", name="Fiber install"),
        subscriber_id=None,
        assigned_vendor_id="vendor-1",
        assignment_type=None,
        status="in_progress",
        bidding_open_at=None,
        bidding_close_at=None,
        approved_quote_id=None,
        procurement_system=None,
        procurement_order_reference=None,
        procurement_delivery_status=None,
        procurement_delivery_error=None,
        procurement_delivered_at=None,
        notes=None,
        created_at=None,
        updated_at=None,
    )
    assigned = _serialize_project(project, viewer_vendor_id="vendor-1")
    available = _serialize_project(project, viewer_vendor_id="vendor-2")

    assert assigned["as_built_action"].allowed is True
    assert available["as_built_action"].allowed is False
    assert available["as_built_action"].reason
    assert "action_permitted(request, project.as_built_action)" in TEMPLATE


def test_detail_route_feeds_proposed_route_context():
    # The proposed route geometry is rendered server-side (vendor auth is
    # ownership-based, so it does not call the admin route API).
    source = inspect.getsource(vendor_portal.vendor_project_detail)
    assert "route_geojson" in source
    assert "build_vendor_project_route_geojson" in source
    assert "route_geojson" in TEMPLATE  # and the template consumes it
