"""Architecture guards for service-extension lifecycle and detail ownership."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_relationships import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/service_extensions.py"
PROJECTION = ROOT / "app/services/web_billing_service_extensions.py"
ROUTE = ROOT / "app/web/admin/billing_extensions.py"
TEMPLATE = ROOT / "templates/admin/billing/service_extension_detail.html"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_service_extension_owners_have_complete_registered_contracts() -> None:
    services = {service.name for service in all_services()}
    lifecycle = service_relationship("financial.service_extensions")
    detail = service_relationship("ui.service_extension_detail_projection")

    assert lifecycle.contract is not None
    assert detail.contract is not None
    assert lifecycle.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert detail.contract.transaction.mode is TransactionMode.READ_ONLY
    assert lifecycle.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert detail.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(lifecycle, service_names=services)
    assert not contract_validation_errors(detail, service_names=services)
    lifecycle_concerns = {
        concern.name: concern for concern in lifecycle.contract.concerns
    }
    assert (
        lifecycle_concerns["service-extension lifecycle and exact grant intervals"].role
        is OwnerRole.COMMAND_WRITER
    )
    assert (
        lifecycle_concerns[
            "immutable applied service-extension entry evidence"
        ].canonical_writer
        == "financial.service_extensions"
    )


def test_lifecycle_owner_uses_one_boundary_per_public_command() -> None:
    source = _source(OWNER)
    for command in (
        "CreateServiceExtensionCommand",
        "ApplyServiceExtensionCommand",
        "CancelServiceExtensionCommand",
        "CreateServiceExtensionOutcome",
        "ApplyServiceExtensionOutcome",
        "CancelServiceExtensionOutcome",
    ):
        assert f"class {command}:" in source
    # create, apply, cancel, anchor-projection repair, and duplicate-entry
    # reconciliation (the last landed on main via #1593).
    assert source.count("execute_owner_command(") == 5
    for forbidden in (
        "HTTPException",
        ".commit(",
        ".rollback(",
        "begin_nested",
        "defer_until_commit=True",
    ):
        assert forbidden not in source
    assert "stage_audit_event(" in source
    assert 'entity_type="service_extension"' in source
    assert "with_for_update()" in source
    # Two-person control: the approver must differ from the creator.
    assert "self_approval_forbidden" in source


def test_lifecycle_evidence_and_idempotency_are_database_enforced() -> None:
    owner = _source(OWNER)
    model = _source(ROOT / "app/models/service_extension.py")
    migration = _source(ROOT / "alembic/versions/421_service_extension_activity_sot.py")
    events = _source(ROOT / "app/services/events/types.py")

    assert "_EXTENSION_ID_NAMESPACE" in owner
    assert "_create_fingerprint(" in owner
    assert "pg_advisory_xact_lock" in owner
    assert "uq_service_extension_entries_extension_subscription" in model
    assert "uq_service_extension_entries_extension_subscription" in migration
    assert "canceled_by" in model
    assert "canceled_at" in model
    for action in ("created", "applied", "canceled"):
        assert f'billing.service_extension_{action}"' in events


def test_detail_route_is_a_thin_projection_and_command_adapter() -> None:
    source = _source(ROUTE)
    detail_section = source[
        source.index("def service_extension_detail(") : source.index(
            '@router.post(\n    "/service-extensions/{extension_id}/apply"'
        )
    ]
    assert "build_service_extension_detail(" in detail_section
    for forbidden in (
        "AuditEvent",
        "audit_adapter",
        ".query(",
        "select(",
        "has_permission(",
        "status_colors",
    ):
        assert forbidden not in detail_section
    assert "CreateServiceExtensionCommand(" in source
    assert "ApplyServiceExtensionCommand(" in source
    assert "CancelServiceExtensionCommand(" in source


def test_projection_owns_exact_history_actor_and_action_presentation() -> None:
    source = _source(PROJECTION)
    assert 'entity_type="service_extension"' in source
    assert "entity_id=str(extension.id)" in source
    assert "event.actor_label" in source
    assert "legacy_reconstructed" in source
    assert "has_permission(" in source
    assert "transition_eligibility(" in source
    assert 'metadata.get("path"' not in source
    assert "AuditEvent.actor_id ==" not in source


def test_template_only_renders_typed_status_activity_and_eligibility() -> None:
    source = _source(TEMPLATE)
    assert "detail.summary.status_presentation" in source
    assert "timeline_item(" in source
    assert "detail.can_apply" in source
    assert "detail.can_cancel" in source
    assert "Created by" in source
    assert "Created at" in source
    assert "status_colors" not in source
    assert "app_datetime" not in source
    assert "AuditEvent" not in source
    assert "View all" not in source


def test_service_extension_legacy_writer_debt_is_removed() -> None:
    writer_baseline = _source(ROOT / "tests/architecture/sot_writer_baseline.txt")
    legacy_manifest = _source(
        ROOT / "tests/architecture/sot_manifest_legacy_baseline.txt"
    )
    assert "app.services.service_extensions" not in writer_baseline
    assert "financial.service_extensions" not in legacy_manifest
    assert "ui.service_extension_detail_projection" not in legacy_manifest
