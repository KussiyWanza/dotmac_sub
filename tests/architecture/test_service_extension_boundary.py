"""Guard the canonical service-extension interval and transaction boundary."""

from pathlib import Path

from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_relationships import service_relationship

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app/services/service_extensions.py"
COVERAGE = PROJECT_ROOT / "app/services/prepaid_service_coverage.py"
RECONCILIATION = PROJECT_ROOT / "app/services/prepaid_coverage_reconciliation.py"
MIGRATION = PROJECT_ROOT / "alembic/versions/417_service_extension_grant_intervals.py"


def test_service_extension_owner_contract_is_complete() -> None:
    service = service_relationship("financial.service_extensions")

    assert service.module == "app.services.service_extensions"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    concerns = {item.name: item for item in service.contract.concerns}
    assert (
        concerns["service-extension lifecycle and exact grant intervals"].role
        is OwnerRole.COMMAND_WRITER
    )
    assert (
        concerns["service-extension billing-anchor projection"].canonical_writer
        == "financial.service_extensions"
    )


def test_service_extension_public_writes_use_one_owner_boundary_each() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert source.count("execute_owner_command(") == 4
    assert "db.commit(" not in source
    assert "db.rollback(" not in source
    assert ".begin_nested(" not in source
    assert "reconcile_service_extension_duplicates" in source
    assert "preview_service_extension_duplicate_reconciliation" in source


def test_all_grant_consumers_use_the_exact_recorded_interval() -> None:
    owner = OWNER.read_text(encoding="utf-8")
    coverage = COVERAGE.read_text(encoding="utf-8")
    reconciliation = RECONCILIATION.read_text(encoding="utf-8")

    for source in (owner, coverage, reconciliation):
        assert "ServiceExtensionEntry.grant_starts_at" in source
        assert "ServiceExtensionEntry.grant_ends_at" in source
    assert "_shield_window_end" not in owner
    assert "ServiceExtensionEntry.previous_next_billing_at <=" not in coverage
    assert "ServiceExtensionEntry.previous_next_billing_at <=" not in reconciliation


def test_historical_migration_preserves_the_recorded_legacy_interval() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "416_binary_device_operational_lifecycle"' in source
    assert "grant_starts_at = previous_next_billing_at" in source
    assert "grant_ends_at = new_next_billing_at" in source
    assert "anchor_basis = 'legacy_previous_anchor'" in source
    assert "previous_next_billing_at + " not in source
