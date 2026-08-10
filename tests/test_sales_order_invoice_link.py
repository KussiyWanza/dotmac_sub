"""Sale-to-money gains a structural link, not another metadata join.

`SalesOrder` financial status is what gates the whole sales-to-service
lifecycle, and settlement evidence could not be attributed to the order: the
only join was `Project.metadata_` string comparison, with no foreign key, no
uniqueness and no referential integrity.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from app.models.billing import Invoice, InvoiceStatus
from app.models.sales import SalesOrder, SalesOrderInvoiceLink
from app.services.sales_orders import link_sales_order_invoice


def _order(db_session, subscriber) -> SalesOrder:
    order = SalesOrder(
        subscriber_id=subscriber.id,
        order_number=f"SO-TEST-{uuid4().hex[:8]}",
        status="draft",
        payment_status="pending",
        subtotal=Decimal("10000.00"),
        tax_total=Decimal("750.00"),
        total=Decimal("10750.00"),
        amount_paid=Decimal("0.00"),
        balance_due=Decimal("10750.00"),
    )
    db_session.add(order)
    db_session.flush()
    return order


def _invoice(db_session, subscriber) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-TEST-{uuid4().hex[:8]}",
        status=InvoiceStatus.draft,
        subtotal=Decimal("10000.00"),
        total=Decimal("10750.00"),
        balance_due=Decimal("10750.00"),
    )
    db_session.add(invoice)
    db_session.flush()
    return invoice


def test_link_attributes_an_invoice_to_its_sales_order(db_session, subscriber):
    order = _order(db_session, subscriber)
    invoice = _invoice(db_session, subscriber)

    link = link_sales_order_invoice(
        db_session, sales_order_id=order.id, invoice_id=invoice.id
    )

    assert link is not None
    assert link.sales_order_id == order.id
    assert link.invoice_id == invoice.id
    assert link.account_id == subscriber.id
    assert link.purpose == "installation"
    assert link.origin == "native"


def test_linking_the_same_invoice_twice_is_a_no_op(db_session, subscriber):
    """Invoice attachment replays; it must not duplicate or re-provenance."""
    order = _order(db_session, subscriber)
    invoice = _invoice(db_session, subscriber)

    first = link_sales_order_invoice(
        db_session, sales_order_id=order.id, invoice_id=invoice.id
    )
    second = link_sales_order_invoice(
        db_session, sales_order_id=order.id, invoice_id=invoice.id
    )

    assert first is not None and second is not None
    assert first.id == second.id
    rows = (
        db_session.query(SalesOrderInvoiceLink)
        .filter(SalesOrderInvoiceLink.invoice_id == invoice.id)
        .all()
    )
    assert len(rows) == 1


def test_an_unresolvable_sales_order_is_left_for_review(db_session, subscriber):
    """A dangling id must not be forced through a RESTRICT foreign key."""
    invoice = _invoice(db_session, subscriber)

    assert (
        link_sales_order_invoice(
            db_session, sales_order_id=uuid4(), invoice_id=invoice.id
        )
        is None
    )
    assert db_session.query(SalesOrderInvoiceLink).count() == 0


def test_a_malformed_id_does_not_raise(db_session, subscriber):
    invoice = _invoice(db_session, subscriber)

    assert (
        link_sales_order_invoice(
            db_session, sales_order_id="not-a-uuid", invoice_id=invoice.id
        )
        is None
    )
