from pathlib import Path


def test_prepaid_recovery_billing_uses_exact_payment_and_restoration_owners():
    source = Path("app/services/prepaid_recovery_billing.py").read_text()

    assert "settle_single_invoice_from_credit(db, invoice, only_if_full=True)" in source
    assert "restore_account_services(" in source


def test_bill_now_never_voids_an_existing_invoice():
    source = Path("app/services/prepaid_recovery_billing.py").read_text()

    assert "open_recovery_invoice" in source
    assert "Invoices.void" not in source
