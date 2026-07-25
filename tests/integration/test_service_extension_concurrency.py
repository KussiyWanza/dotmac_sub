"""PostgreSQL serialization for service-extension lifecycle transitions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditEvent
from app.models.catalog import (
    AccessType,
    PriceBasis,
    ServiceType,
    SubscriptionStatus,
)
from app.models.event_store import EventStore
from app.models.service_extension import ServiceExtensionEntry, ServiceExtensionScope
from app.models.subscriber import Subscriber
from app.schemas.catalog import (
    CatalogOfferCreate,
    OfferVersionCreate,
    SubscriptionCreate,
)
from app.services import catalog as catalog_service
from app.services import service_extensions
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id


def test_concurrent_apply_admits_one_transition_and_one_evidence_set(engine):
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    with session_factory() as setup:
        subscriber = Subscriber(
            first_name="Extension",
            last_name="Concurrency",
            email=f"extension-concurrency-{suffix}@example.com",
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(subscriber)
        setup.commit()
        offer = catalog_service.offers.create(
            setup,
            CatalogOfferCreate(
                name=f"Extension Concurrency {suffix}",
                code=f"EXT-CONC-{suffix}",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        catalog_service.offer_versions.create(
            setup,
            OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name=f"Extension Concurrency {suffix} v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        subscription = catalog_service.subscriptions.create(
            setup,
            SubscriptionCreate(
                account_id=subscriber.id,
                offer_id=offer.id,
                status=SubscriptionStatus.active,
                next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
        )
        setup.commit()
        created = service_extensions.create_service_extension(
            setup,
            service_extensions.CreateServiceExtensionCommand(
                context=CommandContext.system(
                    actor="service:pytest-service-extension-concurrency",
                    scope=service_extensions.CREATE_SCOPE,
                    reason="Create concurrent apply fixture",
                    idempotency_key=str(uuid4()),
                ),
                reason="Concurrent extension apply",
                window_start=datetime(2026, 7, 24, 8, 0, tzinfo=UTC),
                window_end=datetime(2026, 7, 24, 10, 0, tzinfo=UTC),
                days=2,
                scope_type=ServiceExtensionScope.subscribers,
                subscriber_identifiers=(str(subscriber.id),),
                subscriber_ids_resolved=True,
            ),
        )
        extension_id = created.extension_id
        subscription_id = subscription.id

    barrier = Barrier(2)

    def apply() -> bool:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            outcome = service_extensions.apply_service_extension(
                worker,
                service_extensions.ApplyServiceExtensionCommand(
                    context=CommandContext.system(
                        actor="service:pytest-service-extension-concurrency",
                        scope=service_extensions.APPLY_SCOPE,
                        reason="Verify locked concurrent apply",
                        idempotency_key=str(uuid4()),
                    ),
                    extension_id=extension_id,
                ),
            )
            return outcome.replayed

    with ThreadPoolExecutor(max_workers=2) as pool:
        replays = list(pool.map(lambda _index: apply(), range(2)))

    assert sorted(replays) == [False, True]
    with session_factory() as check:
        assert (
            check.query(ServiceExtensionEntry)
            .filter(
                ServiceExtensionEntry.extension_id == extension_id,
                ServiceExtensionEntry.subscription_id == subscription_id,
            )
            .count()
            == 1
        )
        assert (
            check.query(AuditEvent)
            .filter_by(
                entity_type="service_extension",
                entity_id=str(extension_id),
                action="billing.service_extension_applied",
            )
            .count()
            == 1
        )
        assert (
            check.query(EventStore)
            .filter(
                EventStore.event_type == "billing.service_extension_applied",
                EventStore.payload["extension_id"].astext == str(extension_id),
            )
            .count()
            == 1
        )
