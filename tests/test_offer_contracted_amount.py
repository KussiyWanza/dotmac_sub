"""A subscription's contracted amount: resolved, or honestly absent. Never zero.

The lookup previously ended `return ... if price else Decimal("0")`. That zero
reads downstream as a real contracted amount — it renders as a genuine price in
billing summaries — while prepaid enforcement treats `unit_price` NULL *or
<= 0* as "no contracted terms" and blocks the account. So the zero satisfied
nobody: it did not make the subscription billable, and it hid why.

It also consulted only `OfferPrice`, so a subscription pinned to a correctly
priced offer *version* resolved to zero as well.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferPrice,
    OfferStatus,
    PriceBasis,
    PriceType,
    ServiceType,
)
from app.services.catalog.subscriptions import contracted_amount_for_offer


def _offer(db_session, *, basis: PriceBasis, amount: str | None) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Offer {basis.value} {amount} {uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=basis,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    if amount is not None:
        db_session.add(
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal(amount),
                is_active=True,
            )
        )
        db_session.flush()
    return offer


def test_active_recurring_price_is_returned(db_session):
    offer = _offer(db_session, basis=PriceBasis.flat, amount="15000.00")

    assert contracted_amount_for_offer(db_session, offer.id) == Decimal("15000.00")


def test_an_unpriced_offer_resolves_to_none_never_zero(db_session):
    """`None` is honestly absent; zero is a claim.

    A subscription without a price is a state this system models on purpose —
    the invoice cycle skips it and plan-family edits preserve a null price on a
    live subscription — so this reports absence rather than refusing.
    """
    offer = _offer(db_session, basis=PriceBasis.flat, amount=None)

    resolved = contracted_amount_for_offer(db_session, offer.id)

    assert resolved is None
    assert resolved != Decimal("0")


def test_a_usage_offer_without_a_recurring_price_resolves_to_none(db_session):
    offer = _offer(db_session, basis=PriceBasis.usage, amount=None)

    assert contracted_amount_for_offer(db_session, offer.id) is None


def test_a_pinned_offer_version_price_wins(db_session):
    """Resolution must mirror what the subscription is actually invoiced at.

    `billing_automation._resolve_price` prefers a pinned version's price, so
    consulting only OfferPrice reported a correctly-priced versioned offer as
    unpriced — and, with the old fallback, contracted it at zero.
    """
    from app.models.catalog import OfferVersion, OfferVersionPrice

    offer = _offer(db_session, basis=PriceBasis.flat, amount="10000.00")
    version = OfferVersion(
        offer_id=offer.id,
        version_number=2,
        name="v2",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    db_session.add(version)
    db_session.flush()
    db_session.add(
        OfferVersionPrice(
            offer_version_id=version.id,
            price_type=PriceType.recurring,
            amount=Decimal("22000.00"),
            is_active=True,
        )
    )
    db_session.flush()

    resolved = contracted_amount_for_offer(
        db_session, offer.id, offer_version_id=version.id
    )

    assert resolved == Decimal("22000.00")
