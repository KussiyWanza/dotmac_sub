"""Protect vendor portfolio composition, visibility, and KPI parity."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_relationships import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
PORTFOLIO = APP / "services" / "vendor_delivery_portfolio.py"
DELIVERY = APP / "services" / "project_vendor_delivery.py"
SUPPLY = APP / "services" / "vendor_supply_views.py"
WEB_CONTEXT = APP / "services" / "web_vendors.py"
WEB_ROUTE = APP / "web" / "admin" / "vendors.py"
TEMPLATE = ROOT / "templates" / "admin" / "vendors" / "detail.html"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _calls(path: Path) -> list[str]:
    tree = ast.parse(_source(path), filename=str(path))
    return [
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    ]


def test_portfolio_has_a_complete_read_only_contract() -> None:
    service_names = {item.name for item in all_services()}
    portfolio = service_relationship("ui.vendor_delivery_portfolio_projection")
    supply = service_relationship("ui.vendor_supply_projection")

    assert portfolio.contract is not None
    assert portfolio.contract.transaction.mode is TransactionMode.READ_ONLY
    assert portfolio.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert "latest active vendor supply record selection" in supply.owns
    assert not contract_validation_errors(portfolio, service_names=service_names)
    assert not contract_validation_errors(supply, service_names=service_names)


def test_portfolio_is_a_read_only_composer_without_per_project_queries() -> None:
    calls = _calls(PORTFOLIO)
    source = _source(PORTFOLIO)

    for forbidden in (
        "commit",
        "rollback",
        "flush",
        "execute_owner_command",
        "get_project_vendor_delivery",
    ):
        assert forbidden not in calls
    assert "HTTPException" not in source
    assert "project_vendor_delivery_from_record" in calls
    assert "latest_material_releases_for_projects" in calls
    assert "latest_advances_for_projects" in calls
    assert "row_number" in _source(SUPPLY)


def test_vendor_adapter_supplies_capabilities_to_the_typed_projection() -> None:
    context_source = _source(WEB_CONTEXT)
    route_source = _source(WEB_ROUTE)

    assert "get_vendor_delivery_portfolio(" in context_source
    assert "ProjectVendorDeliveryVisibility(" in context_source
    assert "build_vendor_detail_context(" in route_source
    assert 'can(request, "inventory:read")' in route_source
    assert 'can(request, "network:fiber:read")' in route_source
    assert 'can(request, "finance:ap:read")' in route_source


def test_template_renders_projection_owned_statuses_and_exact_cohort_links() -> None:
    source = _source(TEMPLATE)

    assert 'id="delivery-portfolio"' in source
    assert "portfolio.kpis" in source
    assert "kpi.cohort_url" in source
    assert "portfolio.items" in source
    assert "status_presentation_badge(item.status," in source
    assert "item.latest_material_release" in source
    assert "item.latest_advance" in source
    assert "if item.status.value ==" not in source


def test_eager_record_composer_is_public_and_visibility_scoped() -> None:
    source = _source(DELIVERY)

    assert "class ProjectVendorDeliveryVisibility" in source
    assert "def project_vendor_delivery_from_record(" in source
    assert "visibility.has_visible_scope" in source
