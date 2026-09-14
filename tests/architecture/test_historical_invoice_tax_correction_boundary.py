from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER_PATH = ROOT / "app/services/historical_invoice_tax_corrections.py"
ADAPTER_PATH = ROOT / "scripts/billing/correct_historical_invoice_tax.py"


def test_historical_tax_correction_is_a_registered_typed_owner():
    owner = service_relationship("financial.historical_invoice_tax_corrections")

    assert owner.module == "app.services.historical_invoice_tax_corrections"
    assert owner.contract is not None
    contract = owner.contract
    assert contract.transaction.mode.value == "coordinator_managed"
    assert "execute_owner_command once" in contract.transaction.boundary
    assert contract.events is not None
    assert "invoice.tax_correction_completed" in contract.events.event_types


def test_owner_and_operator_keep_one_transaction_boundary():
    owner_source = OWNER_PATH.read_text(encoding="utf-8")
    adapter_source = ADAPTER_PATH.read_text(encoding="utf-8")

    assert owner_source.count("execute_owner_command(") == 1
    assert "db.commit(" not in owner_source
    assert "db.rollback(" not in owner_source
    assert "execute_owner_command(" not in adapter_source
    assert "Invoice(" not in adapter_source
    assert "InvoiceLine(" not in adapter_source
    assert ".commit(" not in adapter_source
    assert ".rollback(" not in adapter_source


def test_operator_is_preview_only_unless_apply_is_explicit():
    adapter_source = ADAPTER_PATH.read_text(encoding="utf-8")

    assert 'action="store_true"' in adapter_source
    assert "if not args.apply:" in adapter_source
    assert (
        "expected_preview_fingerprint=args.expected_preview_fingerprint"
        in adapter_source
    )
