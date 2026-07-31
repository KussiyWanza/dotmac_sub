import uuid

from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    NasDevice,
    NasDeviceStatus,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.network_monitoring import PopSite
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.services.web_customer_lists import (
    CUSTOMER_LIST_DEFINITION,
    build_customer_list_query,
    build_customers_index_context,
)


def _build_context(db_session, **params):
    return build_customers_index_context(
        db_session,
        list_query=build_customer_list_query(**params),
    )


def _make_offer(db_session):
    offer = CatalogOffer(
        name=f"Customer List Offer {uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    return offer


def _make_customer(db_session, email: str) -> Subscriber:
    customer = Subscriber(
        first_name="Customer",
        last_name=email.split("@", 1)[0],
        email=email,
        user_type=UserType.customer,
        is_active=True,
    )
    db_session.add(customer)
    db_session.flush()
    return customer


def _make_pop_site(db_session, name: str) -> PopSite:
    pop_site = PopSite(name=f"{name} {uuid.uuid4().hex[:8]}", is_active=True)
    db_session.add(pop_site)
    db_session.flush()
    return pop_site


def _make_nas(db_session, name: str, pop_site: PopSite | None = None) -> NasDevice:
    nas = NasDevice(
        name=f"{name} {uuid.uuid4().hex[:8]}",
        status=NasDeviceStatus.active,
        is_active=True,
        pop_site_id=pop_site.id if pop_site else None,
    )
    db_session.add(nas)
    db_session.flush()
    return nas


def _make_subscription(
    db_session,
    customer: Subscriber,
    *,
    status: SubscriptionStatus,
    ipv4_address: str | None = None,
    nas_device: NasDevice | None = None,
    login: str | None = None,
) -> Subscription:
    subscription = Subscription(
        subscriber_id=customer.id,
        offer_id=_make_offer(db_session).id,
        status=status,
        billing_mode=BillingMode.postpaid,
        ipv4_address=ipv4_address,
        provisioning_nas_device_id=nas_device.id if nas_device else None,
        login=login,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _make_ipam_assignment(
    db_session,
    customer: Subscriber,
    subscription: Subscription,
    ip_address: str,
) -> IPAssignment:
    address = IPv4Address(address=ip_address)
    db_session.add(address)
    db_session.flush()
    assignment = IPAssignment(
        subscriber_id=customer.id,
        subscription_id=subscription.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address.id,
        is_active=True,
    )
    db_session.add(assignment)
    db_session.flush()
    return assignment


def test_customer_list_excludes_reseller_users(db_session):
    customer = Subscriber(
        first_name="Customer",
        last_name="User",
        email="customer-list@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    reseller = Subscriber(
        first_name="Reseller",
        last_name="User",
        email="reseller-list@example.com",
        user_type=UserType.reseller,
        is_active=True,
    )
    db_session.add_all([customer, reseller])
    db_session.commit()

    context = _build_context(
        db_session,
        search=None,
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    emails = {item["email"] for item in context["customers"]}
    assert customer.email in emails
    assert reseller.email not in emails


def test_customer_list_keeps_explicitly_retained_canceled_imports(db_session):
    retained = Subscriber(
        first_name="Retained",
        last_name="Canceled",
        email="retained-canceled-list@example.com",
        status=SubscriberStatus.canceled,
        is_active=False,
        user_type=UserType.customer,
        splynx_customer_id=7301,
        metadata_={"splynx_deleted": False, "splynx_status": "blocked"},
    )
    legacy_deleted = Subscriber(
        first_name="Legacy",
        last_name="Deleted",
        email="legacy-deleted-list@example.com",
        status=SubscriberStatus.canceled,
        is_active=False,
        user_type=UserType.customer,
        splynx_customer_id=7302,
        metadata_={"splynx_status": "blocked"},
    )
    db_session.add_all([retained, legacy_deleted])
    db_session.commit()

    context = _build_context(
        db_session,
        search="canceled-list",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    emails = {item["email"] for item in context["customers"]}
    assert retained.email in emails
    assert legacy_deleted.email not in emails


def test_customer_list_projects_service_counts_without_action_ids(db_session):
    customer = _make_customer(db_session, "service-counts@example.com")
    _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.active,
    )
    _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.suspended,
    )
    db_session.commit()

    context = _build_context(
        db_session,
        search="service-counts",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    projected = context["customers"][0]
    assert projected["active_subscription_count"] == 1
    assert projected["suspended_subscription_count"] == 1
    assert "active_subscription_ids" not in projected
    assert "suspended_subscription_ids" not in projected


def test_customer_list_ip_search_matches_exact_current_ipv4_only(db_session):
    current = _make_customer(db_session, "current-ip@example.com")
    current_sub = _make_subscription(
        db_session,
        current,
        status=SubscriptionStatus.active,
        ipv4_address="160.119.126.18",
    )
    _make_ipam_assignment(db_session, current, current_sub, "160.119.126.18")

    suffix = _make_customer(db_session, "suffix-ip@example.com")
    _make_subscription(
        db_session,
        suffix,
        status=SubscriptionStatus.active,
        ipv4_address="160.119.126.180",
    )

    historical = _make_customer(db_session, "historical-ip@example.com")
    _make_subscription(
        db_session,
        historical,
        status=SubscriptionStatus.canceled,
        ipv4_address="160.119.126.18",
    )
    db_session.commit()

    context = _build_context(
        db_session,
        search="160.119.126.18",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    emails = {item["email"] for item in context["customers"]}
    assert emails == {"current-ip@example.com"}


def test_customer_list_display_prefers_active_ipam_then_active_subscription(
    db_session,
):
    customer = _make_customer(db_session, "display-ip@example.com")
    active_sub = _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.active,
        ipv4_address="10.0.0.5",
    )
    _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.canceled,
        ipv4_address="10.0.0.9",
    )
    _make_ipam_assignment(db_session, customer, active_sub, "10.0.0.7")
    db_session.commit()

    context = _build_context(
        db_session,
        search="display-ip",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    row = next(item for item in context["customers"] if item["email"] == customer.email)
    assert row["ipv4"] == "10.0.0.7"
    assert row["ipv4_label"] == "Current IPAM IPv4"


def test_customer_list_trims_search_before_text_matching(db_session):
    suffix = uuid.uuid4().hex[:8]
    customer = _make_customer(db_session, f"trim-search-{suffix}@example.com")
    customer.first_name = f"TrimName{suffix}"
    customer.phone = f"080{suffix[:8]}"
    customer.account_number = f"ACC-TRIM-{suffix}"
    pppoe_login = f"pppoe-trim-{suffix}"
    _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.active,
        login=pppoe_login,
    )
    db_session.commit()

    search_terms = [
        customer.first_name,
        customer.email,
        customer.phone,
        customer.account_number,
        pppoe_login,
    ]
    for term in search_terms:
        context = _build_context(
            db_session,
            search=f"  {term}  ",
            status=None,
            customer_type=None,
            nas_id=None,
            pop_site_id=None,
            page=1,
            per_page=25,
        )

        emails = {item["email"] for item in context["customers"]}
        assert customer.email in emails
        assert context["search"] == term


def test_customer_list_does_not_display_placeholder_ipv4(db_session):
    customer = _make_customer(db_session, "placeholder-ip@example.com")
    _make_subscription(
        db_session,
        customer,
        status=SubscriptionStatus.active,
        ipv4_address="0.0.0.0",
    )
    db_session.commit()

    context = _build_context(
        db_session,
        search="placeholder-ip",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    row = next(item for item in context["customers"] if item["email"] == customer.email)
    assert row["ipv4"] is None
    assert row["ipv4_label"] is None


def test_customer_location_filter_uses_customer_pop_site_not_nas_pop_site(db_session):
    karu_bts = _make_pop_site(db_session, "Karu BTS")
    afr_pop = _make_pop_site(db_session, "AFR")
    afr_nas = _make_nas(db_session, "AFR Access", afr_pop)

    karu_customer = _make_customer(db_session, "karu-location@example.com")
    karu_customer.pop_site_id = karu_bts.id
    _make_subscription(
        db_session,
        karu_customer,
        status=SubscriptionStatus.active,
        nas_device=afr_nas,
    )

    afr_customer = _make_customer(db_session, "afr-location@example.com")
    afr_customer.pop_site_id = afr_pop.id
    _make_subscription(
        db_session,
        afr_customer,
        status=SubscriptionStatus.active,
        nas_device=afr_nas,
    )
    db_session.commit()

    context = _build_context(
        db_session,
        search=None,
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=str(karu_bts.id),
        page=1,
        per_page=25,
    )

    emails = {item["email"] for item in context["customers"]}
    assert karu_customer.email in emails
    assert afr_customer.email not in emails


def test_customer_nas_filter_still_uses_subscription_nas(db_session):
    karu_bts = _make_pop_site(db_session, "Karu BTS")
    afr_pop = _make_pop_site(db_session, "AFR")
    afr_nas = _make_nas(db_session, "AFR Access", afr_pop)

    karu_customer = _make_customer(db_session, "karu-nas@example.com")
    karu_customer.pop_site_id = karu_bts.id
    _make_subscription(
        db_session,
        karu_customer,
        status=SubscriptionStatus.active,
        nas_device=afr_nas,
    )
    db_session.commit()

    context = _build_context(
        db_session,
        search=None,
        status=None,
        customer_type=None,
        nas_id=str(afr_nas.id),
        pop_site_id=None,
        page=1,
        per_page=25,
    )

    emails = {item["email"] for item in context["customers"]}
    assert karu_customer.email in emails
    assert any(str(nas.id) == str(afr_nas.id) for nas in context["nas_options"])


def test_customer_list_declares_search_filter_and_sort_capabilities():
    assert CUSTOMER_LIST_DEFINITION.searchable_keys == (
        "name",
        "email",
        "phone",
        "account_number",
        "pppoe_login",
        "ipv4",
    )
    assert CUSTOMER_LIST_DEFINITION.filterable_keys == (
        "customer_type",
        "status",
        "nas_id",
        "pop_site_id",
    )
    assert CUSTOMER_LIST_DEFINITION.sortable_keys == (
        "name",
        "status",
        "created_at",
    )


def test_customer_list_filters_before_building_page_metadata(db_session):
    token = f"PageFilter{uuid.uuid4().hex[:8]}"
    for index in range(11):
        customer = _make_customer(db_session, f"active-{index}-{token}@example.com")
        customer.first_name = token
        customer.status = SubscriberStatus.active
    for index in range(11):
        customer = _make_customer(db_session, f"suspended-{index}-{token}@example.com")
        customer.first_name = token
        customer.status = SubscriberStatus.suspended
    db_session.commit()

    context = _build_context(
        db_session,
        search=token,
        status="active",
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        sort_by="created_at",
        sort_dir="asc",
        page=2,
        per_page=10,
    )

    assert context["page_meta"].total_items == 11
    assert context["page_meta"].total_pages == 2
    assert context["page_meta"].start_item == 11
    assert len(context["customers"]) == 1
    assert context["customers"][0]["raw"].status.value == "active"
    assert context["customers"][0]["status_presentation"].model_dump(mode="json") == {
        "value": "active",
        "label": "Active",
        "tone": "positive",
        "icon": "check",
    }


def test_customer_list_uses_id_tie_breaker_across_pages(db_session):
    token = f"StableSort{uuid.uuid4().hex[:8]}"
    created_ids = []
    for index in range(12):
        customer = _make_customer(db_session, f"stable-{index}-{token}@example.com")
        customer.first_name = token
        customer.last_name = "Same"
        created_ids.append(str(customer.id))
    db_session.commit()

    common = {
        "search": token,
        "status": None,
        "customer_type": None,
        "nas_id": None,
        "pop_site_id": None,
        "sort_by": "name",
        "sort_dir": "asc",
        "per_page": 10,
    }
    first = _build_context(db_session, page=1, **common)
    second = _build_context(db_session, page=2, **common)

    projected_ids = [
        str(customer["id"]) for customer in [*first["customers"], *second["customers"]]
    ]
    assert projected_ids == sorted(created_ids)
    assert set(projected_ids[:10]).isdisjoint(projected_ids[10:])
