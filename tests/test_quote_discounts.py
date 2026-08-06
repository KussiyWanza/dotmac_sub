"""Focused contracts for Quote-level discounts and their history projection."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.models.party import Party, PartyIdentityStatus, PartyType
from app.models.project import ProjectType
from app.models.sales import (
    Lead,
    LeadStatus,
    Quote,
    QuoteDiscountAction,
    QuoteDiscountHistory,
    QuoteDiscountHistoryImmutableError,
    QuoteDiscountType,
    QuoteStatus,
)
from app.models.system_user import SystemUser
from app.schemas.sales import QuoteLineItemCreate
from app.services import web_sales
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.sales import quote_authoring, quote_discount_reporting
from app.services.sales import service as sales_service
from app.timezone import localize_datetime


def _identity(db_session) -> tuple[SystemUser, Lead, Party]:
    party = Party(
        party_type=PartyType.person.value,
        display_name="Discount Customer",
        status=PartyIdentityStatus.active.value,
    )
    actor = SystemUser(
        first_name="Ada",
        last_name="Sales",
        email=f"discount-{uuid4().hex}@example.com",
        is_active=True,
    )
    db_session.add_all([party, actor])
    db_session.flush()
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Quote discount test identity",
        title="Discounted rollout",
        status=LeadStatus.new.value,
        is_active=True,
    )
    db_session.add(lead)
    db_session.commit()
    return actor, lead, party


def _author_discounted_quote(
    db_session,
    *,
    actor: SystemUser,
    lead: Lead,
    discount_type: QuoteDiscountType = QuoteDiscountType.percentage,
    value: Decimal = Decimal("10"),
) -> Quote:
    quote_id = uuid4()
    command = quote_authoring.AuthorQuoteCommand(
        context=CommandContext.system(
            actor=str(actor.id),
            scope="crm:quote:write",
            reason="Test Quote-level discount",
        ),
        quote_id=quote_id,
        actor_system_user_id=actor.id,
        lead_id=lead.id,
        status=QuoteStatus.draft,
        currency="NGN",
        project_type=ProjectType.fiber_optics_installation,
        tax_rate_id=None,
        manual_tax_total=Decimal("5.00"),
        expires_at=None,
        is_active=True,
        notes=None,
        install=quote_authoring.QuoteInstallLocation(),
        lines=(
            quote_authoring.QuoteLineDraft(
                description="Installation",
                quantity=Decimal("2"),
                unit_price=Decimal("100"),
            ),
        ),
        discount=quote_authoring.QuoteDiscountInput(
            discount_type=discount_type,
            value=value,
        ),
    )
    db_session_adapter.release_read_transaction(db_session)
    outcome = quote_authoring.author_quote(db_session, command)
    assert outcome.replayed is False
    return db_session.get(Quote, quote_id)


def _change(
    db_session,
    *,
    quote: Quote,
    actor: SystemUser,
    expected_revision: int,
    discount: quote_authoring.QuoteDiscountInput | None,
    command_id: UUID | None = None,
):
    command = quote_authoring.ChangeQuoteDiscountCommand(
        context=CommandContext.system(
            actor=str(actor.id),
            scope="crm:quote:write",
            reason="Test Quote discount change",
            command_id=command_id,
        ),
        quote_id=quote.id,
        actor_system_user_id=actor.id,
        expected_revision=expected_revision,
        discount=discount,
    )
    db_session_adapter.release_read_transaction(db_session)
    return quote_authoring.change_quote_discount(db_session, command)


def test_fixed_amount_discount_is_applied_once_to_subtotal(db_session):
    actor, lead, _party = _identity(db_session)
    quote = _author_discounted_quote(
        db_session,
        actor=actor,
        lead=lead,
        discount_type=QuoteDiscountType.fixed_amount,
        value=Decimal("25"),
    )

    assert quote.subtotal == Decimal("200.00")
    assert quote.discount_amount == Decimal("25.00")
    assert quote.discounted_subtotal == Decimal("175.00")
    assert quote.tax_total == Decimal("5.00")
    assert quote.total == Decimal("180.00")


def test_changed_and_removed_discounts_remain_in_append_only_history(db_session):
    actor, lead, _party = _identity(db_session)
    quote = _author_discounted_quote(db_session, actor=actor, lead=lead)

    changed = _change(
        db_session,
        quote=quote,
        actor=actor,
        expected_revision=1,
        discount=quote_authoring.QuoteDiscountInput(
            discount_type=QuoteDiscountType.fixed_amount,
            value=Decimal("30"),
            reason="Retention approval",
        ),
    )
    removal_command_id = uuid4()
    removed = _change(
        db_session,
        quote=quote,
        actor=actor,
        expected_revision=changed.revision,
        discount=None,
        command_id=removal_command_id,
    )
    replayed_removal = _change(
        db_session,
        quote=quote,
        actor=actor,
        expected_revision=changed.revision,
        discount=None,
        command_id=removal_command_id,
    )

    histories = (
        db_session.query(QuoteDiscountHistory)
        .filter_by(quote_id=quote.id)
        .order_by(QuoteDiscountHistory.revision)
        .all()
    )
    db_session.refresh(quote)
    assert [row.action for row in histories] == [
        QuoteDiscountAction.applied.value,
        QuoteDiscountAction.changed.value,
        QuoteDiscountAction.removed.value,
    ]
    assert [row.revision for row in histories] == [1, 2, 3]
    assert histories[1].reason == "Retention approval"
    assert removed.discount_amount == Decimal("0.00")
    assert replayed_removal.replayed is True
    assert replayed_removal.discount_amount == Decimal("0.00")
    assert quote.discount_type is None
    assert quote.discount_amount == Decimal("0.00")
    assert quote.total == Decimal("205.00")

    histories[0].reason = "rewrite"
    with pytest.raises(QuoteDiscountHistoryImmutableError):
        db_session.flush()
    db_session.rollback()


def test_stale_discount_revision_and_accepted_quote_fail_closed(db_session):
    actor, lead, _party = _identity(db_session)
    quote = _author_discounted_quote(db_session, actor=actor, lead=lead)

    with pytest.raises(
        quote_authoring.QuoteAuthoringError, match="changed while this page was open"
    ):
        _change(
            db_session,
            quote=quote,
            actor=actor,
            expected_revision=0,
            discount=None,
        )

    db_session.rollback()
    quote.status = QuoteStatus.accepted.value
    db_session.commit()
    with pytest.raises(
        quote_authoring.QuoteAuthoringError, match="cannot be discounted"
    ):
        _change(
            db_session,
            quote=quote,
            actor=actor,
            expected_revision=1,
            discount=None,
        )


def test_line_changes_require_temporarily_removing_active_quote_discount(db_session):
    actor, lead, _party = _identity(db_session)
    quote = _author_discounted_quote(db_session, actor=actor, lead=lead)
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(DomainError, match="Remove the Quote discount"):
        sales_service.quote_line_items.create(
            db_session,
            QuoteLineItemCreate(
                quote_id=quote.id,
                description="Extra work",
                quantity=Decimal("1"),
                unit_price=Decimal("10"),
            ),
        )


def test_history_query_filters_customer_actor_type_status_and_date(db_session):
    actor, lead, _party = _identity(db_session)
    quote = _author_discounted_quote(db_session, actor=actor, lead=lead)
    history = db_session.query(QuoteDiscountHistory).filter_by(quote_id=quote.id).one()
    local_applied_at = localize_datetime(history.applied_at)
    assert local_applied_at is not None
    applied_date = local_applied_at.date()

    result = quote_discount_reporting.list_quote_discount_history(
        db_session,
        quote_discount_reporting.QuoteDiscountHistoryQuery(
            date_from=applied_date,
            date_to=applied_date,
            customer="Discount Customer",
            salesperson_id=actor.id,
            discount_type=QuoteDiscountType.percentage,
            quote_status=QuoteStatus.draft,
        ),
    )

    assert result.total_count == 1
    assert result.items[0].quote_id == quote.id
    assert result.items[0].customer_name == "Discount Customer"
    assert result.items[0].actor_name == "Ada Sales"
    assert result.items[0].discount_amount == Decimal("20.00")

    empty = quote_discount_reporting.list_quote_discount_history(
        db_session,
        quote_discount_reporting.QuoteDiscountHistoryQuery(
            date_from=date(2000, 1, 1),
            date_to=date(2000, 1, 1),
        ),
    )
    assert empty.total_count == 0


def test_history_failure_context_is_retryable_without_database_access():
    context = web_sales.build_quote_discounts_failure_context(
        date_from="2026-08-01",
        date_to="2026-08-05",
        customer="Discount Customer",
        salesperson_id=None,
        discount_type=QuoteDiscountType.percentage.value,
        quote_status=QuoteStatus.draft.value,
        page=1,
        per_page=25,
    )

    assert context["discounts"] == []
    assert context["total"] == 0
    assert context["retry_url"].startswith("/admin/sales/quote-discounts")
    assert "No Quote data was changed" in context["error"]
