from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_cutover_adapter_uses_typed_owner_command_without_direct_writes():
    source = (
        ROOT / "scripts" / "procurement" / "cutover_purchase_orders.py"
    ).read_text(encoding="utf-8")

    assert "cut_over_purchase_order_origination" in source
    assert "owner_command_session" in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "SessionLocal" not in source
    assert "FieldErpSyncEvent(" not in source
    assert "SyncFlowOwnership(" not in source


def test_historical_po_staging_has_one_registered_owner_boundary():
    owner = (
        ROOT / "app" / "services" / "procurement_purchase_order_cutover.py"
    ).read_text(encoding="utf-8")
    registry = (
        ROOT
        / "app"
        / "services"
        / "sot_registry"
        / "domains"
        / "integration_control_plane.py"
    ).read_text(encoding="utf-8")

    concern = "Selfcare procurement ERP ownership cutover and reconciled PO backfill"
    assert owner.count("execute_owner_command(") == 1
    assert "outbox.enqueue(" in owner
    assert "isolate=False" in owner
    assert "FieldErpSyncFlow.purchase_order" in owner
    assert "FieldErpSyncFlow.purchase_invoice" in owner
    assert ".commit(" not in owner
    assert ".rollback(" not in owner
    assert concern in owner
    assert concern in registry
