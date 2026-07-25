"""Architecture guards for exact-service IPAM ownership migration."""

from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
)
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/ip_assignment_repair.py"
SCRIPT = ROOT / "scripts/one_off/repair_ipam_to_served.py"


def test_ipam_ownership_owner_has_complete_transaction_contract() -> None:
    service = service_relationship("network.ip_assignment_service_ownership")

    assert service.module == "app.services.ip_assignment_repair"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.SHADOWING
    assert service.contract.concerns[0].role is OwnerRole.RECONCILER


def test_legacy_projection_authority_and_partial_commit_paths_are_retired() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "backfill_create" not in source
    assert "reclaim_stale" not in source
    assert "dedupe_active" not in source
    assert "db.commit()" not in source
    assert "db.rollback()" not in source
    assert "execute_owner_command(" in source


def test_operator_adapter_is_dry_run_first_and_fingerprint_gated() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"--apply"' in source
    assert '"--fingerprint"' in source
    assert '"--idempotency-key"' in source
    assert "reconcile_ip_assignment_service_ownership(" in source
    assert ".commit(" not in source
