from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.models.billing import LedgerEntryType, LedgerSource, PaymentStatus
from app.services.admin_workflow_guidance import guidance_for_path
from app.services.web_billing_ledger import render_ledger_csv
from app.services.web_billing_payments import build_create_payload


def _payload(payment_date: date, *, status: str = PaymentStatus.succeeded.value):
    return build_create_payload(
        account_id=uuid4(),
        collection_account_id=None,
        payment_method_id=None,
        amount=Decimal("125.00"),
        currency="NGN",
        status=status,
        reference="historical-receipt",
        memo="Confirmed bank receipt",
        invoice_id=None,
        payment_date=payment_date,
    )


def test_historical_payment_date_is_recorded_in_utc() -> None:
    payload = _payload(date(2026, 1, 2))
    assert payload.paid_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert payload.status is PaymentStatus.succeeded
    assert payload.amount == Decimal("125.00")


def test_pending_payment_does_not_claim_a_receipt_timestamp() -> None:
    payload = _payload(date(2026, 1, 2), status=PaymentStatus.pending.value)
    assert payload.paid_at is None
    assert payload.status is PaymentStatus.pending


def test_future_receipt_date_is_rejected() -> None:
    with pytest.raises(ValueError, match="Payment date cannot be in the future"):
        _payload(datetime.now(UTC).date() + timedelta(days=2))


@pytest.mark.parametrize("payment_date", [None, date(2026, 1, 2)])
def test_payment_preview_forwards_the_same_date_as_confirmation(
    monkeypatch, payment_date: date | None
) -> None:
    from app.web import admin
    from app.web.admin import billing_payments as routes

    preview = Mock(return_value={"preview": SimpleNamespace(payment_preview=object())})
    render = Mock(return_value=object())
    monkeypatch.setattr(
        routes.web_billing_payments_service, "preview_payment_create", preview
    )
    monkeypatch.setattr(routes.templates, "TemplateResponse", render)
    monkeypatch.setattr(admin, "get_current_user", lambda request: None)
    monkeypatch.setattr(admin, "get_sidebar_stats", lambda db: {})
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/billing/payments/create/preview",
            "headers": [],
        }
    )
    db = Mock()
    result = routes.payment_create_preview(
        request=request,
        account_id=str(uuid4()),
        amount="125.00",
        currency="NGN",
        status=PaymentStatus.succeeded.value,
        invoice_id=None,
        collection_account_id=None,
        payment_method_id=None,
        reference="historical-receipt",
        memo="Confirmed bank receipt",
        payment_date=payment_date,
        idempotency_token="historical-date-test",
        db=db,
    )
    assert result is render.return_value
    assert preview.call_args.kwargs["payment_date"] == payment_date
    assert render.call_args.args[1]["payment_date"] == (
        payment_date.isoformat() if payment_date else ""
    )
    db.rollback.assert_not_called()


@pytest.mark.parametrize("linked", [True, False])
def test_ledger_csv_preserves_linked_and_projected_invoice_references(
    linked: bool,
) -> None:
    number = 'INV,"historical"-01'
    entry = SimpleNamespace(
        id=uuid4(),
        account=SimpleNamespace(name="Example Customer"),
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        amount=Decimal("15.00"),
        currency="NGN",
        memo="Imported invoice",
        effective_date=datetime(2026, 1, 2, tzinfo=UTC),
        created_at=datetime(2026, 3, 15, tzinfo=UTC),
        invoice=SimpleNamespace(invoice_number=number) if linked else None,
        invoice_number="ignored-fallback" if linked else number,
    )
    rows = list(csv.DictReader(io.StringIO(render_ledger_csv([entry]))))
    assert len(rows) == 1
    assert rows[0]["invoice_number"] == number
    assert rows[0]["debit_amount"] == "15.00"
    assert rows[0]["credit_amount"] == ""
    assert rows[0]["date"] == "2026-01-02T00:00:00+00:00"


def test_billing_guidance_explains_historical_dates_and_invoice_exports() -> None:
    invoice = guidance_for_path("/admin/billing/invoices/new")
    payment = guidance_for_path("/admin/billing/payments/new")
    overview = guidance_for_path("/admin/billing")
    assert invoice is not None and payment is not None and overview is not None
    invoice_text = " ".join((*invoice.steps, *invoice.notes))
    payment_text = " ".join((*payment.steps, *payment.notes))
    assert "billing:invoice:update" in invoice_text
    assert "historical invoice date" in invoice_text
    assert "actual receipt date" in payment_text
    assert "same date in the preview" in payment_text
    assert "pending payment has no paid_at" in payment_text
    assert "invoice_number" in " ".join(overview.steps)
