"""Paystack refund/dispute vocabulary: a real refund or lost chargeback must
actually reverse the payment, an unmatched one must not silently succeed, and
a refund/dispute receipt must never collide with its original charge's
`charge.success` receipt identity.

Companion to `tests/test_payment_webhook_settlement.py` (settlement) --- this
file is scoped to the NEW Paystack refund/dispute event families only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    Payment,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderType,
    PaymentRefund,
    PaymentReversal,
    PaymentStatus,
)
from app.models.integration_platform import IntegrationInbox
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services.api_billing_webhooks import process_paystack_webhook
from tests.integration_platform_helpers import enable_payment_provider

_SECRET = b"sk_test_webhook_secret"


@pytest.fixture(autouse=True)
def _payment_capabilities(db_session, monkeypatch):
    monkeypatch.setenv("PAYSTACK_TEST_SECRET", _SECRET.decode())
    monkeypatch.setenv("PAYSTACK_TEST_PUBLIC", "pk_test_webhook")
    enable_payment_provider(db_session, "paystack")


def _make_provider(db):
    provider = PaymentProvider(
        name="Paystack", provider_type=PaymentProviderType.paystack
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def _make_invoice(db, account_id, *, amount: str, invoice_number: str):
    from datetime import UTC, datetime, timedelta

    issued_at = datetime.now(UTC)
    return billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=account_id,
            invoice_number=invoice_number,
            currency="NGN",
            subtotal=Decimal(amount),
            total=Decimal(amount),
            balance_due=Decimal(amount),
            status=InvoiceStatus.issued,
            issued_at=issued_at,
            due_at=issued_at + timedelta(days=30),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:paystack-refund-dispute",
            due_date_policy_version="test-v1",
        ),
    )


def _post_paystack(db, body: bytes):
    signature = hmac.new(_SECRET, body, hashlib.sha512).hexdigest()
    return process_paystack_webhook(db=db, body=body, signature=signature)


def _charge_success_body(
    *, reference: str, tx_id: str, amount_kobo: int, metadata: dict
) -> bytes:
    return json.dumps(
        {
            "event": "charge.success",
            "data": {
                "id": tx_id,
                "reference": reference,
                "amount": amount_kobo,
                "fees": 0,
                "currency": "NGN",
                "status": "success",
                "metadata": metadata,
            },
        }
    ).encode()


def _refund_body(
    *,
    event: str,
    refund_id: str,
    amount_kobo: int,
    tx_id: str | None = None,
    tx_reference: str | None = None,
) -> bytes:
    data: dict = {
        "id": refund_id,
        "amount": amount_kobo,
        "currency": "NGN",
        "status": event.rsplit(".", 1)[-1],
    }
    if tx_id or tx_reference:
        transaction: dict = {}
        if tx_id:
            transaction["id"] = tx_id
        if tx_reference:
            transaction["reference"] = tx_reference
        data["transaction"] = transaction
    return json.dumps({"event": event, "data": data}).encode()


def _dispute_body(
    *,
    event: str,
    dispute_id: str,
    resolution: str | None = None,
    refund_amount_kobo: int | None = None,
    tx_id: str | None = None,
    tx_reference: str | None = None,
) -> bytes:
    data: dict = {"id": dispute_id, "currency": "NGN"}
    if resolution is not None:
        data["resolution"] = resolution
    if refund_amount_kobo is not None:
        data["refund_amount"] = refund_amount_kobo
    if tx_id or tx_reference:
        transaction: dict = {}
        if tx_id:
            transaction["id"] = tx_id
        if tx_reference:
            transaction["reference"] = tx_reference
        data["transaction"] = transaction
    return json.dumps({"event": event, "data": data}).encode()


def _settle_charge(db, subscriber, *, invoice, reference, tx_id, amount_kobo):
    body = _charge_success_body(
        reference=reference,
        tx_id=tx_id,
        amount_kobo=amount_kobo,
        metadata={"invoice_id": str(invoice.id)},
    )
    response = _post_paystack(db, body)
    assert response.status_code == 200
    return db.query(Payment).filter_by(external_id=tx_id).one()


def test_refund_processed_reverses_payment_end_to_end(db_session, subscriber):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="5000.00", invoice_number="INV-REFUND-1"
    )
    payment = _settle_charge(
        db_session,
        subscriber,
        invoice=invoice,
        reference="DMAC-REFUND-1",
        tx_id="800001",
        amount_kobo=500000,
    )
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid

    refund_body = _refund_body(
        event="refund.processed",
        refund_id="rfnd_800001",
        amount_kobo=500000,
        tx_id="800001",
        tx_reference="DMAC-REFUND-1",
    )
    response = _post_paystack(db_session, refund_body)

    assert response.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.refunded
    assert payment.refunded_amount == Decimal("5000.00")
    allocations = payment.allocations
    assert allocations and all(not a.is_active for a in allocations)
    db_session.refresh(invoice)
    assert invoice.status != InvoiceStatus.paid
    assert invoice.balance_due == Decimal("5000.00")
    refund = db_session.query(PaymentRefund).filter_by(payment_id=payment.id).one()
    assert refund.provider_event_id is not None


def test_refund_processed_partial_refund(db_session, subscriber):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="5000.00", invoice_number="INV-REFUND-2"
    )
    payment = _settle_charge(
        db_session,
        subscriber,
        invoice=invoice,
        reference="DMAC-REFUND-2",
        tx_id="800002",
        amount_kobo=500000,
    )

    refund_body = _refund_body(
        event="refund.processed",
        refund_id="rfnd_800002",
        amount_kobo=200000,
        tx_id="800002",
        tx_reference="DMAC-REFUND-2",
    )
    response = _post_paystack(db_session, refund_body)

    assert response.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.partially_refunded
    assert payment.refunded_amount == Decimal("2000.00")
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("2000.00")


def test_unmatched_refund_event_does_not_return_200(db_session):
    """Defect C + PR1's terminal-state guard working together: the resolution
    ladder correctly fails to find a payment, and PR1's guard
    (`_expected_financial_effect_unresolved`, merged in #3017) stops that from
    silently returning 200 with an empty consequence."""
    _make_provider(db_session)
    body = _refund_body(
        event="refund.processed",
        refund_id="rfnd_no_match",
        amount_kobo=100000,
        tx_id="no-such-transaction",
        tx_reference="DMAC-NO-SUCH-REFERENCE",
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 500
    assert (
        db_session.query(PaymentProviderEvent)
        .filter_by(external_id="rfnd_no_match")
        .count()
        == 0
    )


@pytest.mark.parametrize(
    "event", ["refund.pending", "refund.processing", "refund.failed"]
)
def test_refund_non_terminal_events_do_not_reverse_anything(
    db_session, subscriber, event
):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session,
        subscriber.id,
        amount="1000.00",
        invoice_number=f"INV-{event.replace('.', '-')}",
    )
    payment = _settle_charge(
        db_session,
        subscriber,
        invoice=invoice,
        reference=f"DMAC-{event}",
        tx_id=f"tx-{event}",
        amount_kobo=100000,
    )

    body = _refund_body(
        event=event,
        refund_id=f"rfnd-{event}",
        amount_kobo=100000,
        tx_id=f"tx-{event}",
        tx_reference=f"DMAC-{event}",
    )
    response = _post_paystack(db_session, body)

    assert response.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.succeeded
    assert payment.refunded_amount == Decimal("0.00")
    receipt = (
        db_session.query(IntegrationInbox)
        .filter_by(provider_event_id=f"paystack-{event}-rfnd-{event}")
        .one()
    )
    assert receipt.state == "processed"
    assert receipt.event_type == event


def test_dispute_create_is_informational_and_does_not_reverse(db_session, subscriber):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="1000.00", invoice_number="INV-DISPUTE-CREATE"
    )
    payment = _settle_charge(
        db_session,
        subscriber,
        invoice=invoice,
        reference="DMAC-DISPUTE-CREATE",
        tx_id="800010",
        amount_kobo=100000,
    )

    body = _dispute_body(
        event="charge.dispute.create",
        dispute_id="dp_800010",
        tx_id="800010",
        tx_reference="DMAC-DISPUTE-CREATE",
    )
    response = _post_paystack(db_session, body)

    assert response.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.succeeded


def test_dispute_resolve_merchant_won_is_informational(db_session, subscriber):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="1000.00", invoice_number="INV-DISPUTE-WON"
    )
    payment = _settle_charge(
        db_session,
        subscriber,
        invoice=invoice,
        reference="DMAC-DISPUTE-WON",
        tx_id="800011",
        amount_kobo=100000,
    )

    body = _dispute_body(
        event="charge.dispute.resolve",
        dispute_id="dp_800011",
        resolution="declined",
        tx_id="800011",
        tx_reference="DMAC-DISPUTE-WON",
    )
    response = _post_paystack(db_session, body)

    assert response.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.succeeded
    assert (
        db_session.query(PaymentReversal).filter_by(payment_id=payment.id).count() == 0
    )


def test_dispute_resolve_merchant_lost_routes_to_reversals_not_refunds(
    db_session, subscriber
):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="1000.00", invoice_number="INV-DISPUTE-LOST"
    )
    payment = _settle_charge(
        db_session,
        subscriber,
        invoice=invoice,
        reference="DMAC-DISPUTE-LOST",
        tx_id="800012",
        amount_kobo=100000,
    )

    body = _dispute_body(
        event="charge.dispute.resolve",
        dispute_id="dp_800012",
        resolution="merchant-accepted",
        refund_amount_kobo=100000,
        tx_id="800012",
        tx_reference="DMAC-DISPUTE-LOST",
    )
    response = _post_paystack(db_session, body)

    assert response.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.reversed
    reversal = db_session.query(PaymentReversal).filter_by(payment_id=payment.id).one()
    assert reversal.amount == Decimal("1000.00")
    assert db_session.query(PaymentRefund).filter_by(payment_id=payment.id).count() == 0


def test_dispute_resolve_unrecognized_outcome_is_rejected_loudly(
    db_session, subscriber
):
    """An unmapped resolution value must never be silently guessed either way."""
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session,
        subscriber.id,
        amount="1000.00",
        invoice_number="INV-DISPUTE-UNKNOWN",
    )
    payment = _settle_charge(
        db_session,
        subscriber,
        invoice=invoice,
        reference="DMAC-DISPUTE-UNKNOWN",
        tx_id="800013",
        amount_kobo=100000,
    )

    body = _dispute_body(
        event="charge.dispute.resolve",
        dispute_id="dp_800013",
        resolution="some-future-paystack-value",
        refund_amount_kobo=100000,
        tx_id="800013",
        tx_reference="DMAC-DISPUTE-UNKNOWN",
    )
    response = _post_paystack(db_session, body)

    assert response.status_code in (400, 409)
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.succeeded


def test_refund_delivered_twice_produces_exactly_one_refund(db_session, subscriber):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="1000.00", invoice_number="INV-IDEMPOTENT"
    )
    payment = _settle_charge(
        db_session,
        subscriber,
        invoice=invoice,
        reference="DMAC-IDEMPOTENT",
        tx_id="800020",
        amount_kobo=100000,
    )

    body = _refund_body(
        event="refund.processed",
        refund_id="rfnd_800020",
        amount_kobo=100000,
        tx_id="800020",
        tx_reference="DMAC-IDEMPOTENT",
    )

    first = _post_paystack(db_session, body)
    second = _post_paystack(db_session, body)

    assert first.status_code == 200
    assert second.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.refunded
    assert payment.refunded_amount == Decimal("1000.00")
    assert db_session.query(PaymentRefund).filter_by(payment_id=payment.id).count() == 1


def test_refund_receipt_identity_does_not_collide_with_original_charge_receipt(
    db_session, subscriber
):
    """Defect D: the single most important test in this change.

    A refund payload that shares its `data.id` with the ORIGINAL charge's
    `reference` must not be treated as a tampered duplicate of the
    `charge.success` receipt -- that would quarantine the whole installation
    and disable all inbound Paystack payment processing fleet-wide.
    """
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="1000.00", invoice_number="INV-COLLISION"
    )
    shared_value = "DMAC-COLLIDE-1"
    payment = _settle_charge(
        db_session,
        subscriber,
        invoice=invoice,
        reference=shared_value,
        tx_id="800030",
        amount_kobo=100000,
    )

    # The refund's OWN id is deliberately set to the exact same string as the
    # original charge's `reference`. Before the event-scoped identity fix,
    # both webhooks would resolve to the identical `provider_event_id`
    # (`paystack-DMAC-COLLIDE-1`) with different payload bytes -- a tampered-
    # duplicate collision.
    refund_body = _refund_body(
        event="refund.processed",
        refund_id=shared_value,
        amount_kobo=100000,
        tx_id="800030",
        tx_reference=shared_value,
    )

    with patch("app.services.integrations.inbox.quarantine_installation") as quarantine:
        response = _post_paystack(db_session, refund_body)

    quarantine.assert_not_called()
    assert response.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.refunded

    original_receipt = (
        db_session.query(IntegrationInbox)
        .filter_by(provider_event_id=f"paystack-{shared_value}")
        .one()
    )
    refund_receipt = (
        db_session.query(IntegrationInbox)
        .filter_by(provider_event_id=f"paystack-refund.processed-{shared_value}")
        .one()
    )
    assert original_receipt.id != refund_receipt.id
