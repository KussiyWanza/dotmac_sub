from pathlib import Path


def test_prepaid_recovery_billing_has_no_parallel_settlement_writer():
    source = Path("app/services/prepaid_recovery_billing.py").read_text()

    assert "settle_prepaid_recovery_invoice" not in source
    assert "preview_prepaid_recovery_settlement" not in source
    assert "settle_single_invoice_from_credit" not in source
    assert "restore_account_services(" not in source


def test_bill_now_never_voids_an_existing_invoice():
    source = Path("app/services/prepaid_recovery_billing.py").read_text()

    assert "open_recovery_invoice" in source
    assert "Invoices.void" not in source
