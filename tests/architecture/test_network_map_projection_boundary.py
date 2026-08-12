"""Architecture guard for the comprehensive network-map read projection."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

from app.services import network_map
from app.services.network_map_contracts import (
    NetworkMapPlantProjection,
    NetworkMapProjection,
)
from app.services.sot_registry.registry import service_relationship

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_network_map_projection_has_registered_typed_owner():
    service = service_relationship("ui.network_map_projection")

    assert service.module == "app.services.network_map"
    assert service.contract is not None
    assert "network.radius_sessions" in service.depends_on
    assert "ui.status_presentation" in service.depends_on
    assert get_type_hints(network_map.build_network_map_projection)["return"] is (
        NetworkMapProjection
    )


def test_network_map_owner_consumes_radius_resolver_not_accounting_rows():
    source = inspect.getsource(network_map)

    assert "subscription_session_snapshots" in source
    assert "RadiusAccountingSession" not in source
    assert "build_network_map_context" not in source


def test_network_map_adapter_serializes_the_typed_projection():
    route_source = (PROJECT_ROOT / "app/web/admin/network.py").read_text(
        encoding="utf-8"
    )

    assert "build_network_map_projection(db=db)" in route_source
    assert "projection.to_template_context()" in route_source


def test_network_map_template_does_not_derive_customer_session_semantics():
    template = (PROJECT_ROOT / "templates/admin/network/map.html").read_text(
        encoding="utf-8"
    )

    assert "p.connectivity.presentation" in template
    assert "p.connectivity.layer" in template
    assert "p.is_online" not in template
    assert "Customer (Online)" not in template
    assert "Customer (Offline)" not in template


def test_plant_projection_is_typed_and_does_not_enter_customer_session_paths():
    source = inspect.getsource(network_map.build_network_map_plant_projection)

    assert (
        get_type_hints(network_map.build_network_map_plant_projection)["return"]
        is NetworkMapPlantProjection
    )
    assert "subscription_session_snapshots" not in source
    assert "OntUnit" not in source
    assert "build_network_map_projection" not in source
    assert "db.get(OLTDevice" not in source
    assert "func.count(Splitter.id)" in source
    assert "func.count(FiberSplice.id)" in source
    assert "func.count(FiberSpliceTray.id)" in source


def test_playwright_database_setup_uses_disposable_postgres_guard():
    source = (PROJECT_ROOT / "tests/playwright/conftest.py").read_text(encoding="utf-8")

    assert "parse_test_database_target(_e2e_database_url)" in source
    assert "_create_engine(_e2e_database_target.url)" in source
    assert "_create_engine(_e2e_database_url)" not in source
