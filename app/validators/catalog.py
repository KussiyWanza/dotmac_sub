from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import (
    AddOn,
    CatalogOffer,
    OfferAddOn,
    OfferStatus,
    OfferVersion,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Address, Subscriber


def validate_subscription_links(
    db: Session,
    subscriber_id: str,
    offer_id: str,
    offer_version_id: str | None,
    service_address_id: str | None,
):
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    offer = db.get(CatalogOffer, offer_id)
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")

    if offer_version_id:
        version = db.get(OfferVersion, offer_version_id)
        if not version:
            raise HTTPException(status_code=404, detail="Offer version not found")
        if str(version.offer_id) != offer_id:
            raise HTTPException(
                status_code=400, detail="Offer version does not match offer"
            )

    if service_address_id:
        address = db.get(Address, service_address_id)
        if not address:
            raise HTTPException(status_code=404, detail="Address not found")
        if str(address.subscriber_id) != subscriber_id:
            raise HTTPException(
                status_code=400,
                detail="Service address does not belong to subscriber",
            )


def validate_offer_active(db: Session, offer_id: str):
    offer = db.get(CatalogOffer, offer_id)
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")
    if not offer.is_active or offer.status != OfferStatus.active:
        raise HTTPException(status_code=400, detail="Offer is not active")
    return offer


def validate_offer_version_active(
    db: Session,
    offer_version_id: str,
    offer_id: str,
    reference_at: datetime,
):
    version = db.get(OfferVersion, offer_version_id)
    if not version:
        raise HTTPException(status_code=404, detail="Offer version not found")
    if str(version.offer_id) != offer_id:
        raise HTTPException(
            status_code=400, detail="Offer version does not match offer"
        )
    if not version.is_active:
        raise HTTPException(status_code=400, detail="Offer version is not active")
    if version.effective_start and reference_at < version.effective_start:
        raise HTTPException(status_code=400, detail="Offer version not yet effective")
    if version.effective_end and reference_at > version.effective_end:
        raise HTTPException(
            status_code=400, detail="Offer version is no longer effective"
        )
    return version


def validate_subscription_dates(
    status: SubscriptionStatus,
    start_at: datetime | None,
    end_at: datetime | None,
    next_billing_at: datetime | None,
    canceled_at: datetime | None,
):
    if status == SubscriptionStatus.active and start_at is None:
        raise HTTPException(
            status_code=400, detail="start_at required when subscription is active"
        )
    if status == SubscriptionStatus.active and next_billing_at is None:
        raise HTTPException(
            status_code=400,
            detail="next_billing_at required when subscription is active",
        )
    if start_at and end_at and start_at > end_at:
        raise HTTPException(status_code=400, detail="start_at must be before end_at")
    if next_billing_at and start_at and next_billing_at < start_at:
        raise HTTPException(
            status_code=400, detail="next_billing_at must be after start_at"
        )
    if status == SubscriptionStatus.canceled and not canceled_at:
        raise HTTPException(
            status_code=400, detail="canceled_at required when canceled"
        )
    if status != SubscriptionStatus.canceled and canceled_at:
        raise HTTPException(
            status_code=400, detail="canceled_at only allowed when canceled"
        )


def enforce_single_active_subscription(
    db: Session,
    subscriber_id: str,
    status: SubscriptionStatus,
    exclude_id: str | None = None,
):
    active_statuses = {
        SubscriptionStatus.pending,
        SubscriptionStatus.active,
        SubscriptionStatus.suspended,
    }
    if status not in active_statuses:
        return
    query = db.query(Subscription).filter(Subscription.subscriber_id == subscriber_id)
    if exclude_id:
        query = query.filter(Subscription.id != exclude_id)
    query = query.filter(Subscription.status.in_(active_statuses))
    if query.first():
        raise HTTPException(
            status_code=400,
            detail="Account already has an active subscription",
        )


def validate_subscription_add_on(
    db: Session,
    subscription_id: str,
    add_on_id: str,
    quantity: int,
):
    subscription = db.get(Subscription, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    add_on = db.get(AddOn, add_on_id)
    if not add_on or not add_on.is_active:
        raise HTTPException(status_code=404, detail="Add-on not found")
    link = (
        db.query(OfferAddOn)
        .filter(OfferAddOn.offer_id == subscription.offer_id)
        .filter(OfferAddOn.add_on_id == add_on_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=400, detail="Add-on not allowed for offer")
    if link.min_quantity and quantity < link.min_quantity:
        raise HTTPException(status_code=400, detail="Quantity below offer minimum")
    if link.max_quantity and quantity > link.max_quantity:
        raise HTTPException(status_code=400, detail="Quantity exceeds offer maximum")


def validate_offer_add_on(
    db: Session,
    offer_id: str,
    add_on_id: str,
    quantity: int,
):
    offer = db.get(CatalogOffer, offer_id)
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")
    add_on = db.get(AddOn, add_on_id)
    if not add_on or not add_on.is_active:
        raise HTTPException(status_code=404, detail="Add-on not found")
    link = (
        db.query(OfferAddOn)
        .filter(OfferAddOn.offer_id == offer_id)
        .filter(OfferAddOn.add_on_id == add_on_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=400, detail="Add-on not allowed for offer")
    if link.min_quantity and quantity < link.min_quantity:
        raise HTTPException(status_code=400, detail="Quantity below offer minimum")
    if link.max_quantity and quantity > link.max_quantity:
        raise HTTPException(status_code=400, detail="Quantity exceeds offer maximum")
    return link, add_on
