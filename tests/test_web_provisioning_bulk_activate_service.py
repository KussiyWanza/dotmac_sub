from __future__ import annotations

from app.models.audit import AuditEvent
from app.models.catalog import (
    AccessType,
    BillingCycle,
    CatalogOffer,
    OfferStatus,
    PlanCategory,
    PriceBasis,
    ServiceType,
    Subscription,
)
from app.models.network import IPAssignment, IpPool, IPVersion
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.services import web_provisioning_bulk_activate as bulk_service


def _create_offer(db_session, *, name: str, category: PlanCategory) -> CatalogOffer:
    offer = CatalogOffer(
        name=name,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        plan_category=category,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    return offer


def test_bulk_activation_preview_counts(db_session):
    reseller = Reseller(name="Partner A", is_active=True)
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)

    s1 = Subscriber(
        first_name="Alice",
        last_name="Preview",
        email="alice-preview@example.com",
        status=SubscriberStatus.suspended,
        reseller_id=reseller.id,
    )
    s2 = Subscriber(
        first_name="Bob",
        last_name="Preview",
        email="bob-preview@example.com",
        status=SubscriberStatus.suspended,
        reseller_id=reseller.id,
    )
    db_session.add(s1)
    db_session.add(s2)
    db_session.commit()

    offer = _create_offer(
        db_session, name="Internet Plan", category=PlanCategory.internet
    )
    filters = bulk_service.BulkFilters(
        tab="internet",
        reseller_id=str(reseller.id),
        subscriber_status="suspended",
        pop_site_id=None,
        date_from=None,
        date_to=None,
        custom_attr_key=None,
        custom_attr_value=None,
    )
    mapping = bulk_service.BulkMapping(
        offer_id=str(offer.id),
        activation_date=None,
        nas_device_id=None,
        ipv4_assignment="dynamic",
        static_ipv4=None,
        mac_address=None,
        login_prefix="sub-",
        login_suffix=None,
        service_password_mode="auto",
        service_password_manual=None,
        skip_active_service_check=False,
        set_subscribers_active=True,
    )

    preview = bulk_service.build_preview(db_session, filters=filters, mapping=mapping)
    assert preview["total_matches"] == 2
    assert preview["counts"]["create"] == 2


def test_bulk_activation_execute_creates_subscriptions_and_audit(db_session):
    reseller = Reseller(name="Partner B", is_active=True)
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)

    subscriber = Subscriber(
        first_name="Charlie",
        last_name="Execute",
        email="charlie-execute@example.com",
        status=SubscriberStatus.suspended,
        reseller_id=reseller.id,
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)

    offer = _create_offer(
        db_session, name="Recurring Plan", category=PlanCategory.recurring
    )
    filters = bulk_service.BulkFilters(
        tab="recurring",
        reseller_id=str(reseller.id),
        subscriber_status="suspended",
        pop_site_id=None,
        date_from=None,
        date_to=None,
        custom_attr_key=None,
        custom_attr_value=None,
    )
    mapping = bulk_service.BulkMapping(
        offer_id=str(offer.id),
        activation_date=None,
        nas_device_id=None,
        ipv4_assignment="dynamic",
        static_ipv4=None,
        mac_address="AA:BB:CC:DD:EE:FF",
        login_prefix="isp-",
        login_suffix="-x",
        service_password_mode="manual",
        service_password_manual="SecretPass123",
        skip_active_service_check=False,
        set_subscribers_active=True,
    )

    job = bulk_service.create_job(
        db_session,
        filters=filters,
        mapping=mapping,
        actor_id=str(subscriber.id),
    )
    result = bulk_service.execute_job(db_session, job_id=str(job["job_id"]))
    assert result["status"] in {"completed", "partial"}

    created = (
        db_session.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .all()
    )
    assert created
    assert created[0].offer_id == offer.id
    assert created[0].mac_address == "AA:BB:CC:DD:EE:FF"

    db_session.refresh(subscriber)
    assert subscriber.status == SubscriberStatus.active

    audit_rows = (
        db_session.query(AuditEvent).filter(AuditEvent.action == "bulk_activate").all()
    )
    assert audit_rows


def _static_mapping(offer_id: str) -> bulk_service.BulkMapping:
    return bulk_service.BulkMapping(
        offer_id=offer_id,
        activation_date=None,
        nas_device_id=None,
        ipv4_assignment="static",
        static_ipv4="10.99.0.5",
        mac_address=None,
        login_prefix=None,
        login_suffix=None,
        service_password_mode="auto",
        service_password_manual=None,
        skip_active_service_check=False,
        set_subscribers_active=False,
    )


def test_static_ipv4_is_refused_for_a_multi_subscriber_batch(db_session):
    """One scalar address across N subscribers is a duplicate factory.

    ``Subscription.ipv4_address`` has no uniqueness constraint, so this used to
    land silently and surface later as ``duplicate_served_projection``.
    """
    reseller = Reseller(name="Partner Static Multi", is_active=True)
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)

    for index in range(2):
        db_session.add(
            Subscriber(
                first_name="Static",
                last_name=f"Multi{index}",
                email=f"static-multi-{index}@example.com",
                status=SubscriberStatus.suspended,
                reseller_id=reseller.id,
            )
        )
    db_session.commit()

    offer = _create_offer(
        db_session, name="Static Multi Plan", category=PlanCategory.recurring
    )
    filters = bulk_service.BulkFilters(
        tab="recurring",
        reseller_id=str(reseller.id),
        subscriber_status="suspended",
        pop_site_id=None,
        date_from=None,
        date_to=None,
        custom_attr_key=None,
        custom_attr_value=None,
    )
    job = bulk_service.create_job(
        db_session,
        filters=filters,
        mapping=_static_mapping(str(offer.id)),
        actor_id=None,
    )
    result = bulk_service.execute_job(db_session, job_id=str(job["job_id"]))

    assert result["status"] == "failed"
    assert "10.99.0.5" in str(result.get("error") or "")

    served = [
        row.ipv4_address
        for row in db_session.query(Subscription).all()
        if row.ipv4_address
    ]
    assert served == [], "a refused batch must not write any served address"


def test_bulk_activation_never_writes_a_served_ipv4_without_an_assignment(db_session):
    """The served column is a projection of IPAM, never a standalone write.

    Bulk activation calls ``activate_subscription(emit=False)``, so the
    provisioning handler's allocator never runs on its own. Whatever this path
    writes into ``ipv4_address`` must still be backed by an ``IPAssignment`` —
    an unbacked column value is the ``assignment_missing`` cohort.
    """
    reseller = Reseller(name="Partner Static Single", is_active=True)
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)

    subscriber = Subscriber(
        first_name="Static",
        last_name="Single",
        email="static-single@example.com",
        status=SubscriberStatus.suspended,
        reseller_id=reseller.id,
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)

    db_session.add(
        IpPool(
            name="bulk-static-v4",
            ip_version=IPVersion.ipv4,
            cidr="10.99.0.0/24",
            gateway="10.99.0.1",
            is_active=True,
        )
    )
    db_session.commit()

    offer = _create_offer(
        db_session, name="Static Single Plan", category=PlanCategory.recurring
    )
    filters = bulk_service.BulkFilters(
        tab="recurring",
        reseller_id=str(reseller.id),
        subscriber_status="suspended",
        pop_site_id=None,
        date_from=None,
        date_to=None,
        custom_attr_key=None,
        custom_attr_value=None,
    )
    job = bulk_service.create_job(
        db_session,
        filters=filters,
        mapping=_static_mapping(str(offer.id)),
        actor_id=None,
    )
    bulk_service.execute_job(db_session, job_id=str(job["job_id"]))

    created = (
        db_session.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .all()
    )
    assert len(created) == 1
    subscription = created[0]
    # Non-vacuous: the requested static address must actually have been served,
    # otherwise the invariant below would hold trivially.
    assert subscription.ipv4_address == "10.99.0.5"

    backing = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscription_id == subscription.id)
        .filter(IPAssignment.is_active.is_(True))
        .filter(IPAssignment.ipv4_address_id.is_not(None))
        .all()
    )
    assert backing, (
        f"subscription {subscription.id} carries served IPv4 "
        f"{subscription.ipv4_address} with no active exact-service IPAssignment "
        "— this is exactly the assignment_missing cohort"
    )
    assert backing[0].ipv4_address.address == "10.99.0.5"


def test_bulk_activation_snapshots_the_contracted_amount(db_session):
    """A bulk-activated subscription must not be born without its price.

    Bulk activation builds the Subscription directly instead of going through
    Subscriptions.create, so it used to skip that owner's price snapshot and
    persist unit_price NULL. Prepaid enforcement then fails closed forever with
    `contracted_prepaid_renewal_terms_unavailable`: no threshold can be
    computed, so the account keeps consuming service and never suspends.
    """
    from decimal import Decimal

    from app.models.catalog import OfferPrice, PriceType

    reseller = Reseller(name="Partner Price", is_active=True)
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)

    subscriber = Subscriber(
        first_name="Dana",
        last_name="Priced",
        email="dana-priced@example.com",
        status=SubscriberStatus.suspended,
        reseller_id=reseller.id,
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)

    offer = _create_offer(
        db_session, name="Priced Plan", category=PlanCategory.recurring
    )
    db_session.add(
        OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            amount=Decimal("15000.00"),
            is_active=True,
        )
    )
    db_session.commit()

    filters = bulk_service.BulkFilters(
        tab="recurring",
        reseller_id=str(reseller.id),
        subscriber_status="suspended",
        pop_site_id=None,
        date_from=None,
        date_to=None,
        custom_attr_key=None,
        custom_attr_value=None,
    )
    mapping = bulk_service.BulkMapping(
        offer_id=str(offer.id),
        activation_date=None,
        nas_device_id=None,
        ipv4_assignment="dynamic",
        static_ipv4=None,
        mac_address="AA:BB:CC:DD:EE:01",
        login_prefix="isp-",
        login_suffix="-p",
        service_password_mode="manual",
        service_password_manual="SecretPass123",
        skip_active_service_check=False,
        set_subscribers_active=True,
    )

    job = bulk_service.create_job(
        db_session, filters=filters, mapping=mapping, actor_id=str(subscriber.id)
    )
    bulk_service.execute_job(db_session, job_id=str(job["job_id"]))

    created = (
        db_session.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .all()
    )
    assert created
    assert created[0].unit_price == Decimal("15000.00"), (
        "bulk activation must snapshot the offer's contracted amount; a NULL "
        "unit_price blocks prepaid enforcement for this account permanently"
    )
