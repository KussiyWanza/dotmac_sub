"""Pin generic customer and subscription edits outside lifecycle authority."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_relationships import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]
SUBSCRIBER_SCHEMA = ROOT / "app" / "schemas" / "subscriber.py"
SUBSCRIBER_SERVICE = ROOT / "app" / "services" / "subscriber.py"
CATALOG_SCHEMA = ROOT / "app" / "schemas" / "catalog.py"
CATALOG_SERVICE = ROOT / "app" / "services" / "catalog" / "subscriptions.py"
CUSTOMER_FORM = ROOT / "templates" / "admin" / "customers" / "form.html"


def _class_fields(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == class_name
    )
    return {
        item.target.id
        for item in node.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }


def _method_source(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    class_node = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == class_name
    )
    method = next(
        item
        for item in class_node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == method_name
    )
    assert method.end_lineno is not None
    return "\n".join(lines[method.lineno - 1 : method.end_lineno])


def test_account_status_owner_has_complete_confirmation_contract() -> None:
    service = service_relationship("customer.account_status_actions")
    names = {item.name for item in all_services()}

    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(service, service_names=names)
    concerns = {item.name: item for item in service.contract.concerns}
    assert (
        concerns["administrative account-status impact preview"].role
        is OwnerRole.RESOLVER
    )
    assert (
        concerns["administrative account-bound idempotent status confirmation"].role
        is OwnerRole.APPLICATION_COORDINATOR
    )


def test_generic_customer_update_has_no_lifecycle_fields_or_writer() -> None:
    fields = _class_fields(SUBSCRIBER_SCHEMA, "SubscriberUpdate")
    update = _method_source(SUBSCRIBER_SERVICE, "Subscribers", "update")
    form = CUSTOMER_FORM.read_text(encoding="utf-8")

    assert {"status", "is_active"}.isdisjoint(fields)
    assert "apply_requested_account_status(" not in update
    assert 'lifecycle_fields = {"status", "is_active"}' in update
    assert "Lifecycle status is changed only through" in form


def test_generic_subscription_patch_excludes_lifecycle_fields_and_catalog_path() -> (
    None
):
    fields = _class_fields(CATALOG_SCHEMA, "SubscriptionTechnicalUpdate")
    service = CATALOG_SERVICE.read_text(encoding="utf-8")

    assert {
        "status",
        "start_at",
        "end_at",
        "next_billing_at",
        "canceled_at",
        "cancel_reason",
    }.isdisjoint(fields)
    assert "catalog_update" not in service
    assert "_handle_status_transition_via_lifecycle" not in service
