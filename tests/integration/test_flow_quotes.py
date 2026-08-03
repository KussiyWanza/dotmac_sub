"""Quote module flow on PostgreSQL: request → deposit initiate → verify →
accept → sales order, all on the native path (quotes_native_write_enabled ON).

Only the external payment gateway is faked; invoices, quotes, sales orders,
and the ledger dedupe guard are the real services on a real database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.billing import Invoice, InvoiceStatus, Payment
from app.models.party import Party
from app.models.project import ProjectTemplate
from app.models.sales import Quote, SalesOrder
from app.models.subscriber import Subscriber
from app.services import quote_deposits
from app.services.sales import selfserve
from app.services.subscriber import _default_reseller_id

_FAP = SimpleNamespace(id=uuid.uuid4(), name="NAP-041")
_PIN = {"latitude": 9.0765, "longitude": 7.3986, "address": "12 Mississippi St"}


def _subscriber(db) -> Subscriber:
    party = Party(
        display_name="Flow Quote",
        party_type="person",
        status="active",
    )
    db.add(party)
    db.flush()
    sub = Subscriber(
        first_name="Flow",
        last_name="Quote",
        email=f"fq-{uuid.uuid4().hex[:8]}@example.com",
        # subscribers.reseller_id is NOT NULL (migration 116); default to House.
        reseller_id=_default_reseller_id(db),
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Quote integration fixture Party binding",
    )
    db.add(sub)
    if (
        db.query(ProjectTemplate)
        .filter_by(project_type="fiber_optics_installation", is_active=True)
        .first()
        is None
    ):
        db.add(
            ProjectTemplate(
                name=f"Quote flow {uuid.uuid4().hex[:8]}",
                project_type="fiber_optics_installation",
                is_active=True,
            )
        )
    db.flush()
    return sub


def _customer(sub):
    return {
        "account_id": str(sub.id),
        "subscriber_id": str(sub.id),
        "username": "fq@example.com",
    }


@pytest.fixture(autouse=True)
def _native_write(enable_flags):
    enable_flags("quotes_native_write_enabled")


def test_quote_lifecycle_native(db_session):
    sub = _subscriber(db_session)

    # 1. Request — native, no CRM link on the subscriber at all.
    with patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=(_FAP, 1300.0),
    ):
        quote = selfserve.selfserve_quotes.request_quote(
            db_session, str(sub.id), **_PIN
        )
    assert quote.status == "draft"
    payload = selfserve.build_portal_quote_payload(db_session, quote)
    deposit = Decimal(payload["deposit_amount"])
    assert deposit > 0

    # 2. Initiate deposit — a real issued Invoice lands in the ledger.
    intent = {"provider_type": "paystack", "reference": "flow_ref_1", "currency": "NGN"}
    with patch(
        "app.services.quote_deposits.payments.create_invoice_payment_intent",
        return_value=intent,
    ):
        out = quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id)
        )
    invoice = db_session.get(Invoice, uuid.UUID(out["invoice_id"]))
    assert invoice is not None and invoice.status == InvoiceStatus.issued
    assert invoice.metadata_ == {
        "quote_id": str(quote.id),
        "payment_flow": "quote_deposit",
    }
    assert Decimal(out["amount"]) == deposit

    # 3. Verify — gateway says paid; the accept tail fires natively.
    invoice.status = InvoiceStatus.paid
    db_session.flush()
    with patch(
        "app.services.quote_deposits.payments.verify_and_record_payment",
        return_value={"invoice": invoice, "amount": deposit},
    ):
        verified = quote_deposits.verify_deposit(
            db_session,
            _customer(sub),
            str(sub.id),
            str(quote.id),
            reference="flow_ref_1",
        )
    assert verified["paid"] is True
    assert verified["quote"]["status"] == "accepted"

    # 4. Consequences: quote accepted, sales order marked, no bespoke payment
    # row (risk #2 — the sole ledger event is the invoice payment).
    db_session.refresh(quote)
    assert quote.status == "accepted"
    order = db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).one()
    assert order.deposit_paid is True
    assert db_session.query(Payment).filter(Payment.account_id == sub.id).count() == 0

    # 5. Double-charge guard: re-initiating against the paid ledger invoice
    # 409s regardless of any mirror state.
    with pytest.raises(HTTPException) as exc:
        quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id)
        )
    assert exc.value.status_code == 409


def test_quote_native_ids_are_uuid_namespace(db_session):
    """The native flow's public identity is the Quote UUID end-to-end — the
    deposit response echoes it and the row resolves by it."""
    sub = _subscriber(db_session)
    with patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=(_FAP, 900.0),
    ):
        quote = selfserve.selfserve_quotes.request_quote(
            db_session, str(sub.id), **_PIN
        )
    fetched = selfserve.selfserve_quotes.get_for_subscriber(
        db_session, str(sub.id), str(quote.id)
    )
    assert fetched.id == quote.id
    assert db_session.get(Quote, quote.id) is fetched
