"""Protect the CRM customer observation and legacy name-repair boundaries."""

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
OBSERVER = ROOT / "app" / "services" / "crm_customers.py"
ROUTE = ROOT / "app" / "api" / "crm_webhooks.py"
REPAIR_OWNER = ROOT / "app" / "services" / "customer_name_repairs.py"
REPAIR_ADAPTER = ROOT / "scripts" / "one_off" / "restore_crm_placeholder_identity.py"
LEGACY_PROFILE = ROOT / "app" / "services" / "web_customer_actions.py"
PROVISIONING_OWNER = ROOT / "app" / "services" / "crm_subscriber_provisioning.py"
CRM_API = ROOT / "app" / "api" / "crm.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_crm_customer_observer_is_read_only_and_provenance_exact() -> None:
    source = _source(OBSERVER)
    for forbidden in (
        "SubscriberCreate",
        "SubscriberUpdate",
        "HTTPException",
        ".commit(",
        ".rollback(",
        ".flush(",
        "db.add(",
        "setattr(",
        "normalize_email_identifier",
        "normalize_phone_identifier",
    ):
        assert forbidden not in source
    assert "def observe_customer(" in source
    assert "Subscriber.metadata_[key].as_string()" in source
    assert '"crm_project_id"' not in source


def test_crm_customer_route_cannot_create_or_update_an_account() -> None:
    source = _source(ROUTE)
    route = source.split('@router.post("/customers")', 1)[1].split(
        '@router.post("")', 1
    )[0]
    assert "CRMCustomerObservation.from_payload(payload)" in route
    assert "observe_customer(db, observation)" in route
    for forbidden in (
        "upsert_customer_from_payload",
        "Subscriber(",
        "SubscriberCreate",
        "SubscriberUpdate",
        ".commit(",
    ):
        assert forbidden not in route


def test_explicit_crm_provisioning_is_separate_from_observation_webhook() -> None:
    observer = _source(OBSERVER)
    webhook = _source(ROUTE)
    owner = _source(PROVISIONING_OWNER)
    api = _source(CRM_API)

    assert "crm_subscriber_provisioning" not in observer
    assert "crm_subscriber_provisioning" not in webhook
    assert "prepare_new_account(" in owner
    assert owner.count("execute_owner_command(") == 1
    assert '@router.post(\n    "/subscribers",' in api
    assert "Depends(require_crm_service_auth)" in api
    assert "ProvisionCRMSubscriberCommand(" in api
    for forbidden in (
        "fastapi",
        "HTTPException",
        ".commit(",
        ".rollback(",
        "begin_nested",
    ):
        assert forbidden not in owner


def test_customer_name_repair_owner_has_complete_contract() -> None:
    service = service_relationship("customer.name_repairs")
    service_names = {item.name for item in all_services()}

    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(service, service_names=service_names)
    concern = service.contract.concerns[0]
    assert concern.role is OwnerRole.COMMAND_WRITER
    assert concern.canonical_writer == "customer.name_repairs"


def test_customer_name_repair_has_one_atomic_owner_boundary() -> None:
    source = _source(REPAIR_OWNER)
    tree = ast.parse(source, filename=str(REPAIR_OWNER))
    calls = [
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    ]

    assert calls.count("execute_owner_command") == 1
    for forbidden in (
        "fastapi",
        "HTTPException",
        ".commit(",
        ".rollback(",
        "begin_nested",
        "except Exception",
        "setattr(",
    ):
        assert forbidden not in source
    assert ".with_for_update()" in source
    assert "audit_events.stage(" in source
    assert "emit_event(" in source
    assert "rebuild_identity_index_for_subscriber(" in source


def test_repair_cli_is_a_thin_adapter_and_old_helper_is_retired() -> None:
    adapter = _source(REPAIR_ADAPTER)
    legacy = _source(LEGACY_PROFILE)

    assert "RepairCustomerNamesCommand(" in adapter
    assert "repair_customer_names(" in adapter
    assert "with SessionLocal() as planner_db:" in adapter
    assert "with SessionLocal() as command_db:" in adapter
    for forbidden in (
        "db.commit(",
        "db.rollback(",
        "AuditEvent(",
        "Subscriber(",
        "apply_approved_customer_name_repairs",
    ):
        assert forbidden not in adapter
    assert "ApprovedCustomerNameRepair" not in legacy
    assert "apply_approved_customer_name_repairs" not in legacy
