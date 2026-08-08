"""Focused contracts for Invoice discounts and append-only history."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.billing import (
    Invoice,
    InvoiceDiscountAction,
    InvoiceDiscountHistory,
    InvoiceDiscountHistoryImmutableError,
    InvoiceDiscountSource,
    InvoiceDiscountType,
    InvoiceStatus,
    TaxRate,
)
from app.models.event_store import EventStore
from app.models.sales import Quote
from app.models.system_user import SystemUser
from app.schemas.billing import InvoiceCreate, InvoiceLineCreate, InvoiceUpdate
from app.services import billing as billing_service
from app.services import invoice_discounts, invoice_draft_authoring, quote_deposits
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.timezone import APP_TIMEZONE


def _actor(db_session, label: str = "Invoice Sales") -> SystemUser:
    actor = SystemUser(
        first_name=label.split()[0],
        last_name=label.split()[-1],
        display_name=label,
        email=f"invoice-discount-{uuid4().hex}@example.com",
        is_active=True,
    )
    db_session.add(actor)
    db_session.commit()
    return actor


def _context(key: str) -> CommandContext:
    return CommandContext.system(
        actor="invoice-discount-test",
        scope="invoice_draft:test",
        reason="Invoice discount regression test",
        idempotency_key=key,
    )


def _create_discounted_draft(db_session, subscriber, actor) -> Invoice:
    tax = TaxRate(name=f"VAT-{uuid4().hex}", rate=Decimal("7.50"), is_active=True)
    db_session.add(tax)
    db_session.commit()
    command = invoice_draft_authoring.CreateInvoiceDraftCommand(
        account_id=subscriber.id,
        invoice_number=None,
        currency="NGN",
        issued_at=None,
        due_at=None,
        memo="Discounted draft",
        is_proforma=False,
        lines=(
            invoice_draft_authoring.DraftLineCommand(
                description="Installation",
                quantity=Decimal("1"),
                unit_price=Decimal("1000.00"),
                tax_rate_id=tax.id,
            ),
        ),
        discount=invoice_discounts.InvoiceDiscountInput(
            discount_type=InvoiceDiscountType.percentage,
            value=Decimal("10"),
            reason="Commercial approval",
        ),
        actor_system_user_id=actor.id,
    )
    db_session_adapter.release_read_transaction(db_session)
    result = invoice_draft_authoring.create_invoice_draft(
        db_session, command, context=_context(f"create-{uuid4()}")
    )
    return db_session.get(Invoice, result.invoice_id)


def test_percentage_discount_recalculates_tax_and_saves_history(
    db_session, subscriber
) -> None:
    actor = _actor(db_session)
    invoice = _create_discounted_draft(db_session, subscriber, actor)

    assert invoice.subtotal == Decimal("1000.00")
    assert invoice.discount_amount == Decimal("100.00")
    assert invoice.discounted_subtotal == Decimal("900.00")
    assert invoice.tax_total == Decimal("67.50")
    assert invoice.total == Decimal("967.50")
    assert invoice.balance_due == Decimal("967.50")
    assert invoice.discount_applied_by_system_user_id == actor.id
    history = db_session.query(InvoiceDiscountHistory).one()
    assert history.action == InvoiceDiscountAction.applied.value
    assert history.original_subtotal == Decimal("1000.00")
    assert history.total_after_discount == Decimal("967.50")
    discount_event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "invoice.discount_applied")
        .one()
    )
    assert discount_event.invoice_id == invoice.id
    assert discount_event.payload["revision"] == 1
    assert discount_event.payload["total"] == "967.50"

    issued = billing_service.invoices.update(
        db_session,
        str(invoice.id),
        InvoiceUpdate(status=InvoiceStatus.issued),
    )
    assert issued.status == InvoiceStatus.issued
    assert issued.total == Decimal("967.50")


def test_fixed_discount_cannot_exceed_subtotal() -> None:
    with pytest.raises(invoice_discounts.InvoiceDiscountError) as exc:
        invoice_discounts.resolve_invoice_discount(
            Decimal("100.00"),
            invoice_discounts.InvoiceDiscountInput(
                discount_type=InvoiceDiscountType.fixed_amount,
                value=Decimal("100.01"),
            ),
        )
    assert exc.value.code == "financial.invoice_discounts.exceeds_subtotal"


def test_quote_deposit_inheritance_preserves_the_payable_amount() -> None:
    quote = Quote(
        status="sent",
        currency="NGN",
        subtotal=Decimal("1000.00"),
        discount_type="percentage",
        discount_value=Decimal("10.00"),
        discount_amount=Decimal("100.00"),
        tax_total=Decimal("67.50"),
        total=Decimal("967.50"),
        discount_applied_by_system_user_id=uuid4(),
        is_active=True,
    )
    amounts = quote_deposits._quote_deposit_invoice_amounts(quote, Decimal("483.75"))
    assert amounts.original_subtotal == Decimal("500.00")
    assert amounts.discount is not None
    assert amounts.discount.value == Decimal("10.00")
    resolved = invoice_discounts.resolve_invoice_discount(
        amounts.original_subtotal, amounts.discount
    )
    assert resolved is not None
    assert resolved.amount == Decimal("50.00")
    assert round(
        amounts.original_subtotal - resolved.amount + amounts.tax_total, 2
    ) == Decimal("483.75")


def test_quote_deposit_invoice_stores_inherited_discount_without_changing_due(
    db_session, subscriber
) -> None:
    actor = _actor(db_session)
    quote = Quote(
        subscriber_id=subscriber.id,
        status="sent",
        currency="NGN",
        subtotal=Decimal("1000.00"),
        discount_type="percentage",
        discount_value=Decimal("10.00"),
        discount_amount=Decimal("100.00"),
        discount_applied_by_system_user_id=actor.id,
        discount_applied_at=datetime.now(UTC),
        discount_revision=1,
        tax_total=Decimal("67.50"),
        total=Decimal("967.50"),
        is_active=True,
    )
    db_session.add(quote)
    db_session.flush()
    deposit = Decimal("483.75")
    amounts = quote_deposits._quote_deposit_invoice_amounts(quote, deposit)
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,
            currency="NGN",
            subtotal=deposit,
            total=deposit,
            balance_due=deposit,
        ),
        commit=False,
    )
    quote_deposits._stage_inherited_quote_discount(
        db_session,
        quote=quote,
        invoice=invoice,
        amounts=amounts,
    )
    db_session.commit()

    assert invoice.subtotal == Decimal("500.00")
    assert invoice.discount_amount == Decimal("50.00")
    assert invoice.tax_total == Decimal("33.75")
    assert invoice.total == deposit
    assert invoice.balance_due == deposit
    assert invoice.discount_source == InvoiceDiscountSource.quote.value
    history = db_session.query(InvoiceDiscountHistory).one()
    assert history.action == InvoiceDiscountAction.inherited.value
    assert history.source_quote_id == quote.id


def test_removal_keeps_history_and_history_is_immutable(db_session, subscriber) -> None:
    actor = _actor(db_session)
    invoice = _create_discounted_draft(db_session, subscriber, actor)
    line = next(item for item in invoice.lines if item.is_active)
    command = invoice_draft_authoring.UpdateInvoiceDraftCommand(
        invoice_id=invoice.id,
        account_id=invoice.account_id,
        invoice_number=invoice.invoice_number,
        currency=invoice.currency,
        issued_at=invoice.issued_at,
        due_at=invoice.due_at,
        memo=invoice.memo,
        is_proforma=invoice.is_proforma,
        lines=(
            invoice_draft_authoring.DraftLineCommand(
                line_id=line.id,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                tax_rate_id=line.tax_rate_id,
            ),
        ),
        actor_system_user_id=actor.id,
        discount=None,
    )
    db_session_adapter.release_read_transaction(db_session)
    invoice_draft_authoring.update_invoice_draft(
        db_session, command, context=_context(f"remove-{uuid4()}")
    )
    db_session.refresh(invoice)

    assert invoice.discount_type is None
    assert invoice.discount_amount == Decimal("0.00")
    assert invoice.total == Decimal("1075.00")
    history = (
        db_session.query(InvoiceDiscountHistory)
        .order_by(InvoiceDiscountHistory.revision)
        .all()
    )
    assert [item.action for item in history] == ["applied", "removed"]
    history[0].reason = "rewrite"
    with pytest.raises(InvoiceDiscountHistoryImmutableError):
        db_session.flush()
    db_session.rollback()


def test_direct_line_change_is_blocked_while_discount_is_active(
    db_session, subscriber
) -> None:
    actor = _actor(db_session)
    invoice = _create_discounted_draft(db_session, subscriber, actor)

    with pytest.raises(HTTPException) as exc:
        billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Bypass line",
                quantity=Decimal("1"),
                unit_price=Decimal("10.00"),
            ),
        )
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as totals_exc:
        billing_service.invoices.update(
            db_session,
            str(invoice.id),
            InvoiceUpdate(subtotal=Decimal("2000.00")),
        )
    assert totals_exc.value.status_code == 409


def test_quote_inherited_discount_is_locked_against_second_discount(
    db_session, subscriber
) -> None:
    actor = _actor(db_session)
    quote = Quote(
        subscriber_id=subscriber.id,
        status="sent",
        currency="NGN",
        subtotal=Decimal("1000.00"),
        total=Decimal("900.00"),
        is_active=True,
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("500.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("500.00"),
        balance_due=Decimal("500.00"),
    )
    db_session.add_all([quote, invoice])
    db_session.flush()
    inherited = invoice_discounts.StageInvoiceDiscountCommand(
        invoice_id=invoice.id,
        actor_system_user_id=actor.id,
        command_id=uuid4(),
        discount=invoice_discounts.InvoiceDiscountInput(
            discount_type=InvoiceDiscountType.percentage,
            value=Decimal("10"),
        ),
        source=InvoiceDiscountSource.quote,
        source_quote_id=quote.id,
    )
    invoice_discounts.stage_invoice_discount(db_session, invoice, inherited)
    db_session.commit()

    with pytest.raises(invoice_discounts.InvoiceDiscountError) as exc:
        invoice_discounts.stage_invoice_discount(
            db_session,
            invoice,
            replace(
                inherited,
                command_id=uuid4(),
                source=InvoiceDiscountSource.manual,
                source_quote_id=None,
            ),
        )
    assert exc.value.code == "financial.invoice_discounts.inherited_locked"


def test_history_query_filters_customer_actor_type_and_status(
    db_session, subscriber
) -> None:
    actor = _actor(db_session, "Ada Finance")
    invoice = _create_discounted_draft(db_session, subscriber, actor)
    discount_applied_at = invoice.discount_applied_at
    assert discount_applied_at is not None
    if discount_applied_at.tzinfo is None:
        discount_applied_at = discount_applied_at.replace(tzinfo=UTC)
    local_applied_date = discount_applied_at.astimezone(APP_TIMEZONE).date()

    result = invoice_discounts.list_invoice_discount_history(
        db_session,
        invoice_discounts.InvoiceDiscountHistoryQuery(
            date_from=local_applied_date,
            date_to=local_applied_date,
            salesperson_id=actor.id,
            discount_type=InvoiceDiscountType.percentage,
            invoice_status=InvoiceStatus.draft,
        ),
    )
    assert result.total_count == 1
    assert result.items[0].invoice_id == invoice.id
    assert result.items[0].actor_name == "Ada Finance"
    applied_at = result.items[0].applied_at
    if applied_at.tzinfo is None:
        applied_at = applied_at.replace(tzinfo=UTC)
    assert applied_at <= datetime.now(UTC)
