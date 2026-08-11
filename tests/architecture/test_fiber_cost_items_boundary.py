"""Keep fiber pricing behind its typed, atomic source-of-truth boundary."""

from dataclasses import is_dataclass
from pathlib import Path
from typing import get_type_hints

from app.models.fiber_cost_item import FiberCostUnit as PersistedFiberCostUnit
from app.schemas.fiber_cost_items import (
    CreateFiberCostItemCommand,
    FiberCostEstimate,
    FiberCostItemOutcome,
    FiberCostUnit,
    UpdateFiberCostItemCommand,
)
from app.services import fiber_cost_items
from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/fiber_cost_items.py"
ADAPTER = ROOT / "app/web/admin/network_fiber_costs.py"
CALCULATION_CONTRACT = ROOT / "app/schemas/fiber_cost_calculation.py"
COST_TEMPLATE = ROOT / "templates/admin/network/fiber/cost_items.html"
MAP_TEMPLATE = ROOT / "templates/admin/network/fiber/map.html"
WEB_READ_SERVICE = ROOT / "app/services/web_network_fiber.py"
MIGRATION = ROOT / "alembic/versions/519_fiber_cost_items.py"


def test_fiber_cost_writes_are_one_atomic_owner_command() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert source.count("execute_owner_command(") == 2
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "stage_audit_event(" in source
    assert "emit_event(" in source
    assert ".with_for_update()" in source
    assert "command.expected_version" in source
    assert '"before": _audit_values(before)' in source
    assert '"after": _audit_values(outcome)' in source


def test_fiber_cost_migration_seeds_the_required_version_explicitly() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert source.count("sort_order, version,") == 2
    assert "CAST(:sort_order AS integer), 1, NOW(), NOW()" in source
    assert ":sort_order, 1,\n                       CURRENT_TIMESTAMP" in source


def test_fiber_cost_manifest_declares_the_runtime_owner_boundary() -> None:
    service = service_relationship("network.fiber_cost_items")

    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    write_concern = next(
        concern
        for concern in service.contract.concerns
        if concern.name == "fiber drop-cost components and their prices"
    )
    assert write_concern.role is OwnerRole.COMMAND_WRITER
    assert write_concern.canonical_writer == "network.fiber_cost_items"
    assert "network.fiber_cost_items.stale_version" in (
        service.contract.errors.domain_codes
    )


def test_public_fiber_cost_contracts_are_frozen_and_typed() -> None:
    calculation_source = CALCULATION_CONTRACT.read_text(encoding="utf-8")

    assert "app.models" not in calculation_source
    assert "app.services" not in calculation_source
    assert PersistedFiberCostUnit is FiberCostUnit
    assert issubclass(FiberCostUnit, str)
    assert tuple(member.value for member in FiberCostUnit) == ("per_meter", "flat")
    for contract in (
        CreateFiberCostItemCommand,
        UpdateFiberCostItemCommand,
        FiberCostItemOutcome,
        FiberCostEstimate,
    ):
        assert is_dataclass(contract)
        assert contract.__dataclass_params__.frozen
        assert "Any" not in repr(contract.__annotations__)

    create_hints = get_type_hints(fiber_cost_items.create_item)
    update_hints = get_type_hints(fiber_cost_items.update_item)
    estimate_hints = get_type_hints(fiber_cost_items.estimate_for_distance)
    assert create_hints["command"] is CreateFiberCostItemCommand
    assert create_hints["return"] is FiberCostItemOutcome
    assert update_hints["command"] is UpdateFiberCostItemCommand
    assert update_hints["return"] is FiberCostItemOutcome
    assert estimate_hints["return"] is FiberCostEstimate


def test_web_adapter_only_maps_transport_and_permissions() -> None:
    source = ADAPTER.read_text(encoding="utf-8")

    assert "CreateFiberCostItemCommand(" in source
    assert "UpdateFiberCostItemCommand(" in source
    assert source.count("db_session_adapter.release_read_transaction(db)") == 2
    assert "record_audit_event" not in source
    assert ".commit(" not in source
    assert "expected_version: int = Form(...)" in source
    assert 'require_permission("network:fiber:read")' in source
    assert "fiber_cost_items_service.WRITE_SCOPE" in source


def test_templates_preserve_write_gating_and_route_estimate_identity() -> None:
    cost_source = COST_TEMPLATE.read_text(encoding="utf-8")
    map_source = MAP_TEMPLATE.read_text(encoding="utf-8")
    web_read_source = WEB_READ_SERVICE.read_text(encoding="utf-8")

    assert cost_source.count("{% if can_write %}") >= 4
    assert 'name="expected_version"' in cost_source
    assert web_read_source.count('"cost_estimate": serialize_cost_estimate(') == 2
    assert "cost_estimate: data.cost_estimate" in map_source
    assert "planRequestRevision" in map_source
    assert "planEstimate !== targetEstimate" in map_source
    assert "cost_status: 'loading'" in map_source
    assert "targetEstimate.cost_status = 'failed'" in map_source
