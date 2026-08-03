"""A quote with no line items must not be able to commit the business.

#1198 gave staff a quote form but no way to add line items, and its status
dropdown offered every status including ``accepted``. Accepting a Quote now
runs the atomic sales-conversion coordinator, so a Quote worth exactly nothing
must still be rejected before it can create a customer, order, or
implementation scope.

The invariant belongs to the sales service, not the form: web, API and importer
all mutate quotes through it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.party import Party
from app.models.project import ProjectTemplate
from app.models.sales import Lead, Quote, QuoteLineItem, QuoteStatus, SalesOrder
from app.schemas.sales import (
    LeadCreate,
    QuoteCreate,
    QuoteLineItemCreate,
    QuoteLineItemUpdate,
    QuoteUpdate,
)
from app.services import sales as sales_service
from app.services.sales import quote_acceptance


def _lead(db_session, subscriber) -> Lead:
    if subscriber.party_id is None:
        party = Party(
            display_name=f"{subscriber.first_name} {subscriber.last_name}",
            party_type="person",
            status="active",
        )
        db_session.add(party)
        db_session.flush()
        subscriber.party_id = party.id
        subscriber.party_bound_at = datetime.now(UTC)
        subscriber.party_binding_source = "pytest"
        subscriber.party_binding_reason = "Quote financial fixture Party binding"
        db_session.commit()
    return sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )


def _ensure_sales_template(db_session) -> None:
    if (
        db_session.query(ProjectTemplate)
        .filter_by(project_type="fiber_optics_installation", is_active=True)
        .first()
        is None
    ):
        db_session.add(
            ProjectTemplate(
                name="Financial safety installation",
                project_type="fiber_optics_installation",
                is_active=True,
            )
        )
        db_session.commit()


def _draft(db_session, subscriber) -> Quote:
    _ensure_sales_template(db_session)
    lead = _lead(db_session, subscriber)
    return sales_service.quotes.create(
        db_session,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
            status=QuoteStatus.draft,
        ),
    )


def _add_line(db_session, quote, *, unit_price="50000.00") -> QuoteLineItem:
    return sales_service.quote_line_items.create(
        db_session,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Fibre drop, 120m",
            quantity="1",
            unit_price=unit_price,
        ),
    )


def test_cannot_create_a_quote_that_is_already_accepted(db_session, subscriber):
    """The exact path #1198 opened: an accepted quote with no lines would have
    run the whole fulfilment pipeline for zero money."""
    lead = _lead(db_session, subscriber)
    with pytest.raises(ValueError, match="starts as a draft"):
        sales_service.quotes.create(
            db_session,
            QuoteCreate(
                subscriber_id=subscriber.id,
                lead_id=lead.id,
                project_type="fiber_optics_installation",
                status=QuoteStatus.accepted,
            ),
        )

    # Nothing was persisted, and no sales order was spawned.
    assert db_session.query(Quote).count() == 0
    assert db_session.query(SalesOrder).count() == 0


def test_cannot_create_a_quote_that_is_already_sent(db_session, subscriber):
    lead = _lead(db_session, subscriber)
    with pytest.raises(ValueError, match="starts as a draft"):
        sales_service.quotes.create(
            db_session,
            QuoteCreate(
                subscriber_id=subscriber.id,
                lead_id=lead.id,
                project_type="fiber_optics_installation",
                status=QuoteStatus.sent,
            ),
        )
    assert db_session.query(Quote).count() == 0


def test_cannot_accept_a_quote_with_no_line_items(db_session, subscriber):
    quote = _draft(db_session, subscriber)
    assert quote.total == 0

    with pytest.raises(
        quote_acceptance.QuoteAcceptanceError, match="at least one line item"
    ):
        sales_service.quotes.update(
            db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.accepted)
        )

    # The rejected transition left the quote exactly as it was -- not
    # half-applied -- and fired nothing downstream.
    db_session.refresh(quote)
    assert quote.status == QuoteStatus.draft.value
    assert db_session.query(SalesOrder).count() == 0


def test_cannot_send_a_quote_with_no_line_items(db_session, subscriber):
    quote = _draft(db_session, subscriber)

    with pytest.raises(ValueError, match="at least one line item"):
        sales_service.quotes.update(
            db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.sent)
        )

    db_session.refresh(quote)
    assert quote.status == QuoteStatus.draft.value


def test_a_quote_with_lines_can_still_be_sent_and_accepted(db_session, subscriber):
    """The guard must not break the legitimate path."""
    quote = _draft(db_session, subscriber)
    _add_line(db_session, quote)
    db_session.refresh(quote)
    assert quote.total > 0

    sales_service.quotes.update(
        db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.sent)
    )
    db_session.refresh(quote)
    assert quote.status == QuoteStatus.sent.value

    sales_service.quotes.update(
        db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.accepted)
    )
    db_session.refresh(quote)
    assert quote.status == QuoteStatus.accepted.value
    # The fulfilment pipeline ran -- for a quote that is actually worth money.
    assert db_session.query(SalesOrder).count() == 1


def test_removing_the_last_line_makes_the_quote_unsendable_again(
    db_session, subscriber
):
    """Deleting a line must re-derive the totals, not leave stale money behind —
    and a quote stripped back to nothing must fail the same guard as one that
    never had lines."""
    quote = _draft(db_session, subscriber)
    _add_line(db_session, quote)
    db_session.refresh(quote)
    assert quote.total > 0

    line = quote.line_items[0]
    sales_service.quote_line_items.delete(db_session, str(line.id))

    db_session.refresh(quote)
    assert quote.line_items == []
    assert quote.subtotal == 0
    assert quote.total == 0

    with pytest.raises(ValueError, match="at least one line item"):
        sales_service.quotes.update(
            db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.sent)
        )


def test_a_zero_priced_line_is_allowed(db_session, subscriber):
    """The guard is 'has lines', not 'total > 0'. A deliberately free install
    (promo, goodwill, warranty rework) is a real quote with real lines; refusing
    it would be a different bug."""
    quote = _draft(db_session, subscriber)
    _add_line(db_session, quote, unit_price="0.00")

    sales_service.quotes.update(
        db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.accepted)
    )

    db_session.refresh(quote)
    assert quote.status == QuoteStatus.accepted.value
    assert quote.total == 0


def test_accepted_quote_fields_and_deactivation_are_rejected(
    db_session,
    subscriber,
):
    quote = _draft(db_session, subscriber)
    _add_line(db_session, quote)
    sales_service.quotes.update(
        db_session,
        str(quote.id),
        QuoteUpdate(status=QuoteStatus.accepted),
    )
    order = db_session.query(SalesOrder).filter_by(quote_id=quote.id).one()
    accepted_total = quote.total

    with pytest.raises(quote_acceptance.QuoteAcceptanceError) as edit_error:
        sales_service.quotes.update(
            db_session,
            str(quote.id),
            QuoteUpdate(notes="Repriced after acceptance"),
        )
    assert edit_error.value.code == ("sales.quote_acceptance.accepted_quote_immutable")
    assert edit_error.value.details["attempted_mutation"] == "quote_fields"

    with pytest.raises(quote_acceptance.QuoteAcceptanceError) as delete_error:
        sales_service.quotes.delete(db_session, str(quote.id))
    assert delete_error.value.details["attempted_mutation"] == "quote_deactivation"

    db_session.expire_all()
    accepted = db_session.get(Quote, quote.id)
    assert accepted is not None
    assert accepted.notes is None
    assert accepted.is_active is True
    assert accepted.total == accepted_total
    assert db_session.get(SalesOrder, order.id).total == accepted_total


def test_accepted_quote_line_items_are_rejected_without_money_drift(
    db_session,
    subscriber,
):
    quote = _draft(db_session, subscriber)
    line = _add_line(db_session, quote)
    sales_service.quotes.update(
        db_session,
        str(quote.id),
        QuoteUpdate(status=QuoteStatus.accepted),
    )
    order = db_session.query(SalesOrder).filter_by(quote_id=quote.id).one()
    accepted_total = quote.total

    with pytest.raises(quote_acceptance.QuoteAcceptanceError) as create_error:
        sales_service.quote_line_items.create(
            db_session,
            QuoteLineItemCreate(
                quote_id=quote.id,
                description="Unapproved extra work",
                quantity=Decimal("1"),
                unit_price=Decimal("1.00"),
            ),
        )
    assert create_error.value.details["attempted_mutation"] == "line_item_create"

    with pytest.raises(quote_acceptance.QuoteAcceptanceError) as update_error:
        sales_service.quote_line_items.update(
            db_session,
            str(line.id),
            QuoteLineItemUpdate(unit_price=Decimal("1.00")),
        )
    assert update_error.value.details["attempted_mutation"] == "line_item_update"

    with pytest.raises(quote_acceptance.QuoteAcceptanceError) as delete_error:
        sales_service.quote_line_items.delete(db_session, str(line.id))
    assert delete_error.value.details["attempted_mutation"] == "line_item_delete"

    db_session.expire_all()
    accepted = db_session.get(Quote, quote.id)
    stored_line = db_session.get(QuoteLineItem, line.id)
    assert accepted is not None
    assert stored_line is not None
    assert stored_line.unit_price == Decimal("50000.00")
    assert accepted.total == accepted_total
    assert db_session.get(SalesOrder, order.id).total == accepted_total
    assert db_session.query(QuoteLineItem).filter_by(quote_id=quote.id).count() == 1
