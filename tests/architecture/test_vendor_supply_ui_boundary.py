"""Protect vendor supply UI ownership and adapter boundaries."""

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
PROJECTION = APP / "services" / "vendor_supply_views.py"
CONFIRMATION = APP / "services" / "vendor_supply_review_proposals.py"
API = APP / "api" / "vendor_portal.py"
WEB = APP / "web" / "vendor_portal.py"
ADMIN = APP / "web" / "admin" / "vendor_operations.py"


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


def test_supply_projection_and_confirmation_have_complete_contracts() -> None:
    service_names = {item.name for item in all_services()}
    projection = service_relationship("ui.vendor_supply_projection")
    confirmation = service_relationship("operations.vendor_supply_review_confirmation")

    assert projection.contract is not None
    assert confirmation.contract is not None
    assert projection.contract.transaction.mode is TransactionMode.READ_ONLY
    assert confirmation.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert projection.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert confirmation.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(projection, service_names=service_names)
    assert not contract_validation_errors(confirmation, service_names=service_names)


def test_projection_is_read_only_and_confirmation_owns_one_root_transaction() -> None:
    projection = _source(PROJECTION)
    confirmation = _source(CONFIRMATION)

    for forbidden in (".commit(", ".rollback(", "begin_nested", "HTTPException"):
        assert forbidden not in projection
        assert forbidden not in confirmation
    assert "execute_owner_command" not in projection
    assert _calls(CONFIRMATION).count("execute_owner_command") == 1
    assert ".with_for_update(" in projection


def test_supply_adapters_do_not_call_participant_committed_wrappers() -> None:
    combined = "\n".join(_source(path) for path in (API, WEB, ADMIN))

    for forbidden in (
        "request_release_committed(",
        "request_advance_committed(",
        "approve_committed(",
        "reject_committed(",
        ".commit(",
        ".rollback(",
    ):
        assert forbidden not in combined
    assert "RequestVendorMaterialReleaseCommand(" in combined
    assert "RequestVendorAdvanceCommand(" in combined
    assert "vendor_supply_review_proposals.issue_review(" in combined
    assert "ConfirmVendorSupplyReviewCommand(" in combined
