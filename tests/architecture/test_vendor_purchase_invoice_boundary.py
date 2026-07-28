"""Protect vendor purchase-invoice coordination and record ownership."""

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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"
OWNER = APP_ROOT / "services" / "vendor_purchase_invoices.py"
RECORDS = APP_ROOT / "services" / "vendor_purchase_invoice_records.py"
FILE_STORAGE = APP_ROOT / "services" / "file_storage.py"
API = APP_ROOT / "api" / "vendor_portal.py"
WEB = APP_ROOT / "web" / "vendor_portal.py"
ADMIN = APP_ROOT / "web" / "admin" / "vendor_operations.py"
FIELD_MANAGER = APP_ROOT / "api" / "field" / "manager.py"
SUBMISSIONS = APP_ROOT / "services" / "vendor_submission_proposals.py"
SCHEMA = APP_ROOT / "schemas" / "vendor_purchase_invoice.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.AST:
    return ast.parse(_source(path), filename=str(path))


def test_invoice_coordinator_and_record_owner_have_complete_contracts() -> None:
    service_names = {item.name for item in all_services()}
    coordinator = service_relationship("operations.vendor_purchase_invoices")
    records = service_relationship("operations.vendor_purchase_invoice_records")

    assert coordinator.contract is not None
    assert records.contract is not None
    assert coordinator.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert records.contract.transaction.mode is TransactionMode.PARTICIPANT
    assert coordinator.module != records.module
    assert coordinator.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert records.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(coordinator, service_names=service_names)
    assert not contract_validation_errors(records, service_names=service_names)
    coordinator_concerns = {item.name: item for item in coordinator.contract.concerns}
    record_concerns = {item.name: item for item in records.contract.concerns}
    assert (
        coordinator_concerns["vendor purchase-invoice mutation coordination"].role
        is OwnerRole.APPLICATION_COORDINATOR
    )
    assert (
        record_concerns["vendor purchase-invoice lifecycle"].role
        is OwnerRole.COMMAND_WRITER
    )


def test_invoice_coordinator_has_one_transport_neutral_transaction_boundary() -> None:
    source = _source(OWNER)
    calls = [
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(_tree(OWNER))
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    ]

    assert calls.count("execute_owner_command") == 1
    for forbidden in (
        "fastapi",
        "HTTPException",
        "IntegrityError",
        ".commit(",
        ".rollback(",
        "begin_nested",
        "commit: bool",
        "setattr(",
        "emit_event(",
    ):
        assert forbidden not in source
    assert ".with_for_update(" in source


def test_invoice_record_owner_only_locks_flushes_and_stages_events() -> None:
    source = _source(RECORDS)

    for forbidden in (
        "fastapi",
        "HTTPException",
        "IntegrityError",
        ".commit(",
        ".rollback(",
        "begin_nested",
        "execute_owner_command",
        "commit: bool",
        "setattr(",
    ):
        assert forbidden not in source
    assert ".with_for_update(" in source
    assert ".flush(" in source
    assert "emit_event(" in source
    assert '"schema_version": 1' in source
    # The approval's committed output drives the payables export through
    # the receipted consumer; the record owner never enqueues directly.
    assert "EventType.vendor_purchase_invoice_approved" in source
    assert "enqueue_purchase_invoice" not in source
    assert "except Exception" not in source


def test_attachment_participant_never_completes_or_predeletes_transaction() -> None:
    records = _source(RECORDS)
    storage = _source(FILE_STORAGE)

    assert "file_uploads.stage_upload(" in records
    assert "file_uploads.stage_soft_delete(" in records
    assert "file_uploads.upload(" not in records
    assert "file_uploads.soft_delete(" not in records
    stage_upload = storage.split("    def stage_upload(", 1)[1].split(
        "    def get_active_entity_file(", 1
    )[0]
    stage_delete = storage.split("    def stage_soft_delete(", 1)[1].split(
        "\n\nfile_uploads =", 1
    )[0]
    assert ".commit(" not in stage_upload
    assert ".rollback(" not in stage_upload
    assert ".commit(" not in stage_delete
    assert ".delete(" not in stage_delete


def test_invoice_currency_uses_the_canonical_setting() -> None:
    owner = _source(OWNER)
    schema = _source(SCHEMA)

    assert "resolve_value(" in owner
    assert 'SettingDomain.billing, "default_currency"' in owner
    assert 'default="NGN"' not in schema


def test_public_adapters_construct_typed_invoice_commands_on_clean_sessions() -> None:
    combined = "\n".join(_source(path) for path in (API, WEB, ADMIN, FIELD_MANAGER))

    for command in (
        "CreateVendorPurchaseInvoiceCommand(",
        "UpdateVendorPurchaseInvoiceCommand(",
        "AddVendorPurchaseInvoiceLineCommand(",
        "UpdateVendorPurchaseInvoiceLineCommand(",
        "DeleteVendorPurchaseInvoiceLineCommand(",
        "UploadVendorPurchaseInvoiceAttachmentCommand(",
        "ReviewVendorPurchaseInvoiceCommand(",
    ):
        assert command in combined
    assert combined.count("release_read_transaction(db)") >= 20
    assert ".commit(" not in combined
    assert ".rollback(" not in combined


def test_invoice_submission_has_only_the_signed_confirmation_writer() -> None:
    api = _source(API)
    submissions = _source(SUBMISSIONS)
    callers: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        if path == OWNER:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if name == "stage_submission":
                callers.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert callers == {"app/services/vendor_submission_proposals.py"}
    assert "issue_purchase_invoice_submission(" in api
    assert "StageVendorPurchaseInvoiceSubmission(" in submissions
    assert "vendor_purchase_invoices.submit(" not in api
