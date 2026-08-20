"""Vendor proposed-route authoring UI and projection behavior."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProposedRouteRevision,
    ProposedRouteRevisionStatus,
    Vendor,
)
from app.schemas.vendor_portal import VendorRouteRevisionCreate
from app.services import network_map, vendor_routes_api
from app.services.field.map_assets import (
    SUPPORTED_FIELD_MAP_ASSET_TYPES,
    VENDOR_ROUTE_AUTHORING_POI_FILTERS,
    VENDOR_ROUTE_AUTHORING_POI_TYPES,
)
from app.services.network_map_contracts import (
    NetworkMapFeature,
    NetworkMapFeatureProperties,
    NetworkMapFeatureType,
    NetworkMapPlantLayer,
    NetworkMapPlantProjection,
    NetworkMapPointGeometry,
    VendorRoutePlanningMapProjection,
)
from app.services.ui_contracts import Action
from app.services.vendor_portal_operations import (
    VENDOR_ROUTE_AUTHORING_LAYER_FILTERS,
    VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS,
    VENDOR_ROUTE_AUTHORING_STATUS_FILTERS,
    _serialize_quote,
)
from app.web import vendor_portal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (PROJECT_ROOT / "templates/vendor/project_detail.html").read_text(
    encoding="utf-8"
)
AUTHORING_JS = (PROJECT_ROOT / "static/js/vendor-route-authoring.js").read_text(
    encoding="utf-8"
)
ASBUILT_JS = (PROJECT_ROOT / "static/js/vendor-asbuilt-map.js").read_text(
    encoding="utf-8"
)


def _route_payload(coordinates: list[list[float]]) -> VendorRouteRevisionCreate:
    return VendorRouteRevisionCreate(
        geojson={"type": "LineString", "coordinates": coordinates},
        length_meters=125.5,
    )


def test_route_payload_requires_a_valid_linestring() -> None:
    payload = _route_payload([[7.4, 9.0], [7.5, 9.1]])

    assert payload.geojson == {
        "type": "LineString",
        "coordinates": [[7.4, 9.0], [7.5, 9.1]],
    }
    assert payload.length_meters == pytest.approx(15_627.5, abs=1)
    assert payload.length_meters != 125.5

    invalid_geometries = (
        {"type": "Point", "coordinates": [7.4, 9.0]},
        {"type": "LineString", "coordinates": [[7.4, 9.0]]},
        {"type": "LineString", "coordinates": [[181, 9.0], [7.5, 9.1]]},
        {"type": "LineString", "coordinates": [[7.4, 91], [7.5, 9.1]]},
        {"type": "LineString", "coordinates": [[7.4, 9.0], [float("nan"), 9.1]]},
    )
    for geometry in invalid_geometries:
        with pytest.raises(ValidationError):
            VendorRouteRevisionCreate(geojson=geometry)
    with pytest.raises(ValidationError):
        VendorRouteRevisionCreate(
            geojson=payload.geojson,
            length_meters=float("inf"),
        )


def test_web_payload_maps_invalid_geometry_to_safe_validation_error() -> None:
    with pytest.raises(HTTPException) as exc:
        vendor_portal._route_revision_payload(
            '{"type":"LineString","coordinates":[[7.4,9.0]]}',
            None,
        )

    assert exc.value.status_code == 422
    assert "at least two map points" in str(exc.value.detail)


def _revision(
    revision_id,
    *,
    revision_number: int,
    status: str,
    length_meters: float | None,
    review_notes: str | None,
) -> SimpleNamespace:
    """A revision shaped for both projections ``_serialize_quote`` builds.

    ``route_authoring`` (this branch) only needs the presentation fields, but the
    same call also builds the staff ``route_revisions`` review list, which walks
    ``revision.quote`` out to the project and vendor. A fixture that omits those
    would only pass by accident of which projection got read.
    """
    return SimpleNamespace(
        id=revision_id,
        quote_id=None,
        revision_number=revision_number,
        status=status,
        length_meters=length_meters,
        review_notes=review_notes,
        submitted_at=None,
        submitted_by_person_id=None,
        reviewed_at=None,
        reviewed_by_person_id=None,
        route_geom=None,
        review_events=[],
        quote=SimpleNamespace(
            vendor_id=None,
            vendor=SimpleNamespace(name="Vendor"),
            project=SimpleNamespace(
                id=None, project=SimpleNamespace(name=None, code=None)
            ),
        ),
    )


def test_quote_projection_owns_route_status_and_actions() -> None:
    draft_id = uuid4()
    submitted_id = uuid4()
    quote = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        vendor_id=uuid4(),
        status="draft",
        currency="NGN",
        subtotal=0,
        vat_rate_percent=0,
        tax_total=0,
        total=0,
        valid_from=None,
        valid_until=None,
        submitted_at=None,
        reviewed_at=None,
        review_notes=None,
        line_items=[],
        route_revisions=[
            _revision(
                draft_id,
                revision_number=1,
                status=ProposedRouteRevisionStatus.draft.value,
                length_meters=100.0,
                review_notes=None,
            ),
            _revision(
                submitted_id,
                revision_number=2,
                status=ProposedRouteRevisionStatus.submitted.value,
                length_meters=None,
                review_notes="Awaiting survey evidence",
            ),
        ],
    )

    projection = _serialize_quote(quote)["route_authoring"]

    assert projection.create_action.allowed is True
    assert [item.id for item in projection.revisions] == [submitted_id, draft_id]
    assert projection.revisions[0].status.label == "Submitted"
    assert projection.revisions[0].submit_action.allowed is False
    assert projection.revisions[1].length_label == "100.0 m"
    assert projection.revisions[1].submit_action.allowed is True


def test_vendor_route_authoring_filter_contracts_are_explicit() -> None:
    assert all(
        option.selected_by_default for option in VENDOR_ROUTE_AUTHORING_LAYER_FILTERS
    )
    assert all(
        option.selected_by_default for option in VENDOR_ROUTE_AUTHORING_STATUS_FILTERS
    )
    assert tuple(
        option.value_meters
        for option in VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS
        if option.selected_by_default
    ) == (1000,)
    assert all(option.label for option in VENDOR_ROUTE_AUTHORING_LAYER_FILTERS)
    assert all(option.label for option in VENDOR_ROUTE_AUTHORING_STATUS_FILTERS)
    assert all(option.label for option in VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS)


def test_vendor_route_planner_reuses_minimized_canonical_plant_projection(
    monkeypatch,
) -> None:
    safe_feature = NetworkMapFeature(
        geometry=NetworkMapPointGeometry(longitude=7.4, latitude=9.1),
        properties=NetworkMapFeatureProperties(
            id=uuid4(),
            feature_type=NetworkMapFeatureType.fdh_cabinet,
            name="Canonical FDH",
            management_ip="10.0.0.1",
            notes="staff-only note",
        ),
    )
    excluded_feature = NetworkMapFeature(
        geometry=NetworkMapPointGeometry(longitude=7.5, latitude=9.2),
        properties=NetworkMapFeatureProperties(
            id=uuid4(),
            feature_type=NetworkMapFeatureType.network_device,
            name="Staff device",
            management_ip="10.0.0.2",
        ),
    )
    monkeypatch.setattr(
        network_map,
        "build_network_map_plant_projection",
        lambda *, db: NetworkMapPlantProjection(
            features=(safe_feature, excluded_feature),
            layer_counts=dict.fromkeys(NetworkMapPlantLayer, 0),
            unmatched_olt_count=0,
        ),
    )

    projection = network_map.build_vendor_route_planning_map_projection(db=object())

    assert isinstance(projection, VendorRoutePlanningMapProjection)
    assert len(projection.features) == 1
    transport = projection.to_transport()
    properties = transport["features"][0]["properties"]
    assert properties == {
        "id": str(safe_feature.properties.id),
        "type": "fdh_cabinet",
        "name": "Canonical FDH",
        "source_owner": "ui.network_map_projection",
    }


def test_authoring_ui_draws_saves_and_submits_owned_revisions() -> None:
    assert 'id="route-author-map"' in TEMPLATE
    assert 'id="route-author-geojson"' in TEMPLATE
    assert 'id="route-author-length"' in TEMPLATE
    assert "min-h-11 w-40 rounded-md border-slate-700 bg-slate-900" in TEMPLATE
    assert 'id="route-author-locate"' in TEMPLATE
    assert 'data-route-focus="{{ revision.id }}"' in TEMPLATE
    assert "/static/js/vendor-route-authoring.js" in TEMPLATE
    assert "VendorRouteAuthoring.mount" in TEMPLATE
    assert "networkPlantGeojson" in TEMPLATE
    assert "config.networkPlantGeojson" in AUTHORING_JS
    assert "canonical plant feature" in AUTHORING_JS
    assert "if (searchElement)" in AUTHORING_JS
    assert "networkPlantIcon" in AUTHORING_JS
    assert "applyPlantViewPreset" in AUTHORING_JS
    assert 'data-route-plant-filter="fdh_cabinet"' in TEMPLATE
    assert 'data-route-plant-filter="fiber_segment"' in TEMPLATE
    assert 'id="route-author-plant-view"' in TEMPLATE
    assert 'value="backbone"' in TEMPLATE
    assert 'value="customer_edge"' in TEMPLATE
    assert 'type: "LineString"' in AUTHORING_JS
    assert "navigator.geolocation" in AUTHORING_JS
    assert 'searchClearElement.addEventListener("click"' in AUTHORING_JS
    assert 'searchElement.value = ""' in AUTHORING_JS
    assert "window.alert" not in AUTHORING_JS
    assert 'role="alert"' in TEMPLATE
    assert "Submitting locks that revision for review" in TEMPLATE
    assert 'id="closure-pin-toggle"' in TEMPLATE
    assert 'id="closure-proposal-form"' in TEMPLATE
    assert "pending staff review" in ASBUILT_JS
    assert 'id="route-author-filters"' in TEMPLATE
    assert "{% for layer_filter in vendor_route_authoring_layer_filters %}" in TEMPLATE
    assert 'data-route-layer-filter value="{{ layer_filter.value }}"' in TEMPLATE
    assert "{{ layer_filter.label }}" in TEMPLATE
    assert (
        "{% for status_filter in vendor_route_authoring_status_filters %}" in TEMPLATE
    )
    assert 'data-route-status-filter value="{{ status_filter.value }}"' in TEMPLATE
    assert "{{ status_filter.label }}" in TEMPLATE
    assert "{% for poi_filter in vendor_route_authoring_poi_filters %}" in TEMPLATE
    assert 'data-route-poi-filter value="{{ poi_filter.value }}"' in TEMPLATE
    assert "{{ poi_filter.label }}" in TEMPLATE
    assert (
        "{% for radius_option in vendor_route_authoring_radius_options %}" in TEMPLATE
    )
    assert 'value="{{ radius_option.value_meters }}"' in TEMPLATE
    assert "{{ radius_option.label }}" in TEMPLATE
    assert 'data-route-filter-action="all"' in TEMPLATE
    assert 'data-route-filter-action="none"' in TEMPLATE
    assert 'data-route-filter-target="layer"' in TEMPLATE
    assert 'data-route-filter-target="status"' in TEMPLATE
    assert 'data-route-filter-target="poi"' in TEMPLATE
    assert tuple(option.value for option in VENDOR_ROUTE_AUTHORING_LAYER_FILTERS) == (
        "proposed",
        "as_built",
        "closure_proposal",
    )
    assert tuple(option.value for option in VENDOR_ROUTE_AUTHORING_STATUS_FILTERS) == (
        "draft",
        "submitted",
        "accepted",
        "rejected",
        "pending",
        "applied",
    )
    filter_values = tuple(
        filter_option.value for filter_option in VENDOR_ROUTE_AUTHORING_POI_FILTERS
    )
    assert filter_values == VENDOR_ROUTE_AUTHORING_POI_TYPES
    assert set(VENDOR_ROUTE_AUTHORING_POI_TYPES).issubset(
        SUPPORTED_FIELD_MAP_ASSET_TYPES
    )
    assert tuple(
        option.value_meters for option in VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS
    ) == (500, 1000, 5000, 10000)
    assert 'id="route-author-poi-radius"' in TEMPLATE
    assert 'id="route-author-poi-nearby"' in TEMPLATE
    assert 'aria-busy="false"' in TEMPLATE
    assert 'id="route-author-poi-clear"' in TEMPLATE
    assert 'id="route-author-filter-summary"' in TEMPLATE
    assert 'id="route-author-search-hint"' in TEMPLATE
    assert 'role="status" aria-live="polite"' in TEMPLATE
    assert 'aria-label="Map legend"' in TEMPLATE
    assert "Proposed route" in TEMPLATE
    assert 'stroke="#9333ea"' in TEMPLATE
    assert 'stroke="#10b981"' in TEMPLATE
    assert 'stroke-dasharray="6 5"' in TEMPLATE
    assert "Reference plant" in TEMPLATE
    assert "Reference plant helps planning" in TEMPLATE
    assert "syncContextLayerVisibility" in AUTHORING_JS
    assert "updateFilterSummary" in AUTHORING_JS
    assert "setDisabled" in AUTHORING_JS
    assert "setNearbyLoading" in AUTHORING_JS
    assert 'setAttribute("aria-busy"' in AUTHORING_JS
    assert "abortNearbyRequest" in AUTHORING_JS
    assert "abortSearchRequest" in AUTHORING_JS
    assert "clearNearbyPoints" in AUTHORING_JS
    assert "filterActionButtons" in AUTHORING_JS
    assert "applyFilterAction" in AUTHORING_JS
    assert "filtersForTarget" in AUTHORING_JS
    assert "syncFilterActionState" in AUTHORING_JS
    assert 'button.setAttribute("aria-pressed", String(active))' in AUTHORING_JS
    assert 'button.classList.toggle("text-emerald-400", active)' in AUTHORING_JS
    assert 'syncFilterActionState("poi")' in AUTHORING_JS
    assert '["layer", "status", "poi"].forEach(syncFilterActionState)' in AUTHORING_JS
    assert "Loading points" in AUTHORING_JS
    assert (
        'mapElement.scrollIntoView({ behavior: "smooth", block: "center" })'
        in AUTHORING_JS
    )
    assert "Searching selected reference plant types" in AUTHORING_JS
    assert "Reference only, not project-assigned" in AUTHORING_JS
    assert (
        "Nearby points cleared. Use Show near me to load the new radius."
        in AUTHORING_JS
    )
    assert "selectedPoiTypes().join" in AUTHORING_JS
    assert "/api/v1/field/vendor/map-assets/nearby" in AUTHORING_JS
    assert "renderNearbyPoints" in AUTHORING_JS
    assert "types=fdh_cabinet,splice_closure&limit=20" not in AUTHORING_JS

    create_source = inspect.getsource(vendor_portal.vendor_create_route_revision)
    submit_source = inspect.getsource(vendor_portal.vendor_submit_route_revision)
    detail_source = inspect.getsource(vendor_portal.vendor_project_detail)
    assert "CreateVendorRouteRevisionCommand(" in create_source
    assert "SubmitVendorRouteRevisionCommand(" in submit_source
    assert '"vendor_route_authoring_layer_filters"' in detail_source
    assert '"vendor_route_authoring_poi_filters"' in detail_source
    assert "build_vendor_route_planning_map_projection" in detail_source
    assert '"route_planning_map_geojson"' in detail_source
    assert '"vendor_route_authoring_radius_options"' in detail_source
    assert '"vendor_route_authoring_status_filters"' in detail_source
    assert "VENDOR_ROUTE_AUTHORING_LAYER_FILTERS" in detail_source
    assert "VENDOR_ROUTE_AUTHORING_POI_FILTERS" in detail_source
    assert "VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS" in detail_source
    assert "VENDOR_ROUTE_AUTHORING_STATUS_FILTERS" in detail_source
    assert "release_read_transaction(db)" in create_source
    assert "release_read_transaction(db)" in submit_source


def test_authoring_filter_contracts_render_in_project_template() -> None:
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
    rendered = templates.env.get_template("vendor/project_detail.html").render(
        request=SimpleNamespace(
            state=SimpleNamespace(
                csrf_token="test-token",
                auth={"permission_keys": ["*"]},
            )
        ),
        vendor=SimpleNamespace(name="Install Co"),
        project=SimpleNamespace(
            id=uuid4(),
            project_id=uuid4(),
            project_code="PRJ-1",
            project_name="Vendor map project",
            status="assigned",
            lifecycle_action=None,
            lifecycle_events=[],
            as_built_action=Action(
                key="submit_as_built",
                label="Submit as-built",
                allowed=True,
            ),
            as_built_submissions=[],
        ),
        quote=SimpleNamespace(
            id=uuid4(),
            status="draft",
            currency="NGN",
            total="0.00",
            line_items=[],
            edit_action=Action(key="edit_quote", label="Edit quote", allowed=True),
            route_authoring=SimpleNamespace(
                create_action=Action(
                    key="create_route_revision",
                    label="Save route draft",
                    allowed=True,
                ),
                revisions=[],
            ),
        ),
        invoice=None,
        route_geojson={"type": "FeatureCollection", "features": []},
        route_planning_map_geojson={"type": "FeatureCollection", "features": []},
        supply=SimpleNamespace(
            material_request_action=Action(
                key="request_material",
                label="Request material",
                allowed=False,
                reason="Not available in this test.",
            ),
            material_releases=[],
            advance_quote_total=None,
            advance_currency="NGN",
            advance_request_action=Action(
                key="request_advance",
                label="Request advance",
                allowed=False,
                reason="Not available in this test.",
            ),
            advances=[],
        ),
        vendor_work_orders=[],
        can_propose_closure=False,
        vendor_route_authoring_layer_filters=VENDOR_ROUTE_AUTHORING_LAYER_FILTERS,
        vendor_route_authoring_poi_filters=VENDOR_ROUTE_AUTHORING_POI_FILTERS,
        vendor_route_authoring_radius_options=VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS,
        vendor_route_authoring_status_filters=VENDOR_ROUTE_AUTHORING_STATUS_FILTERS,
        message=None,
    )

    for option in VENDOR_ROUTE_AUTHORING_LAYER_FILTERS:
        assert f'data-route-layer-filter value="{option.value}"' in rendered
        assert option.label in rendered
    for option in VENDOR_ROUTE_AUTHORING_STATUS_FILTERS:
        assert f'data-route-status-filter value="{option.value}"' in rendered
        assert option.label in rendered
    for option in VENDOR_ROUTE_AUTHORING_POI_FILTERS:
        assert f'data-route-poi-filter value="{option.value}"' in rendered
        assert option.label in rendered
    for option in VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS:
        assert f'value="{option.value_meters}"' in rendered
        assert option.label in rendered
    assert 'id="route-author-search-hint"' in rendered
    assert 'id="route-author-poi-nearby"' in rendered
    assert 'aria-label="Canonical plant map filters"' in rendered
    assert (
        "Search cabinets, closures, access points, buildings, or coordinates"
        in rendered
    )
    assert "vendor-map-search-surface" in rendered
    assert "backdrop-filter: blur(12px)" in rendered
    assert 'data-route-filter-action="all"' in rendered
    assert 'data-route-filter-target="poi"' in rendered
    assert (
        'data-route-filter-target="layer" class="font-semibold text-emerald-400 '
        'transition-colors hover:text-emerald-300" aria-pressed="true"' in rendered
    )
    assert (
        'data-route-filter-target="status" class="font-semibold text-emerald-400 '
        'transition-colors hover:text-emerald-300" aria-pressed="true"' in rendered
    )
    assert 'aria-busy="false"' in rendered
    assert "Reference plant helps planning" in rendered
    assert 'aria-label="Project workspace"' in rendered
    assert 'href="#route-plan"' in rendered
    assert "html { scroll-behavior: smooth; }" in rendered
    assert "@media (prefers-reduced-motion: reduce)" in rendered
    assert "bg-cyan-600" in rendered
    assert "text-cyan-400" in rendered
    assert "hover:bg-cyan-700 hover:text-white" in rendered
    assert "vendor-portal min-h-screen" in rendered
    assert "padding-left: 0.875rem" in rendered
    assert "padding-top: 0.625rem" in rendered
    assert "padding-bottom: 0.625rem" in rendered
    assert "border-bottom-color: #38bdf8" in rendered
    assert "border-bottom-left-radius: 0" in rendered
    assert "border-bottom-right-radius: 0" in rendered
    assert "box-shadow: inset 0 -2px 0 #38bdf8" in rendered
    assert 'input[type="search"]::-webkit-search-cancel-button' in rendered
    assert "-webkit-appearance: none" in rendered
    assert 'id="route-author-search-clear"' in rendered
    assert "hover:bg-slate-700 hover:text-white" in rendered
    assert "<summary class=" in rendered
    assert "Map layers and reference points" in rendered
    assert 'id="route-reference-details"' in rendered
    assert "data-route-reference-panel" in rendered
    assert "prefers-reduced-motion: reduce" in rendered
    assert "cubic-bezier(0.22, 1, 0.36, 1)" in rendered
    assert "border-slate-700 bg-slate-900 shadow-sm" in rendered
    for group_label in ("Layers", "Status", "Points"):
        assert f'<legend class="sr-only">{group_label}</legend>' in rendered
        assert (
            f'<p aria-hidden="true" class="text-xs font-semibold uppercase '
            f'text-slate-500 dark:text-slate-400">{group_label}</p>' in rendered
        )


def test_vendor_route_context_does_not_include_competing_vendor_routes(
    db_session,
    monkeypatch,
) -> None:
    project = Project(name="Scoped vendor route context")
    vendor = Vendor(name="Visible Vendor", code=f"VV-{uuid4().hex[:8]}")
    competitor = Vendor(name="Hidden Vendor", code=f"HV-{uuid4().hex[:8]}")
    db_session.add_all([project, vendor, competitor])
    db_session.flush()
    installation = InstallationProject(project_id=project.id)
    db_session.add(installation)
    db_session.flush()
    own_quote = ProjectQuote(project_id=installation.id, vendor_id=vendor.id)
    other_quote = ProjectQuote(project_id=installation.id, vendor_id=competitor.id)
    db_session.add_all([own_quote, other_quote])
    db_session.flush()
    own_revision = ProposedRouteRevision(
        quote_id=own_quote.id,
        revision_number=1,
    )
    other_revision = ProposedRouteRevision(
        quote_id=other_quote.id,
        revision_number=1,
    )
    db_session.add_all([own_revision, other_revision])
    db_session.commit()
    monkeypatch.setattr(
        vendor_routes_api,
        "_geom_to_geojson",
        lambda _db, _geom: {
            "type": "LineString",
            "coordinates": [[7.4, 9.0], [7.5, 9.1]],
        },
    )

    result = vendor_routes_api.build_vendor_project_route_geojson(
        db_session,
        str(installation.id),
        str(vendor.id),
    )

    proposed_ids = {
        feature["properties"]["id"]
        for feature in result["features"]
        if feature["properties"]["kind"] == "proposed"
    }
    assert proposed_ids == {str(own_revision.id)}
    assert str(other_revision.id) not in proposed_ids
