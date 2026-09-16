"""PostgreSQL lock compatibility for billing anchors and bandwidth samples."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.bandwidth import BandwidthSample
from app.models.catalog import (
    AccessRequirement,
    AccessType,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.catalog import (
    CatalogOfferCreate,
    OfferVersionCreate,
    SubscriptionCreate,
)
from app.services import catalog as catalog_service
from app.services.account_lifecycle import (
    BillingAnchorProjectionCommand,
    BillingAnchorProjectionSource,
    stage_subscription_billing_anchor,
)
from app.services.catalog.offer_access_requirement import SystemAdmission
from app.services.subscriber import _default_reseller_id


def test_anchor_projection_does_not_block_bandwidth_foreign_key_insert(engine):
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    previous_anchor = datetime(2026, 9, 1, tzinfo=UTC)
    target_anchor = previous_anchor + timedelta(days=1)
    with session_factory() as setup:
        subscriber = Subscriber(
            first_name="Anchor",
            last_name="LockCompatibility",
            email=f"anchor-lock-{suffix}@example.com",
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(subscriber)
        setup.commit()
        offer = catalog_service.offers.create(
            setup,
            CatalogOfferCreate(
                name=f"Anchor Lock Compatibility {suffix}",
                code=f"ANCHOR-LOCK-{suffix}",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        offer_id = offer.id
        setup.commit()
        catalog_service.offer_versions.create(
            setup,
            OfferVersionCreate(
                access_requirement=AccessRequirement.unclassified,
                offer_id=offer_id,
                version_number=1,
                name=f"Anchor Lock Compatibility {suffix} v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
            principal=SystemAdmission(reason="test fixture"),
        )
        subscription = catalog_service.subscriptions.create(
            setup,
            SubscriptionCreate(
                account_id=subscriber.id,
                offer_id=offer.id,
                status=SubscriptionStatus.active,
                start_at=starts_at,
                next_billing_at=previous_anchor,
            ),
        )
        setup.commit()
        subscription_id = subscription.id

    anchor_locked = Event()
    bandwidth_inserted = Event()

    def project_anchor() -> None:
        with session_factory() as worker:
            subscription = worker.get(Subscription, subscription_id)
            assert subscription is not None
            stage_subscription_billing_anchor(
                worker,
                subscription,
                BillingAnchorProjectionCommand(
                    subscription_id=subscription_id,
                    expected_previous=previous_anchor,
                    target=target_anchor,
                    source=BillingAnchorProjectionSource.scheduled_billing,
                    evidence_ref=f"pytest:bandwidth-lock:{suffix}",
                ),
            )
            anchor_locked.set()
            assert bandwidth_inserted.wait(timeout=5), (
                "bandwidth FK insert blocked behind the billing-anchor row lock"
            )
            worker.commit()

    def insert_bandwidth_sample() -> None:
        assert anchor_locked.wait(timeout=5)
        with session_factory() as worker:
            worker.execute(text("SET LOCAL lock_timeout = '2s'"))
            worker.add(
                BandwidthSample(
                    subscription_id=subscription_id,
                    rx_bps=1,
                    tx_bps=1,
                    sample_at=starts_at,
                )
            )
            worker.flush()
            bandwidth_inserted.set()
            worker.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        projection = pool.submit(project_anchor)
        bandwidth = pool.submit(insert_bandwidth_sample)
        projection.result(timeout=10)
        bandwidth.result(timeout=10)
