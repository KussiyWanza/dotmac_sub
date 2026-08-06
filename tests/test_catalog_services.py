from datetime import UTC, datetime, timedelta
from decimal import Decimal

from starlette.datastructures import FormData

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    NasVendor,
    OfferPrice,
    OfferStatus,
    PlanCategory,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import OLTDevice, OltProfileBundle, OltProfileSyncTask
from app.schemas.catalog import (
    CatalogOfferCreate,
    CatalogOfferUpdate,
    SubscriptionCreate,
)
from app.schemas.settings import DomainSettingUpdate
from app.services import catalog as catalog_service
from app.services import settings_api
from app.services import web_catalog_offers as web_catalog_offers_service


def test_catalog_offer_create_list(db_session):
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Fiber 100 Home",
            code="FIBER100",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    items = catalog_service.offers.list(
        db_session,
        service_type=None,
        access_type=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(items) == 1
    assert items[0].id == offer.id


def test_catalog_offer_defaults_use_settings(db_session):
    settings_api.upsert_catalog_setting(
        db_session, "default_billing_cycle", DomainSettingUpdate(value_text="annual")
    )
    settings_api.upsert_catalog_setting(
        db_session,
        "default_contract_term",
        DomainSettingUpdate(value_text="twelve_month"),
    )
    settings_api.upsert_catalog_setting(
        db_session, "default_offer_status", DomainSettingUpdate(value_text="inactive")
    )
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Fiber 500 Home",
            code="FIBER500",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    assert offer.billing_cycle.value == "annual"
    assert offer.contract_term.value == "twelve_month"
    assert offer.status.value == "inactive"


def test_offer_description_metadata_roundtrip():
    description = web_catalog_offers_service.normalize_offer_description(
        description="Public IP block add-on",
        plan_kind="ip_address",
        ip_block_size="/29",
    )
    meta, cleaned = web_catalog_offers_service.parse_offer_description_metadata(
        description
    )
    assert meta["plan_kind"] == "ip_address"
    assert meta["ip_block_size"] == "/29"
    assert cleaned == "Public IP block add-on"


def test_overview_page_data_filters_plan_kind_and_counts(db_session, subscriber):
    standard = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Standard Fiber",
            code="STD-1",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            description="Standard internet service",
            status=OfferStatus.active,
        ),
    )
    ip_plan = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Public IP /29",
            code="IP-29",
            service_type=ServiceType.business,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            description=web_catalog_offers_service.normalize_offer_description(
                description="Extra public addresses",
                plan_kind="ip_address",
                ip_block_size="/29",
            ),
            status=OfferStatus.active,
        ),
    )

    catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=ip_plan.id,
            status="active",
        ),
    )

    ip_only = web_catalog_offers_service.overview_page_data(
        db_session,
        plan_kind="ip_address",
        page=1,
        per_page=50,
    )
    assert len(ip_only["offers"]) == 1
    assert str(ip_only["offers"][0].id) == str(ip_plan.id)
    meta = ip_only["offer_plan_metadata"][str(ip_plan.id)]
    assert meta["plan_kind"] == "ip_address"
    assert meta["ip_block_size"] == "/29"
    assert ip_only["offer_active_subscription_counts"][str(ip_plan.id)] == 1

    standard_only = web_catalog_offers_service.overview_page_data(
        db_session,
        plan_kind="standard",
        page=1,
        per_page=50,
    )
    ids = {str(offer.id) for offer in standard_only["offers"]}
    assert str(standard.id) in ids
    assert str(ip_plan.id) not in ids


def test_ensure_offer_radius_profile_creates_generated_profile_and_link(db_session):
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Unlimited Basic",
            code="UNL-BASIC",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            # Megabits: the offer column's unit. The generated profile stores
            # kilobits, so 6 Mbps must land as 6000k — not 6k.
            speed_download_mbps=6,
            speed_upload_mbps=6,
        ),
    )

    profile_id = web_catalog_offers_service.ensure_offer_radius_profile(
        db_session, offer
    )

    links = catalog_service.offer_radius_profiles.list(
        db_session,
        offer_id=str(offer.id),
        profile_id=None,
        order_by="offer_id",
        order_dir="asc",
        limit=10,
        offset=0,
    )

    assert profile_id
    assert len(links) == 1
    assert str(links[0].profile_id) == profile_id

    profile = catalog_service.radius_profiles.get(db_session, profile_id)
    assert profile.name == "Unlimited Basic"
    assert (
        profile.code
        == web_catalog_offers_service.generated_radius_profile_code_for_offer(
            str(offer.id)
        )
    )
    assert profile.vendor == NasVendor.mikrotik
    assert profile.download_speed == 6000
    assert profile.upload_speed == 6000
    assert profile.mikrotik_rate_limit == "6000k/6000k"


def test_ensure_offer_radius_profile_updates_existing_generated_profile(db_session):
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Starter Plan",
            code="STARTER",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            speed_download_mbps=3,
            speed_upload_mbps=2,
        ),
    )

    first_profile_id = web_catalog_offers_service.ensure_offer_radius_profile(
        db_session, offer
    )

    updated_offer = catalog_service.offers.update(
        db_session,
        str(offer.id),
        CatalogOfferUpdate(
            name="Starter Plan Plus",
            speed_download_mbps=7,
            speed_upload_mbps=5,
        ),
    )

    second_profile_id = web_catalog_offers_service.ensure_offer_radius_profile(
        db_session,
        updated_offer,
        previous_profile_id=first_profile_id,
    )

    assert second_profile_id == first_profile_id
    profile = catalog_service.radius_profiles.get(db_session, second_profile_id)
    assert profile.name == "Starter Plan Plus"
    assert profile.download_speed == 7000
    assert profile.upload_speed == 5000
    # Columns stay download/upload; the emitted rx/tx string is upload-first.
    assert profile.mikrotik_rate_limit == "5000k/7000k"


def test_generated_profile_converts_offer_megabits_to_profile_kilobits(db_session):
    """The offer column is megabits; the profile column is kilobits.

    Nothing converted between them, so a 100 Mbps offer generated a
    ``100k/100k`` rate limit — a 1000x throttle that
    ``sync_offer_radius_profile_to_subscriptions`` then pushed to every live
    subscription on that offer. Any admin edit was enough to trigger it.

    Asserted against a literal rather than a multiple of the input so the
    boundary cannot be "fixed" by moving both sides together.
    """

    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Unit Boundary 100",
            code="UNIT-100",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            speed_download_mbps=100,
            speed_upload_mbps=100,
        ),
    )

    profile_id = web_catalog_offers_service.ensure_offer_radius_profile(
        db_session, offer
    )
    profile = catalog_service.radius_profiles.get(db_session, profile_id)

    assert profile.download_speed == 100_000, "100 Mbps is 100000 kbps, not 100"
    assert profile.upload_speed == 100_000
    assert profile.mikrotik_rate_limit == "100000k/100000k"


def test_generated_rate_limit_emits_upload_first(db_session):
    """Mikrotik-Rate-Limit is rx/tx from the NAS's perspective, so rx is the
    subscriber's UPLOAD and tx their DOWNLOAD — the convention stated in
    ``app.services.bandwidth.to_subscriber_directions``.

    Every current offer is symmetrical, which is exactly why this needs an
    asymmetric fixture: with equal speeds the ordering is unobservable and a
    regression would ship silently, then invert real customers' rates the day
    an asymmetric plan is created.
    """

    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Asymmetric Direction Probe",
            code="ASYM-DIR",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            speed_download_mbps=100,
            speed_upload_mbps=20,
        ),
    )

    profile_id = web_catalog_offers_service.ensure_offer_radius_profile(
        db_session, offer
    )
    profile = catalog_service.radius_profiles.get(db_session, profile_id)

    # 20 Mbps up first, 100 Mbps down second. Reversed, the customer would get
    # 20 Mbps down and 100 Mbps up.
    assert profile.mikrotik_rate_limit == "20000k/100000k"


def test_update_offer_with_audit_stages_profile_sync_task_for_existing_bundle(
    db_session,
) -> None:
    olt = OLTDevice(name="Catalog Sync OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Catalog Fiber 100",
        code="CF100",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=100,
        speed_upload_mbps=50,
        olt_profile_auto_sync_enabled=True,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    db_session.add(
        OltProfileBundle(
            olt_id=olt.id,
            offer_id=offer.id,
            name=offer.name,
            checksum="2" * 64,
            vlan_id=203,
            download_kbps=100_000,
            upload_kbps=50_000,
            dba_profile_id=100,
            download_traffic_table_id=101,
            upload_traffic_table_id=102,
            line_profile_id=150,
            service_profile_id=151,
            gem_id=1,
            tcont_id=1,
            command_plan={"groups": []},
            drift_status="applied",
            is_active=True,
        )
    )
    db_session.commit()

    offer_data, _ = web_catalog_offers_service.offer_edit_form_data(
        db_session,
        str(offer.id),
        offer,
    )
    offer_data["speed_download_mbps"] = "150"
    offer_data["price_amount"] = ""

    web_catalog_offers_service.update_offer_with_audit(
        db_session,
        str(offer.id),
        offer,
        offer_data,
        FormData([]),
        request=None,
        actor_id="admin@example.test",
    )
    db_session.commit()

    task = db_session.query(OltProfileSyncTask).one()
    assert task.status == "pending"
    assert task.trigger == "catalog_offer_update"
    assert task.requested_by == "admin@example.test"
    assert task.preview_payload["vlan_id"] == 203
    assert task.preview_payload["download_kbps"] == 150_000


def test_offer_plan_family_edit_preserves_null_price_unit_with_live_subscription(
    db_session, subscriber, catalog_offer
) -> None:
    price = OfferPrice(
        offer_id=catalog_offer.id,
        price_type=PriceType.recurring,
        amount=Decimal("100.00"),
        currency="NGN",
        billing_cycle=BillingCycle.monthly,
        unit=None,
        is_active=True,
    )
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        start_at=datetime.now(UTC) - timedelta(days=5),
        next_billing_at=datetime.now(UTC) + timedelta(days=25),
    )
    db_session.add_all([price, subscription])
    db_session.commit()

    offer_data, _ = web_catalog_offers_service.offer_edit_form_data(
        db_session, str(catalog_offer.id), catalog_offer
    )
    assert offer_data["price_unit"] == ""
    offer_data["plan_family"] = "unlimited"

    updated = web_catalog_offers_service.update_offer_with_audit(
        db_session,
        str(catalog_offer.id),
        catalog_offer,
        offer_data,
        FormData([]),
        request=None,
        actor_id="admin@example.test",
    )
    db_session.commit()

    db_session.refresh(price)
    assert updated.plan_family == "unlimited"
    assert price.unit is None


def test_offer_form_context_exposes_full_billing_cycle_set(db_session):
    context = web_catalog_offers_service.offer_form_context(
        db_session,
        web_catalog_offers_service.default_offer_form(),
    )

    assert context["billing_cycles"] == [item.value for item in BillingCycle]


def test_overview_page_data_filters_plan_family(db_session):
    catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Unlimited Basic",
            code="FAM-UNL",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            plan_family="unlimited",
            status=OfferStatus.active,
        ),
    )
    catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Lonely IP Block",
            code="FAM-NONE",
            service_type=ServiceType.business,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            status=OfferStatus.active,
        ),
    )

    unlimited = web_catalog_offers_service.overview_page_data(
        db_session, plan_family="unlimited", page=1, per_page=50
    )
    names = {offer.name for offer in unlimited["offers"]}
    assert names == {"Unlimited Basic"}
    assert unlimited["plan_family"] == "unlimited"

    unclassified = web_catalog_offers_service.overview_page_data(
        db_session, plan_family="unclassified", page=1, per_page=50
    )
    names = {offer.name for offer in unclassified["offers"]}
    assert "Lonely IP Block" in names
    assert "Unlimited Basic" not in names


def test_plan_family_values_settings_driven(db_session):
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.models.subscription_engine import SettingValueType

    # Default: the built-in tuple
    assert web_catalog_offers_service.plan_family_values(db_session) == [
        "unlimited",
        "dedicated",
        "home_flex",
    ]

    db_session.add(
        DomainSetting(
            domain=SettingDomain.catalog,
            key="plan_families",
            value_type=SettingValueType.string,
            value_text="unlimited, dedicated, home_flex, Business Fiber",
            is_active=True,
        )
    )
    db_session.commit()

    assert web_catalog_offers_service.plan_family_values(db_session) == [
        "unlimited",
        "dedicated",
        "home_flex",
        "business_fiber",
    ]


def test_new_offer_visibility_defaults_off(db_session):
    # A fresh offer must not silently land on the customer portal (the "~40
    # e2e offers went customer-visible" incident) — default OFF.
    form = web_catalog_offers_service.default_offer_form(db_session)
    assert form["show_on_customer_portal"] is False
    assert form["available_for_services"] is False


def test_new_offer_visibility_opt_in_setting(db_session):
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.models.subscription_engine import SettingValueType

    db_session.add(
        DomainSetting(
            domain=SettingDomain.catalog,
            key="new_offer_visible_by_default",
            value_type=SettingValueType.boolean,
            value_text="true",
            value_json=True,
            is_active=True,
        )
    )
    db_session.commit()

    form = web_catalog_offers_service.default_offer_form(db_session)
    assert form["show_on_customer_portal"] is True
    assert form["available_for_services"] is True
