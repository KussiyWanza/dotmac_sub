"""Protect vendor project workspace coordination and record ownership."""

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
OWNER = APP_ROOT / "services" / "vendor_portal_operations.py"
RECORDS = APP_ROOT / "services" / "vendor_project_records.py"
API = APP_ROOT / "api" / "vendor_portal.py"
WEB = APP_ROOT / "web" / "vendor_portal.py"
ADMIN = APP_ROOT / "web" / "admin" / "vendor_operations.py"
FIELD_MANAGER = APP_ROOT / "api" / "field" / "manager.py"
SETTINGS = APP_ROOT / "services" / "settings_spec.py"
SCHEMA = APP_ROOT / "schemas" / "vendor_portal.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.AST:
    return ast.parse(_source(path), filename=str(path))


def test_workspace_and_record_owners_have_complete_contracts() -> None:
    service_names = {item.name for item in all_services()}
    workspace = service_relationship("operations.vendor_project_workspace")
    records = service_relationship("operations.vendor_project_records")

    assert workspace.contract is not None
    assert records.contract is not None
    assert workspace.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert records.contract.transaction.mode is TransactionMode.PARTICIPANT
    assert workspace.module != records.module
    assert workspace.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert records.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(workspace, service_names=service_names)
    assert not contract_validation_errors(records, service_names=service_names)
    workspace_concerns = {item.name: item for item in workspace.contract.concerns}
    record_concerns = {item.name: item for item in records.contract.concerns}
    assert (
        workspace_concerns["vendor project workspace mutation coordination"].role
        is OwnerRole.APPLICATION_COORDINATOR
    )
    assert (
        record_concerns["vendor installation-project quote lifecycle"].role
        is OwnerRole.COMMAND_WRITER
    )
    assert (
        record_concerns["staff proposed-route review state and immutable evidence"].role
        is OwnerRole.AUTHORITATIVE_RECORD
    )


def test_workspace_owner_has_one_transport_neutral_transaction_boundary() -> None:
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
        ".commit(",
        ".rollback(",
        "begin_nested",
        "commit: bool",
        "setattr(",
    ):
        assert forbidden not in source
    assert ".with_for_update(" in source
    assert "emit_event(" not in source


def test_record_owner_only_locks_flushes_and_stages_events() -> None:
    source = _source(RECORDS)

    for forbidden in (
        "fastapi",
        "HTTPException",
        ".commit(",
        ".rollback(",
        "begin_nested",
        "execute_owner_command",
        "commit: bool",
        "setattr(",
    ):
        assert forbidden not in source
    assert ".with_for_update(" in source
    assert "emit_event(" in source
    assert '"schema_version": 1' in source


def test_quote_currency_and_validity_have_canonical_setting_inputs() -> None:
    workspace = _source(OWNER)
    records = _source(RECORDS)
    settings = _source(SETTINGS)
    schema = _source(SCHEMA)

    assert "resolve_value(" in workspace
    assert 'SettingDomain.billing, "default_currency"' in workspace
    assert '"vendor_quote_validity_days"' in workspace
    assert 'key="vendor_quote_validity_days"' in settings
    assert "min_value=1" in settings
    assert "max_value=365" in settings
    assert "timedelta(days=30)" not in records
    assert 'default="NGN"' not in schema


def test_submission_record_participants_have_only_the_named_coordinator() -> None:
    callers: dict[str, set[str]] = {
        "stage_quote_submission": set(),
        "stage_as_built_submission": set(),
    }
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
            if name in callers:
                callers[name].add(path.relative_to(PROJECT_ROOT).as_posix())

    assert callers == {
        "stage_quote_submission": {"app/services/vendor_submission_proposals.py"},
        "stage_as_built_submission": {"app/services/vendor_submission_proposals.py"},
    }


def test_route_review_record_participant_has_only_the_workspace_caller() -> None:
    callers: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        if path == RECORDS:
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
            if name == "stage_route_revision_review":
                callers.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert callers == {"app/services/vendor_portal_operations.py"}


def test_public_adapters_construct_typed_workspace_commands_on_clean_sessions() -> None:
    api = _source(API)
    web = _source(WEB)
    admin = _source(ADMIN)
    field_manager = _source(FIELD_MANAGER)
    combined = "\n".join((api, web, admin, field_manager))

    for command in (
        "CreateVendorQuoteCommand(",
        "AddVendorQuoteLineCommand(",
        "UpdateVendorQuoteLineCommand(",
        "DeleteVendorQuoteLineCommand(",
        "ReviewVendorQuoteCommand(",
        "CreateVendorRouteRevisionCommand(",
        "SubmitVendorRouteRevisionCommand(",
    ):
        assert command in combined
    assert "CreateVendorRouteRevisionCommand(" in web
    assert "SubmitVendorRouteRevisionCommand(" in web
    assert '"/projects/{project_id}/quotes/{quote_id}/route-revisions"' in web
    assert '"/projects/{project_id}/route-revisions/{revision_id}/submit"' in web
    assert combined.count("release_read_transaction(db)") >= 11
    assert ".commit(" not in combined
    assert ".rollback(" not in combined


def test_vendor_api_uses_the_same_signed_submission_protocol() -> None:
    source = _source(API)

    assert "issue_quote_submission(" in source
    assert "issue_as_built_submission(" in source
    assert "ConfirmVendorSubmissionCommand(" in source
    assert "vendor_portal_operations.submit_quote(" not in source
    assert "vendor_portal_operations.submit_as_built(" not in source
