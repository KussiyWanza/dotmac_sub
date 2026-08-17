from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import InvoiceDueDateBasis
from app.schemas.billing import InvoiceCreate, InvoiceLineCreate, InvoiceLineUpdate
from app.schemas.settings import DomainSettingUpdate
from app.services import billing as billing_service
from app.services import settings_api


def test_invoice_line_recalculates_totals(db_session, subscriber_account):
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
        ),
    )
    line = billing_service.invoice_lines.create(
        db_session,
        InvoiceLineCreate(
            invoice_id=invoice.id,
            description="Service fee",
            quantity=Decimal("2"),
            unit_price=Decimal("10.00"),
        ),
    )
    db_session.refresh(invoice)
    assert line.amount == Decimal("20.00")
    assert invoice.subtotal == Decimal("20.00")
    assert invoice.total == Decimal("20.00")
    assert invoice.balance_due == Decimal("20.00")


def test_invoice_line_update_recalculates(db_session, subscriber_account):
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
        ),
    )
    line = billing_service.invoice_lines.create(
        db_session,
        InvoiceLineCreate(
            invoice_id=invoice.id,
            description="Service fee",
            quantity=Decimal("1"),
            unit_price=Decimal("15.00"),
        ),
    )
    updated = billing_service.invoice_lines.update(
        db_session,
        str(line.id),
        InvoiceLineUpdate(quantity=Decimal("3")),
    )
    db_session.refresh(invoice)
    assert updated.amount == Decimal("45.00")
    assert invoice.subtotal == Decimal("45.00")


def test_invoice_defaults_use_settings(db_session, subscriber_account):
    settings_api.upsert_billing_setting(
        db_session, "default_currency", DomainSettingUpdate(value_text="EUR")
    )
    settings_api.upsert_billing_setting(
        db_session, "default_invoice_status", DomainSettingUpdate(value_text="issued")
    )
    issued_at = datetime.now(UTC)
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
            issued_at=issued_at,
            due_at=issued_at + timedelta(days=30),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:default-invoice-status",
            due_date_policy_version="test-v1",
        ),
    )
    assert invoice.currency == "EUR"
    assert invoice.status.value == "issued"
