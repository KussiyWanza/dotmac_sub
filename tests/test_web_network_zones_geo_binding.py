"""Admin zones UI adapter for the typed zone -> GeoArea binding."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData

from app.models.gis import GeoArea, GeoAreaType
from app.services import web_network_zones


def _area(db_session, name: str, *, is_active: bool = True) -> GeoArea:
    area = GeoArea(
        name=name,
        area_type=GeoAreaType.service_area,
        is_active=is_active,
    )
    db_session.add(area)
    db_session.flush()
    return area


def _form(**fields: str) -> FormData:
    return FormData([(key, value) for key, value in fields.items()])


def test_zone_form_binds_updates_and_clears_geo_area(db_session):
    area = _area(db_session, "Abuja Coverage")
    values = web_network_zones.parse_form_values(
        _form(name="Garki Zone", geo_area_id=str(area.id), is_active="true")
    )
    assert web_network_zones.validate_form(values) is None
    zone = web_network_zones.create_zone(db_session, values)
    assert zone.geo_area_id == area.id

    cleared = web_network_zones.parse_form_values(
        _form(name="Garki Zone", geo_area_id="", is_active="true")
    )
    updated = web_network_zones.update_zone(db_session, str(zone.id), cleared)
    assert updated.geo_area_id is None


def test_zone_form_rejects_inactive_geo_area(db_session):
    retired = _area(db_session, "Retired Coverage", is_active=False)
    values = web_network_zones.parse_form_values(
        _form(name="Stale Zone", geo_area_id=str(retired.id), is_active="true")
    )

    with pytest.raises(HTTPException) as excinfo:
        web_network_zones.create_zone(db_session, values)
    assert excinfo.value.status_code == 400


def test_detail_page_shows_inherited_resolution(db_session):
    area = _area(db_session, "Lagos Coverage")
    parent_values = web_network_zones.parse_form_values(
        _form(name="Lagos Parent", geo_area_id=str(area.id), is_active="true")
    )
    parent = web_network_zones.create_zone(db_session, parent_values)
    child_values = web_network_zones.parse_form_values(
        _form(name="Lekki Child", parent_id=str(parent.id), is_active="true")
    )
    child = web_network_zones.create_zone(db_session, child_values)

    payload = web_network_zones.detail_page_data(db_session, str(child.id))
    assert payload is not None
    assert payload["geo_area"] is None
    assert payload["effective_geo_area"].id == area.id


def test_zone_templates_expose_geo_area_binding():
    form = Path("templates/admin/network/zones/form.html").read_text(encoding="utf-8")
    detail = Path("templates/admin/network/zones/detail.html").read_text(
        encoding="utf-8"
    )

    assert 'name="geo_area_id"' in form
    assert "inherit from parent zone" in form
    assert "Geographic Area" in detail
    assert "Unbound (global routing applies)" in detail


def test_list_page_reports_the_binding_declared_on_each_zone(db_session):
    """The list has a bounded-query contract, so it shows own bindings only.

    A child that merely inherits is reported as unbound *here*; the detail
    page resolves the full parent chain.
    """

    area = _area(db_session, "Kano Coverage")
    bound = web_network_zones.create_zone(
        db_session,
        web_network_zones.parse_form_values(
            _form(name="Kano Parent", geo_area_id=str(area.id), is_active="true")
        ),
    )
    child = web_network_zones.create_zone(
        db_session,
        web_network_zones.parse_form_values(
            _form(name="Kano Child", parent_id=str(bound.id), is_active="true")
        ),
    )
    loose = web_network_zones.create_zone(
        db_session,
        web_network_zones.parse_form_values(_form(name="Kano Loose", is_active="true")),
    )

    labels = web_network_zones.list_page_data(db_session)["zone_geo_areas"]

    assert labels[str(bound.id)] == {
        "bound": True,
        "unavailable": False,
        "name": "Kano Coverage",
    }
    assert labels[str(child.id)]["bound"] is False
    assert labels[str(child.id)]["name"] is None
    assert labels[str(loose.id)]["name"] is None
    assert labels[str(loose.id)]["unavailable"] is False


def test_list_page_marks_stale_binding_unavailable(db_session):
    retired = _area(db_session, "Retired List Area")
    zone = web_network_zones.create_zone(
        db_session,
        web_network_zones.parse_form_values(
            _form(name="Stale List Zone", geo_area_id=str(retired.id), is_active="true")
        ),
    )
    retired.is_active = False
    db_session.flush()

    labels = web_network_zones.list_page_data(db_session)["zone_geo_areas"]

    assert labels[str(zone.id)]["unavailable"] is True
    assert labels[str(zone.id)]["name"] is None


def test_tickets_index_bulk_preview_guards_null_preview():
    source = Path("templates/admin/support/tickets/index.html").read_text(
        encoding="utf-8"
    )

    assert "${preview.skipped_count" not in source
    assert "${(preview?.skipped_count || 0) - 5}" in source
