"""Disposable staging acceptance for mistaken-subscription correction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from app.models.catalog import (
    AccessCredential,
    BillingMode,
    CatalogOffer,
    OfferRadiusProfile,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus, UserType

pytestmark = pytest.mark.e2e


@pytest.fixture()
def correction_candidate(e2e_db):
    """Create one unmistakable candidate only in the disposable E2E database."""
    suffix = uuid4().hex[:10]
    reseller = e2e_db.query(Reseller).order_by(Reseller.created_at).first()
    source_offer = (
        e2e_db.query(CatalogOffer)
        .filter(CatalogOffer.is_active.is_(True))
        .order_by(CatalogOffer.created_at)
        .first()
    )
    if reseller is None or source_offer is None:
        pytest.skip("E2E database needs one reseller and one active source offer")

    subscriber = Subscriber(
        first_name="Correction",
        last_name=f"Fixture {suffix}",
        email=f"correction-{suffix}@test.local",
        reseller_id=reseller.id,
        status=SubscriberStatus.active,
        user_type=UserType.customer,
        billing_enabled=True,
        is_active=True,
    )
    target_offer = CatalogOffer(
        name=f"Unlimited Lite 15 Mbps E2E {suffix}",
        code=f"CORR-15-{suffix}",
        service_type=source_offer.service_type,
        access_type=source_offer.access_type,
        price_basis=source_offer.price_basis,
        billing_mode=BillingMode.postpaid,
        is_active=True,
    )
    target_profile = RadiusProfile(
        name=f"Unlimited Lite 15 Mbps E2E {suffix}",
        code=f"CORR-RAD-15-{suffix}",
        download_speed=15000,
        upload_speed=15000,
        is_active=True,
    )
    e2e_db.add_all((subscriber, target_offer, target_profile))
    e2e_db.flush()
    e2e_db.add(
        OfferRadiusProfile(offer_id=target_offer.id, profile_id=target_profile.id)
    )

    now = datetime.now(UTC)
    login = f"correction-{suffix}"
    target = Subscription(
        subscriber_id=subscriber.id,
        offer_id=target_offer.id,
        status=SubscriptionStatus.stopped,
        billing_mode=BillingMode.postpaid,
        login=login,
        created_at=now - timedelta(days=2),
    )
    mistaken = Subscription(
        subscriber_id=subscriber.id,
        offer_id=source_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        login=login,
        created_at=now - timedelta(days=1),
    )
    e2e_db.add_all((target, mistaken))
    e2e_db.flush()
    e2e_db.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            subscription_id=mistaken.id,
            username=login,
            secret_hash="{noop}e2e-disposable-only",
            is_active=True,
        )
    )
    e2e_db.commit()
    return {
        "mistaken_id": mistaken.id,
        "target_id": target.id,
        "target_created_at": target.created_at,
        "target_offer_name": target_offer.name,
    }


def test_review_and_apply_exact_subscription_correction(
    admin_page: Page, settings, e2e_db, correction_candidate
):
    mistaken_id = correction_candidate["mistaken_id"]
    target_id = correction_candidate["target_id"]
    admin_page.goto(
        f"{settings.base_url}/admin/catalog/subscriptions/{mistaken_id}",
        wait_until="domcontentloaded",
    )

    action = admin_page.locator(
        f'[data-action-form="admin.subscription_correction.{target_id}"]'
    )
    expect(action).to_be_visible()
    expect(action).to_contain_text("Correct mistake: restore")
    expect(action).to_contain_text(str(target_id))
    expect(action).to_contain_text(
        correction_candidate["target_created_at"].isoformat()
    )
    expect(action).to_contain_text("15 Mbps down / 15 Mbps up")

    action.locator('input[name="confirmed"]').check()
    action.get_by_role("button", name="Apply reviewed correction").click()
    admin_page.wait_for_url(f"**/admin/catalog/subscriptions/{target_id}?notice=**")
    expect(
        admin_page.get_by_text("Subscription correction applied", exact=False)
    ).to_be_visible()

    e2e_db.expire_all()
    mistaken = e2e_db.get(Subscription, mistaken_id)
    target = e2e_db.get(Subscription, target_id)
    credential = (
        e2e_db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == target.subscriber_id)
        .filter(AccessCredential.is_active.is_(True))
        .one()
    )
    assert mistaken.status is SubscriptionStatus.canceled
    assert target.status is SubscriptionStatus.active
    assert credential.subscription_id == target.id
    assert credential.radius_profile_id is not None
