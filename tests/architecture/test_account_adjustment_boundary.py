"""Architecture guards for the contracted account-adjustment owner."""

from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_relationships import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/billing/adjustments.py"


def _source(path: str | Path) -> str:
    resolved = path if isinstance(path, Path) else ROOT / path
    return resolved.read_text(encoding="utf-8")


def test_account_adjustment_owner_has_a_complete_command_contract() -> None:
    service = service_relationship("financial.account_adjustments")
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(
        service,
        service_names={item.name for item in all_services()},
    )
    concerns = {item.name: item for item in service.contract.concerns}
    assert concerns["prepaid account-debit eligibility and preview"].role is (
        OwnerRole.POLICY
    )
    assert concerns["locked account-debit confirmation"].role is (
        OwnerRole.COMMAND_WRITER
    )
    assert concerns["exact account-adjustment ledger links"].canonical_writer == (
        "financial.account_adjustments"
    )
    assert concerns["previewed account-adjustment reversal evidence"].role is (
        OwnerRole.COMMAND_WRITER
    )


def test_account_adjustment_owner_is_transport_and_transaction_neutral() -> None:
    source = _source(OWNER)
    for forbidden in (
        "fastapi",
        "HTTPException",
        "os.getenv",
        ".commit(",
        ".rollback(",
        "begin_nested",
        "LedgerEntry(",
        '"NGN"',
    ):
        assert forbidden not in source
    assert source.count("execute_owner_command(") == 2


def test_account_adjustment_commands_and_queries_are_typed() -> None:
    source = _source(OWNER)
    for contract in (
        "PreviewAccountAdjustmentQuery",
        "ConfirmAccountAdjustmentCommand",
        "StageSystemAccountAdjustmentCommand",
        "PreviewAccountAdjustmentReversalQuery",
        "ReverseAccountAdjustmentCommand",
    ):
        assert f"class {contract}:" in source
    for legacy in (
        "class AccountAdjustments",
        "def confirm_system(",
        "def confirm_reversal(",
        "commit: bool",
    ):
        assert legacy not in source


def test_api_is_a_thin_account_adjustment_adapter() -> None:
    source = _source("app/api/billing.py")
    assert "PreviewAccountAdjustmentQuery(" in source
    assert "ConfirmAccountAdjustmentCommand(" in source
    assert "PreviewAccountAdjustmentReversalQuery(" in source
    assert "ReverseAccountAdjustmentCommand(" in source
    assert source.count("db_session_adapter.release_read_transaction(db)") >= 2
    account_adjustment_section = source[
        source.index('"/account-adjustments/preview"') : source.index(
            '"/ledger-entries"', source.index('"/account-adjustments/preview"')
        )
    ]
    assert ".commit(" not in account_adjustment_section
    assert ".rollback(" not in account_adjustment_section


def test_only_approved_coordinators_use_nested_adjustment_staging() -> None:
    direct_callers: set[str] = set()
    system_callers: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        if path == OWNER:
            continue
        source = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(ROOT))
        if "stage_account_adjustment(" in source:
            direct_callers.add(relative)
        if "stage_system_account_adjustment(" in source:
            system_callers.add(relative)
    assert direct_callers == {"app/services/customer_portal_flow_addons.py"}
    assert system_callers == {
        "app/services/prepaid_plan_changes.py",
    }


def test_ledger_and_database_own_the_monetary_invariants() -> None:
    owner = _source(OWNER)
    ledger = _source("app/services/billing/ledger.py")
    model = _source("app/models/billing.py")
    assert "LedgerEntries.stage_account_adjustment_debit(" in owner
    assert "LedgerEntries.stage_account_adjustment_reversal(" in owner
    assert "def stage_account_adjustment_debit(" in ledger
    assert "def stage_account_adjustment_reversal(" in ledger
    assert "uq_account_adjustments_origin_idempotency" in model
    assert "uq_account_adjustments_origin_reversal_idempotency" in model
    assert "ck_account_adjustments_amount_positive" in model
    assert "ledger_entry_id" in model
    assert "unique=True" in model


def test_adjustment_evidence_has_events_and_a_drift_signal() -> None:
    owner = _source(OWNER)
    events = _source("app/services/events/types.py")
    assert "def inspect_account_adjustment_evidence(" in owner
    assert '"default_currency"' in owner
    assert "settings_spec.resolve_value(" in owner
    assert "EventType.account_adjustment_confirmed" in owner
    assert "EventType.account_adjustment_reversed" in owner
    assert 'account_adjustment_confirmed = "account_adjustment.confirmed"' in events
    assert 'account_adjustment_reversed = "account_adjustment.reversed"' in events
