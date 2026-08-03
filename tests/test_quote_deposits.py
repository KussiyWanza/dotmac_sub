"""Quote deposit collection via the existing billing surface: initiate + verify.

``verify_deposit`` runs behind the Phase 3 ``quotes_native_write_enabled``
flag: OFF (default) write-through to the CRM via ``quotes_mirror.accept_quote``;
ON native accept via ``sales.selfserve``. Both paths are covered here."""

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
from app.models.quote_mirror import QuoteMirror
from app.models.sales import Quote, SalesOrder
from app.models.subscriber import Subscriber
from app.services import quote_deposits
from app.services.sales import quote_acceptance, selfserve


def _subscriber(db) -> Subscriber:
    party = Party(
        display_name="C R",
        party_type="person",
        status="active",
    )
    db.add(party)
    db.flush()
    sub = Subscriber(
        first_name="C",
        last_name="R",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Quote deposit fixture Party binding",
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
                name=f"Quote deposit {uuid.uuid4().hex[:8]}",
                project_type="fiber_optics_installation",
                is_active=True,
            )
        )
    db.commit()
    db.refresh(sub)
    return sub


def _quote(db, sub, *, deposit="37500.00", paid=False) -> QuoteMirror:
    row = QuoteMirror(
        crm_quote_id=f"q-{uuid.uuid4().hex[:8]}",
        subscriber_id=sub.id,
        status="draft",
        currency="NGN",
        total="75000.00",
        deposit_amount=deposit,
        deposit_paid=paid,
        payload={"id": "q1", "status": "draft", "deposit_amount": deposit},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _customer(sub):
    return {
        "account_id": str(sub.id),
        "subscriber_id": str(sub.id),
        "username": "c@example.com",
    }


def test_initiate_creates_invoice_and_returns_checkout(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub)
    fake_invoice = SimpleNamespace(id=uuid.uuid4(), metadata_=None)
    intent = {
        "provider_type": "paystack",
        "reference": "ref_1",
        "checkout_url": None,
        "currency": "NGN",
    }
    with (
        patch(
            "app.services.quote_deposits.billing_service.invoices.create",
            return_value=fake_invoice,
        ),
        patch(
            "app.services.quote_deposits.payments.create_invoice_payment_intent",
            return_value=intent,
        ) as intent_fn,
    ):
        out = quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id
        )
    assert out["amount"] == "37500.00"
    assert out["payment_reference"] == "ref_1"
    assert out["invoice_id"] == str(fake_invoice.id)
    intent_fn.assert_called_once()


def test_initiate_rejects_when_already_paid(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub, paid=True)
    with pytest.raises(HTTPException) as exc:
        quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id
        )
    assert exc.value.status_code == 409


def test_initiate_rejects_when_no_deposit(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub, deposit="0")
    with pytest.raises(HTTPException) as exc:
        quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id
        )
    assert exc.value.status_code == 400


def test_verify_paid_accepts_quote(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub)
    paid_invoice = SimpleNamespace(status=InvoiceStatus.paid)
    accepted = {"id": row.crm_quote_id, "status": "accepted", "deposit_paid": True}
    with (
        patch(
            "app.services.quote_deposits.payments.verify_and_record_payment",
            return_value={"invoice": paid_invoice, "amount": Decimal("37500.00")},
        ),
        patch(
            "app.services.quote_deposits.quotes_mirror.accept_quote",
            return_value=accepted,
        ) as accept_fn,
    ):
        out = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id, reference="ref_1"
        )
    assert out["paid"] is True
    assert out["quote"]["status"] == "accepted"
    accept_fn.assert_called_once()


def test_verify_unpaid_does_not_accept(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub)
    pending_invoice = SimpleNamespace(status=InvoiceStatus.issued)
    with (
        patch(
            "app.services.quote_deposits.payments.verify_and_record_payment",
            return_value={"invoice": pending_invoice, "amount": Decimal("0.00")},
        ),
        patch("app.services.quote_deposits.quotes_mirror.accept_quote") as accept_fn,
    ):
        out = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id, reference="ref_1"
        )
    assert out["paid"] is False
    accept_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 3 flip flag: quotes_native_write_enabled
# ---------------------------------------------------------------------------

_FAP = SimpleNamespace(id=uuid.uuid4(), name="NAP-041")


def _native_quote(db, sub):
    """A native draft quote created through the self-serve flow (75,000 total,
    37,500 deposit at the default 50%)."""
    with patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=(_FAP, 1300.0),
    ):
        return selfserve.selfserve_quotes.request_quote(
            db,
            str(sub.id),
            latitude=9.0765,
            longitude=7.3986,
            address="12 Mississippi St, Maitama",
        )


def _flag_on():
    return patch("app.services.quote_deposits._native_write_enabled", return_value=True)


def test_native_write_flag_defaults_off(db_session):
    # Spec default False — the CRM write-through stays the live path until
    # the coordinated Phase 3 write flip.
    assert quote_deposits._native_write_enabled(db_session) is False


def test_verify_default_flag_uses_crm_write_through(db_session):
    """Flag OFF: acceptance still goes through quotes_mirror.accept_quote
    (the CRM portal accept) — the native service is not touched."""
    sub = _subscriber(db_session)
    row = _quote(db_session, sub)
    paid_invoice = SimpleNamespace(status=InvoiceStatus.paid)
    accepted = {"id": row.crm_quote_id, "status": "accepted", "deposit_paid": True}
    with (
        patch(
            "app.services.quote_deposits.payments.verify_and_record_payment",
            return_value={"invoice": paid_invoice, "amount": Decimal("37500.00")},
        ),
        patch(
            "app.services.quote_deposits.quotes_mirror.accept_quote",
            return_value=accepted,
        ) as crm_accept,
        patch.object(
            selfserve.selfserve_quotes, "accept_with_deposit"
        ) as native_accept,
    ):
        out = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id, reference="ref_1"
        )
    assert out["paid"] is True
    crm_accept.assert_called_once()
    native_accept.assert_not_called()


def test_verify_native_flag_accepts_natively(db_session):
    """Flag ON: the accept tail runs natively — quote accepted in sub's own
    quotes table, sales order created and marked, no CRM hop."""
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    paid_invoice = SimpleNamespace(status=InvoiceStatus.paid)
    with (
        _flag_on(),
        patch(
            "app.services.quote_deposits.payments.verify_and_record_payment",
            return_value={"invoice": paid_invoice, "amount": Decimal("37500.00")},
        ),
        patch("app.services.quote_deposits.quotes_mirror.accept_quote") as crm_accept,
    ):
        out = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id), reference="ref_1"
        )

    crm_accept.assert_not_called()
    assert out["paid"] is True
    assert out["quote"]["status"] == "accepted"
    assert out["quote"]["deposit_paid"] is True
    assert out["quote"]["deposit_reference"] == "ref_1"

    db_session.refresh(quote)
    assert quote.status == "accepted"
    order = db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).one()
    assert order.deposit_paid is True
    assert order.amount_paid == Decimal("37500.00")
    assert order.payment_status == "partial"
    # Risk #2: exactly one ledger event per deposit — the (mocked) invoice
    # payment. The native accept itself records no payment row.
    assert db_session.query(Payment).count() == 0

    # Transitional mirror write-back keeps mirror reads + initiate_deposit's
    # already-paid check coherent until the PR 8 read flip.
    mirror = (
        db_session.query(QuoteMirror)
        .filter(QuoteMirror.crm_quote_id == str(quote.id))
        .one()
    )
    assert mirror.status == "accepted"
    assert mirror.deposit_paid is True


def test_verify_native_flag_unpaid_leaves_quote_draft(db_session):
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    pending_invoice = SimpleNamespace(status=InvoiceStatus.issued)
    with (
        _flag_on(),
        patch(
            "app.services.quote_deposits.payments.verify_and_record_payment",
            return_value={"invoice": pending_invoice, "amount": Decimal("0.00")},
        ),
    ):
        out = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id), reference="ref_1"
        )
    assert out["paid"] is False
    assert out["quote"]["status"] == "draft"
    db_session.refresh(quote)
    assert quote.status == "draft"
    assert (
        db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).count()
        == 0
    )


def test_verify_native_flag_retry_is_idempotent(db_session):
    """A verify retry after a successful native accept returns the same
    sales order and keeps the original deposit stamp."""
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    paid_invoice = SimpleNamespace(status=InvoiceStatus.paid)
    with (
        _flag_on(),
        patch(
            "app.services.quote_deposits.payments.verify_and_record_payment",
            return_value={"invoice": paid_invoice, "amount": Decimal("37500.00")},
        ),
    ):
        first = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id), reference="ref_1"
        )
        second = quote_deposits.verify_deposit(
            db_session,
            _customer(sub),
            str(sub.id),
            str(quote.id),
            reference="ref_1",
        )
    assert first["quote"]["already_accepted"] is False
    assert second["quote"]["already_accepted"] is True
    assert second["quote"]["sales_order_id"] == first["quote"]["sales_order_id"]
    assert second["quote"]["deposit_reference"] == "ref_1"
    assert (
        db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).count()
        == 1
    )


def test_changed_deposit_retry_cannot_overwrite_sales_order_money(db_session):
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    first = selfserve.selfserve_quotes.accept_with_deposit(
        db_session,
        str(sub.id),
        str(quote.id),
        deposit_reference="ref-original",
        deposit_amount=Decimal("37500.00"),
        provider="paystack",
    )
    order_id = uuid.UUID(first["sales_order_id"])

    with pytest.raises(quote_acceptance.QuoteAcceptanceError) as exc_info:
        selfserve.selfserve_quotes.accept_with_deposit(
            db_session,
            str(sub.id),
            str(quote.id),
            deposit_reference="ref-conflict",
            deposit_amount=Decimal("1.00"),
            provider="paystack",
        )

    assert exc_info.value.code == "sales.quote_acceptance.deposit_evidence_conflict"
    db_session.expire_all()
    stored_quote = db_session.get(Quote, quote.id)
    stored_order = db_session.get(SalesOrder, order_id)
    assert stored_quote.metadata_["deposit"] == {
        "reference": "ref-original",
        "amount": "37500.00",
        "provider": "paystack",
        "paid": True,
    }
    assert stored_order.amount_paid == Decimal("37500.00")
    assert stored_order.balance_due == Decimal("37500.00")
    assert (
        db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).count()
        == 1
    )


def test_verify_native_flag_is_subscriber_scoped(db_session):
    sub = _subscriber(db_session)
    other = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    with _flag_on():
        with pytest.raises(HTTPException) as exc:
            quote_deposits.verify_deposit(
                db_session,
                _customer(other),
                str(other.id),
                str(quote.id),
                reference="ref_1",
            )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Phase 3 flip flag: initiate_deposit native branch (§4.3 write flip)
# ---------------------------------------------------------------------------


def _paid_deposit_invoice(db, sub, quote_id) -> Invoice:
    """A settled deposit invoice in sub's ledger — the authoritative
    already-paid marker the native branch must key on."""
    inv = Invoice(
        account_id=sub.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("37500.00"),
        total=Decimal("37500.00"),
        balance_due=Decimal("0.00"),
        metadata_={"quote_id": str(quote_id), "payment_flow": "quote_deposit"},
    )
    db.add(inv)
    db.commit()
    return inv


def test_initiate_native_flag_creates_invoice_for_uuid_quote(db_session):
    """Flag ON: the quote resolves in sub's own table by UUID (not
    crm_quote_id) and the deposit invoice is raised off the native payload."""
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    fake_invoice = SimpleNamespace(id=uuid.uuid4(), metadata_=None)
    intent = {"provider_type": "paystack", "reference": "ref_n1", "currency": "NGN"}
    with (
        _flag_on(),
        patch(
            "app.services.quote_deposits.billing_service.invoices.create",
            return_value=fake_invoice,
        ),
        patch(
            "app.services.quote_deposits.payments.create_invoice_payment_intent",
            return_value=intent,
        ),
    ):
        out = quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id)
        )
    assert out["quote_id"] == str(quote.id)
    assert out["amount"] == "37500.00"
    assert out["invoice_id"] == str(fake_invoice.id)
    # The traceback metadata is exactly what _native_deposit_invoice_paid keys on.
    assert fake_invoice.metadata_ == {
        "quote_id": str(quote.id),
        "payment_flow": "quote_deposit",
    }


def test_initiate_native_flag_rejects_when_ledger_deposit_paid(db_session):
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    _paid_deposit_invoice(db_session, sub, quote.id)
    with _flag_on():
        with pytest.raises(HTTPException) as exc:
            quote_deposits.initiate_deposit(
                db_session, _customer(sub), str(sub.id), str(quote.id)
            )
    assert exc.value.status_code == 409


def test_initiate_native_ledger_authority_beats_stale_mirror(db_session):
    """Risk #2 regression: a stale mirror claiming the deposit is UNPAID must
    not permit a second charge — the ledger invoice is the sole authority on
    the native branch."""
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    _paid_deposit_invoice(db_session, sub, quote.id)
    stale = QuoteMirror(
        crm_quote_id=str(quote.id),
        subscriber_id=sub.id,
        status="draft",
        currency="NGN",
        total="75000.00",
        deposit_amount="37500.00",
        deposit_paid=False,
        payload={"id": str(quote.id), "status": "draft"},
    )
    db_session.add(stale)
    db_session.commit()
    with _flag_on():
        with pytest.raises(HTTPException) as exc:
            quote_deposits.initiate_deposit(
                db_session, _customer(sub), str(sub.id), str(quote.id)
            )
    assert exc.value.status_code == 409


def test_initiate_native_flag_is_subscriber_scoped(db_session):
    sub = _subscriber(db_session)
    other = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    with _flag_on():
        with pytest.raises(HTTPException) as exc:
            quote_deposits.initiate_deposit(
                db_session, _customer(other), str(other.id), str(quote.id)
            )
    assert exc.value.status_code == 404
