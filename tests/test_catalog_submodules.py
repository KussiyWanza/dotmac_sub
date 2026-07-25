"""Comprehensive tests for catalog service submodules.

Tests cover: Offers, Subscriptions, AddOns, Profiles (RegionZone, UsageAllowance,
SlaProfile), RADIUS profiles/attributes, AccessCredentials, PolicySets/DunningSteps,
OfferAddOn linking, and OfferValidation.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.catalog import (
    AccessType,
    AddOnType,
    BillingCycle,
    BillingMode,
    ContractTerm,
    DunningAction,
    NasVendor,
    OfferStatus,
    PriceBasis,
    PriceType,
    ProrationPolicy,
    ServiceType,
    SubscriptionStatus,
)
from app.schemas.catalog import (
    AccessCredentialCreate,
    AccessCredentialUpdate,
    AddOnCreate,
    AddOnPriceCreate,
    AddOnPriceUpdate,
    AddOnUpdate,
    CatalogOfferCreate,
    CatalogOfferUpdate,
    OfferPriceCreate,
    OfferPriceUpdate,
    OfferRadiusProfileCreate,
    OfferValidationRequest,
    OfferVersionCreate,
    OfferVersionPriceCreate,
    OfferVersionPriceUpdate,
    OfferVersionUpdate,
    PolicyDunningStepCreate,
    PolicyDunningStepUpdate,
    PolicySetCreate,
    PolicySetUpdate,
    RadiusAttributeCreate,
    RadiusAttributeUpdate,
    RadiusProfileCreate,
    RadiusProfileUpdate,
    RegionZoneCreate,
    RegionZoneUpdate,
    SlaProfileCreate,
    SlaProfileUpdate,
    SubscriptionAddOnCreate,
    SubscriptionAddOnUpdate,
    SubscriptionCreate,
    SubscriptionUpdate,
    UsageAllowanceCreate,
    UsageAllowanceUpdate,
    ValidationAddOnRequest,
)
from app.services import catalog as catalog_service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_offer(db, **overrides):
    """Create a catalog offer with sensible defaults."""
    defaults = dict(
        name=f"Test Offer {uuid.uuid4().hex[:6]}",
        code=f"TST-{uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    defaults.update(overrides)
    return catalog_service.offers.create(db, CatalogOfferCreate(**defaults))


def _make_addon(db, **overrides):
    """Create an add-on with sensible defaults."""
    defaults = dict(
        name=f"Test Addon {uuid.uuid4().hex[:6]}",
        addon_type=AddOnType.custom,
    )
    defaults.update(overrides)
    return catalog_service.add_ons.create(db, AddOnCreate(**defaults))


def _make_radius_profile(db, **overrides):
    """Create a RADIUS profile with sensible defaults."""
    defaults = dict(
        name=f"Test RP {uuid.uuid4().hex[:6]}",
        vendor=NasVendor.mikrotik,
    )
    defaults.update(overrides)
    return catalog_service.radius_profiles.create(db, RadiusProfileCreate(**defaults))


# ===========================================================================
# Offer CRUD
# ===========================================================================


class TestOffersCRUD:
    def test_create_offer(self, db_session):
        offer = _make_offer(db_session, name="Basic Fiber 50")
        assert offer.id is not None
        assert offer.name == "Basic Fiber 50"
        assert offer.service_type == ServiceType.residential
        assert offer.is_active is True

    def test_get_offer(self, db_session):
        offer = _make_offer(db_session)
        fetched = catalog_service.offers.get(db_session, str(offer.id))
        assert fetched.id == offer.id

    def test_get_offer_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offers.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_offers(self, db_session):
        _make_offer(db_session, name="Offer A")
        _make_offer(db_session, name="Offer B")
        items = catalog_service.offers.list(
            db_session,
            service_type=None,
            access_type=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 2

    def test_list_offers_filter_by_service_type(self, db_session):
        _make_offer(db_session, service_type=ServiceType.residential)
        _make_offer(db_session, service_type=ServiceType.business)
        residential = catalog_service.offers.list(
            db_session,
            service_type="residential",
            access_type=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        for o in residential:
            assert o.service_type == ServiceType.residential

    def test_update_offer(self, db_session):
        offer = _make_offer(db_session)
        updated = catalog_service.offers.update(
            db_session,
            str(offer.id),
            CatalogOfferUpdate(name="Updated Offer Name"),
        )
        assert updated.name == "Updated Offer Name"

    def test_update_offer_status_archived_syncs_is_active(self, db_session):
        offer = _make_offer(db_session)
        assert offer.is_active is True
        updated = catalog_service.offers.update(
            db_session,
            str(offer.id),
            CatalogOfferUpdate(status=OfferStatus.archived),
        )
        assert updated.status == OfferStatus.archived
        assert updated.is_active is False

    def test_update_offer_status_inactive_syncs_is_active(self, db_session):
        offer = _make_offer(db_session)
        updated = catalog_service.offers.update(
            db_session,
            str(offer.id),
            CatalogOfferUpdate(status=OfferStatus.inactive),
        )
        assert updated.status == OfferStatus.inactive
        assert updated.is_active is False

    def test_delete_offer_soft(self, db_session):
        offer = _make_offer(db_session)
        catalog_service.offers.delete(db_session, str(offer.id))
        db_session.refresh(offer)
        assert offer.is_active is False
        assert offer.status == OfferStatus.archived

    def test_delete_offer_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offers.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ===========================================================================
# Offer Prices
# ===========================================================================


class TestOfferPrices:
    def test_create_offer_price(self, db_session):
        offer = _make_offer(db_session)
        price = catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("5000.00"),
                currency="NGN",
            ),
        )
        assert price.id is not None
        assert price.amount == Decimal("5000.00")

    def test_get_offer_price(self, db_session):
        offer = _make_offer(db_session)
        price = catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(offer_id=offer.id, amount=Decimal("100")),
        )
        fetched = catalog_service.offer_prices.get(db_session, str(price.id))
        assert fetched.id == price.id

    def test_list_offer_prices(self, db_session):
        offer = _make_offer(db_session)
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(offer_id=offer.id, amount=Decimal("100")),
        )
        items = catalog_service.offer_prices.list(
            db_session,
            offer_id=str(offer.id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_update_offer_price(self, db_session):
        offer = _make_offer(db_session)
        price = catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(offer_id=offer.id, amount=Decimal("100")),
        )
        updated = catalog_service.offer_prices.update(
            db_session,
            str(price.id),
            OfferPriceUpdate(amount=Decimal("200")),
        )
        assert updated.amount == Decimal("200")

    def test_delete_offer_price_soft(self, db_session):
        offer = _make_offer(db_session)
        price = catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(offer_id=offer.id, amount=Decimal("100")),
        )
        catalog_service.offer_prices.delete(db_session, str(price.id))
        db_session.refresh(price)
        assert price.is_active is False


# ===========================================================================
# Offer Versions
# ===========================================================================


class TestOfferVersions:
    def test_create_offer_version(self, db_session):
        offer = _make_offer(db_session)
        version = catalog_service.offer_versions.create(
            db_session,
            OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="Fiber 50 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        assert version.id is not None
        assert version.version_number == 1

    def test_create_offer_version_offer_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offer_versions.create(
                db_session,
                OfferVersionCreate(
                    offer_id=uuid.uuid4(),
                    version_number=1,
                    name="Ghost v1",
                    service_type=ServiceType.residential,
                    access_type=AccessType.fiber,
                    price_basis=PriceBasis.flat,
                ),
            )
        assert exc_info.value.status_code == 404

    def test_list_offer_versions(self, db_session):
        offer = _make_offer(db_session)
        catalog_service.offer_versions.create(
            db_session,
            OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        items = catalog_service.offer_versions.list(
            db_session,
            offer_id=str(offer.id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_update_offer_version(self, db_session):
        offer = _make_offer(db_session)
        version = catalog_service.offer_versions.create(
            db_session,
            OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        updated = catalog_service.offer_versions.update(
            db_session,
            str(version.id),
            OfferVersionUpdate(name="v1-updated"),
        )
        assert updated.name == "v1-updated"

    def test_delete_offer_version_soft(self, db_session):
        offer = _make_offer(db_session)
        version = catalog_service.offer_versions.create(
            db_session,
            OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        catalog_service.offer_versions.delete(db_session, str(version.id))
        db_session.refresh(version)
        assert version.is_active is False


# ===========================================================================
# Offer Version Prices
# ===========================================================================


class TestOfferVersionPrices:
    def test_create_offer_version_price(self, db_session):
        offer = _make_offer(db_session)
        version = catalog_service.offer_versions.create(
            db_session,
            OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        price = catalog_service.offer_version_prices.create(
            db_session,
            OfferVersionPriceCreate(
                offer_version_id=version.id,
                amount=Decimal("3000"),
            ),
        )
        assert price.id is not None
        assert price.amount == Decimal("3000")

    def test_create_offer_version_price_version_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offer_version_prices.create(
                db_session,
                OfferVersionPriceCreate(
                    offer_version_id=uuid.uuid4(),
                    amount=Decimal("100"),
                ),
            )
        assert exc_info.value.status_code == 404

    def test_update_offer_version_price(self, db_session):
        offer = _make_offer(db_session)
        version = catalog_service.offer_versions.create(
            db_session,
            OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        price = catalog_service.offer_version_prices.create(
            db_session,
            OfferVersionPriceCreate(
                offer_version_id=version.id,
                amount=Decimal("100"),
            ),
        )
        updated = catalog_service.offer_version_prices.update(
            db_session,
            str(price.id),
            OfferVersionPriceUpdate(amount=Decimal("250")),
        )
        assert updated.amount == Decimal("250")


# ===========================================================================
# Add-on CRUD
# ===========================================================================


class TestAddOnsCRUD:
    def test_create_addon(self, db_session):
        addon = _make_addon(db_session, name="Static IP")
        assert addon.id is not None
        assert addon.name == "Static IP"

    def test_get_addon(self, db_session):
        addon = _make_addon(db_session)
        fetched = catalog_service.add_ons.get(db_session, str(addon.id))
        assert fetched.id == addon.id

    def test_get_addon_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.add_ons.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_addons(self, db_session):
        _make_addon(db_session)
        items = catalog_service.add_ons.list(
            db_session,
            is_active=None,
            addon_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_list_addons_filter_by_type(self, db_session):
        _make_addon(db_session, addon_type=AddOnType.static_ip)
        _make_addon(db_session, addon_type=AddOnType.router_rental)
        items = catalog_service.add_ons.list(
            db_session,
            is_active=None,
            addon_type="static_ip",
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        for item in items:
            assert item.addon_type == AddOnType.static_ip

    def test_update_addon(self, db_session):
        addon = _make_addon(db_session)
        updated = catalog_service.add_ons.update(
            db_session,
            str(addon.id),
            AddOnUpdate(name="Updated Addon"),
        )
        assert updated.name == "Updated Addon"

    def test_delete_addon_soft(self, db_session):
        addon = _make_addon(db_session)
        catalog_service.add_ons.delete(db_session, str(addon.id))
        db_session.refresh(addon)
        assert addon.is_active is False


# ===========================================================================
# Add-on Prices
# ===========================================================================


class TestAddOnPrices:
    def test_create_addon_price(self, db_session):
        addon = _make_addon(db_session)
        price = catalog_service.add_on_prices.create(
            db_session,
            AddOnPriceCreate(
                add_on_id=addon.id,
                amount=Decimal("500"),
            ),
        )
        assert price.id is not None
        assert price.amount == Decimal("500")

    def test_list_addon_prices(self, db_session):
        addon = _make_addon(db_session)
        catalog_service.add_on_prices.create(
            db_session,
            AddOnPriceCreate(add_on_id=addon.id, amount=Decimal("500")),
        )
        items = catalog_service.add_on_prices.list(
            db_session,
            add_on_id=str(addon.id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_update_addon_price(self, db_session):
        addon = _make_addon(db_session)
        price = catalog_service.add_on_prices.create(
            db_session,
            AddOnPriceCreate(add_on_id=addon.id, amount=Decimal("500")),
        )
        updated = catalog_service.add_on_prices.update(
            db_session,
            str(price.id),
            AddOnPriceUpdate(amount=Decimal("750")),
        )
        assert updated.amount == Decimal("750")

    def test_delete_addon_price_soft(self, db_session):
        addon = _make_addon(db_session)
        price = catalog_service.add_on_prices.create(
            db_session,
            AddOnPriceCreate(add_on_id=addon.id, amount=Decimal("500")),
        )
        catalog_service.add_on_prices.delete(db_session, str(price.id))
        db_session.refresh(price)
        assert price.is_active is False


# ===========================================================================
# Profile CRUD (RegionZone, UsageAllowance, SlaProfile)
# ===========================================================================


class TestRegionZones:
    def test_create_region_zone(self, db_session):
        rz = catalog_service.region_zones.create(
            db_session, RegionZoneCreate(name="Central Region", code="CR")
        )
        assert rz.id is not None
        assert rz.name == "Central Region"

    def test_get_region_zone(self, db_session):
        rz = catalog_service.region_zones.create(
            db_session, RegionZoneCreate(name="North Region")
        )
        fetched = catalog_service.region_zones.get(db_session, str(rz.id))
        assert fetched.id == rz.id

    def test_get_region_zone_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.region_zones.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_region_zones(self, db_session):
        catalog_service.region_zones.create(db_session, RegionZoneCreate(name="South"))
        items = catalog_service.region_zones.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_update_region_zone(self, db_session):
        rz = catalog_service.region_zones.create(
            db_session, RegionZoneCreate(name="East")
        )
        updated = catalog_service.region_zones.update(
            db_session, str(rz.id), RegionZoneUpdate(name="East Updated")
        )
        assert updated.name == "East Updated"

    def test_delete_region_zone_soft(self, db_session):
        rz = catalog_service.region_zones.create(
            db_session, RegionZoneCreate(name="West")
        )
        catalog_service.region_zones.delete(db_session, str(rz.id))
        db_session.refresh(rz)
        assert rz.is_active is False


class TestUsageAllowances:
    def test_create_usage_allowance(self, db_session):
        ua = catalog_service.usage_allowances.create(
            db_session,
            UsageAllowanceCreate(name="100GB Plan", included_gb=100),
        )
        assert ua.id is not None
        assert ua.included_gb == 100

    def test_get_usage_allowance(self, db_session):
        ua = catalog_service.usage_allowances.create(
            db_session, UsageAllowanceCreate(name="50GB Plan")
        )
        fetched = catalog_service.usage_allowances.get(db_session, str(ua.id))
        assert fetched.id == ua.id

    def test_list_usage_allowances(self, db_session):
        catalog_service.usage_allowances.create(
            db_session, UsageAllowanceCreate(name="Unlimited")
        )
        items = catalog_service.usage_allowances.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_update_usage_allowance(self, db_session):
        ua = catalog_service.usage_allowances.create(
            db_session, UsageAllowanceCreate(name="Old Name")
        )
        updated = catalog_service.usage_allowances.update(
            db_session, str(ua.id), UsageAllowanceUpdate(name="New Name")
        )
        assert updated.name == "New Name"

    def test_delete_usage_allowance_soft(self, db_session):
        ua = catalog_service.usage_allowances.create(
            db_session, UsageAllowanceCreate(name="ToDelete")
        )
        catalog_service.usage_allowances.delete(db_session, str(ua.id))
        db_session.refresh(ua)
        assert ua.is_active is False


class TestSlaProfiles:
    def test_create_sla_profile(self, db_session):
        sla = catalog_service.sla_profiles.create(
            db_session,
            SlaProfileCreate(
                name="Gold SLA",
                uptime_percent=Decimal("99.9"),
                response_time_hours=4,
            ),
        )
        assert sla.id is not None
        assert sla.name == "Gold SLA"

    def test_get_sla_profile(self, db_session):
        sla = catalog_service.sla_profiles.create(
            db_session, SlaProfileCreate(name="Silver SLA")
        )
        fetched = catalog_service.sla_profiles.get(db_session, str(sla.id))
        assert fetched.id == sla.id

    def test_list_sla_profiles(self, db_session):
        catalog_service.sla_profiles.create(
            db_session, SlaProfileCreate(name="Basic SLA")
        )
        items = catalog_service.sla_profiles.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_update_sla_profile(self, db_session):
        sla = catalog_service.sla_profiles.create(
            db_session, SlaProfileCreate(name="Bronze SLA")
        )
        updated = catalog_service.sla_profiles.update(
            db_session, str(sla.id), SlaProfileUpdate(name="Platinum SLA")
        )
        assert updated.name == "Platinum SLA"

    def test_delete_sla_profile_soft(self, db_session):
        sla = catalog_service.sla_profiles.create(
            db_session, SlaProfileCreate(name="Temp SLA")
        )
        catalog_service.sla_profiles.delete(db_session, str(sla.id))
        db_session.refresh(sla)
        assert sla.is_active is False


# ===========================================================================
# RADIUS Profiles
# ===========================================================================


class TestRadiusProfiles:
    def test_create_radius_profile(self, db_session):
        profile = _make_radius_profile(db_session, name="10Mbps PPPoE")
        assert profile.id is not None
        assert profile.name == "10Mbps PPPoE"
        assert profile.vendor == NasVendor.mikrotik

    def test_get_radius_profile(self, db_session):
        profile = _make_radius_profile(db_session)
        fetched = catalog_service.radius_profiles.get(db_session, str(profile.id))
        assert fetched.id == profile.id

    def test_get_radius_profile_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.radius_profiles.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_radius_profiles(self, db_session):
        _make_radius_profile(db_session)
        items = catalog_service.radius_profiles.list(
            db_session,
            vendor=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_list_radius_profiles_filter_vendor(self, db_session):
        _make_radius_profile(db_session, vendor=NasVendor.mikrotik)
        _make_radius_profile(db_session, vendor=NasVendor.huawei)
        items = catalog_service.radius_profiles.list(
            db_session,
            vendor="mikrotik",
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        for item in items:
            assert item.vendor == NasVendor.mikrotik

    def test_update_radius_profile(self, db_session):
        profile = _make_radius_profile(db_session)
        updated = catalog_service.radius_profiles.update(
            db_session,
            str(profile.id),
            RadiusProfileUpdate(name="Updated RP"),
        )
        assert updated.name == "Updated RP"

    def test_delete_radius_profile_soft(self, db_session):
        profile = _make_radius_profile(db_session)
        catalog_service.radius_profiles.delete(db_session, str(profile.id))
        db_session.refresh(profile)
        assert profile.is_active is False


# ===========================================================================
# RADIUS Attributes
# ===========================================================================


class TestRadiusAttributes:
    def test_create_radius_attribute(self, db_session):
        profile = _make_radius_profile(db_session)
        attr = catalog_service.radius_attributes.create(
            db_session,
            RadiusAttributeCreate(
                profile_id=profile.id,
                attribute="Mikrotik-Rate-Limit",
                operator=":=",
                value="10M/5M",
            ),
        )
        assert attr.id is not None
        assert attr.attribute == "Mikrotik-Rate-Limit"

    def test_create_radius_attribute_profile_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.radius_attributes.create(
                db_session,
                RadiusAttributeCreate(
                    profile_id=uuid.uuid4(),
                    attribute="Test-Attr",
                    value="test",
                ),
            )
        assert exc_info.value.status_code == 404

    def test_get_radius_attribute(self, db_session):
        profile = _make_radius_profile(db_session)
        attr = catalog_service.radius_attributes.create(
            db_session,
            RadiusAttributeCreate(
                profile_id=profile.id,
                attribute="Session-Timeout",
                value="3600",
            ),
        )
        fetched = catalog_service.radius_attributes.get(db_session, str(attr.id))
        assert fetched.id == attr.id

    def test_list_radius_attributes(self, db_session):
        profile = _make_radius_profile(db_session)
        catalog_service.radius_attributes.create(
            db_session,
            RadiusAttributeCreate(
                profile_id=profile.id, attribute="Attr-1", value="v1"
            ),
        )
        catalog_service.radius_attributes.create(
            db_session,
            RadiusAttributeCreate(
                profile_id=profile.id, attribute="Attr-2", value="v2"
            ),
        )
        items = catalog_service.radius_attributes.list(
            db_session,
            profile_id=str(profile.id),
            order_by="attribute",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 2

    def test_update_radius_attribute(self, db_session):
        profile = _make_radius_profile(db_session)
        attr = catalog_service.radius_attributes.create(
            db_session,
            RadiusAttributeCreate(profile_id=profile.id, attribute="Attr", value="old"),
        )
        updated = catalog_service.radius_attributes.update(
            db_session,
            str(attr.id),
            RadiusAttributeUpdate(value="new"),
        )
        assert updated.value == "new"

    def test_update_radius_attribute_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.radius_attributes.update(
                db_session,
                str(uuid.uuid4()),
                RadiusAttributeUpdate(value="x"),
            )
        assert exc_info.value.status_code == 404

    def test_delete_radius_attribute(self, db_session):
        profile = _make_radius_profile(db_session)
        attr = catalog_service.radius_attributes.create(
            db_session,
            RadiusAttributeCreate(
                profile_id=profile.id, attribute="ToDelete", value="del"
            ),
        )
        catalog_service.radius_attributes.delete(db_session, str(attr.id))
        with pytest.raises(HTTPException):
            catalog_service.radius_attributes.get(db_session, str(attr.id))


# ===========================================================================
# Offer-RADIUS Profile Links
# ===========================================================================


class TestOfferRadiusProfiles:
    def test_create_offer_radius_profile_link(self, db_session):
        offer = _make_offer(db_session)
        profile = _make_radius_profile(db_session)
        link = catalog_service.offer_radius_profiles.create(
            db_session,
            OfferRadiusProfileCreate(offer_id=offer.id, profile_id=profile.id),
        )
        assert link.id is not None

    def test_create_offer_radius_profile_offer_not_found(self, db_session):
        profile = _make_radius_profile(db_session)
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offer_radius_profiles.create(
                db_session,
                OfferRadiusProfileCreate(offer_id=uuid.uuid4(), profile_id=profile.id),
            )
        assert exc_info.value.status_code == 404

    def test_create_offer_radius_profile_profile_not_found(self, db_session):
        offer = _make_offer(db_session)
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offer_radius_profiles.create(
                db_session,
                OfferRadiusProfileCreate(offer_id=offer.id, profile_id=uuid.uuid4()),
            )
        assert exc_info.value.status_code == 404

    def test_list_offer_radius_profiles(self, db_session):
        offer = _make_offer(db_session)
        profile = _make_radius_profile(db_session)
        catalog_service.offer_radius_profiles.create(
            db_session,
            OfferRadiusProfileCreate(offer_id=offer.id, profile_id=profile.id),
        )
        items = catalog_service.offer_radius_profiles.list(
            db_session,
            offer_id=str(offer.id),
            profile_id=None,
            order_by="offer_id",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_delete_offer_radius_profile(self, db_session):
        offer = _make_offer(db_session)
        profile = _make_radius_profile(db_session)
        link = catalog_service.offer_radius_profiles.create(
            db_session,
            OfferRadiusProfileCreate(offer_id=offer.id, profile_id=profile.id),
        )
        catalog_service.offer_radius_profiles.delete(db_session, str(link.id))
        with pytest.raises(HTTPException):
            catalog_service.offer_radius_profiles.get(db_session, str(link.id))


# ===========================================================================
# Access Credentials
# ===========================================================================


class TestAccessCredentials:
    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_create_access_credential(self, mock_sync, db_session, subscriber):
        cred = catalog_service.access_credentials.create(
            db_session,
            AccessCredentialCreate(
                subscriber_id=subscriber.id,
                username=f"pppuser-{uuid.uuid4().hex[:8]}",
                secret_hash="hashed123",
            ),
        )
        assert cred.id is not None
        assert cred.is_active is True
        mock_sync.assert_called_once()

    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_create_credential_subscriber_not_found(self, mock_sync, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.access_credentials.create(
                db_session,
                AccessCredentialCreate(
                    subscriber_id=uuid.uuid4(),
                    username="ghost-user",
                ),
            )
        assert exc_info.value.status_code == 404

    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_create_credential_with_radius_profile(
        self, mock_sync, db_session, subscriber
    ):
        profile = _make_radius_profile(db_session)
        cred = catalog_service.access_credentials.create(
            db_session,
            AccessCredentialCreate(
                subscriber_id=subscriber.id,
                username=f"raduser-{uuid.uuid4().hex[:8]}",
                radius_profile_id=profile.id,
            ),
        )
        assert cred.radius_profile_id == profile.id

    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_create_credential_radius_profile_not_found(
        self, mock_sync, db_session, subscriber
    ):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.access_credentials.create(
                db_session,
                AccessCredentialCreate(
                    subscriber_id=subscriber.id,
                    username="bad-profile-user",
                    radius_profile_id=uuid.uuid4(),
                ),
            )
        assert exc_info.value.status_code == 404

    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_get_access_credential(self, mock_sync, db_session, subscriber):
        cred = catalog_service.access_credentials.create(
            db_session,
            AccessCredentialCreate(
                subscriber_id=subscriber.id,
                username=f"getuser-{uuid.uuid4().hex[:8]}",
            ),
        )
        fetched = catalog_service.access_credentials.get(db_session, str(cred.id))
        assert fetched.id == cred.id

    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_list_access_credentials(self, mock_sync, db_session, subscriber):
        catalog_service.access_credentials.create(
            db_session,
            AccessCredentialCreate(
                subscriber_id=subscriber.id,
                username=f"listuser-{uuid.uuid4().hex[:8]}",
            ),
        )
        items = catalog_service.access_credentials.list(
            db_session,
            subscriber_id=str(subscriber.id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_update_access_credential(self, mock_sync, db_session, subscriber):
        cred = catalog_service.access_credentials.create(
            db_session,
            AccessCredentialCreate(
                subscriber_id=subscriber.id,
                username=f"upduser-{uuid.uuid4().hex[:8]}",
            ),
        )
        updated = catalog_service.access_credentials.update(
            db_session,
            str(cred.id),
            AccessCredentialUpdate(secret_hash="newhash"),
        )
        assert updated.secret_hash == "newhash"
        # Called once on create, once on update
        assert mock_sync.call_count == 2

    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_delete_access_credential_soft(self, mock_sync, db_session, subscriber):
        cred = catalog_service.access_credentials.create(
            db_session,
            AccessCredentialCreate(
                subscriber_id=subscriber.id,
                username=f"deluser-{uuid.uuid4().hex[:8]}",
            ),
        )
        catalog_service.access_credentials.delete(db_session, str(cred.id))
        db_session.refresh(cred)
        assert cred.is_active is False


# ===========================================================================
# Policy Sets
# ===========================================================================


class TestPolicySets:
    def test_create_policy_set(self, db_session):
        policy = catalog_service.policy_sets.create(
            db_session,
            PolicySetCreate(
                name="Standard Policy",
                proration_policy=ProrationPolicy.immediate,
                grace_days=5,
            ),
        )
        assert policy.id is not None
        assert policy.name == "Standard Policy"
        assert policy.grace_days == 5

    def test_get_policy_set(self, db_session):
        policy = catalog_service.policy_sets.create(
            db_session, PolicySetCreate(name="Get Policy")
        )
        fetched = catalog_service.policy_sets.get(db_session, str(policy.id))
        assert fetched.id == policy.id

    def test_get_policy_set_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.policy_sets.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_policy_sets(self, db_session):
        catalog_service.policy_sets.create(
            db_session, PolicySetCreate(name="List Policy")
        )
        items = catalog_service.policy_sets.list(
            db_session,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_update_policy_set(self, db_session):
        policy = catalog_service.policy_sets.create(
            db_session, PolicySetCreate(name="Old Policy")
        )
        updated = catalog_service.policy_sets.update(
            db_session,
            str(policy.id),
            PolicySetUpdate(name="New Policy", trial_days=14),
        )
        assert updated.name == "New Policy"
        assert updated.trial_days == 14

    def test_delete_policy_set_soft(self, db_session):
        policy = catalog_service.policy_sets.create(
            db_session, PolicySetCreate(name="Delete Policy")
        )
        catalog_service.policy_sets.delete(db_session, str(policy.id))
        db_session.refresh(policy)
        assert policy.is_active is False

    def test_delete_policy_set_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.policy_sets.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ===========================================================================
# Policy Dunning Steps
# ===========================================================================


class TestPolicyDunningSteps:
    def test_create_dunning_step(self, db_session):
        policy = catalog_service.policy_sets.create(
            db_session, PolicySetCreate(name="Dunning Policy")
        )
        step = catalog_service.policy_dunning_steps.create(
            db_session,
            PolicyDunningStepCreate(
                policy_set_id=policy.id,
                day_offset=7,
                action=DunningAction.notify,
                note="First reminder",
            ),
        )
        assert step.id is not None
        assert step.day_offset == 7
        assert step.action == DunningAction.notify

    def test_create_dunning_step_policy_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.policy_dunning_steps.create(
                db_session,
                PolicyDunningStepCreate(
                    policy_set_id=uuid.uuid4(),
                    day_offset=1,
                    action=DunningAction.suspend,
                ),
            )
        assert exc_info.value.status_code == 404

    def test_get_dunning_step(self, db_session):
        policy = catalog_service.policy_sets.create(
            db_session, PolicySetCreate(name="DS Get Policy")
        )
        step = catalog_service.policy_dunning_steps.create(
            db_session,
            PolicyDunningStepCreate(
                policy_set_id=policy.id,
                day_offset=14,
                action=DunningAction.throttle,
            ),
        )
        fetched = catalog_service.policy_dunning_steps.get(db_session, str(step.id))
        assert fetched.id == step.id

    def test_list_dunning_steps(self, db_session):
        policy = catalog_service.policy_sets.create(
            db_session, PolicySetCreate(name="DS List Policy")
        )
        catalog_service.policy_dunning_steps.create(
            db_session,
            PolicyDunningStepCreate(
                policy_set_id=policy.id,
                day_offset=7,
                action=DunningAction.notify,
            ),
        )
        catalog_service.policy_dunning_steps.create(
            db_session,
            PolicyDunningStepCreate(
                policy_set_id=policy.id,
                day_offset=30,
                action=DunningAction.suspend,
            ),
        )
        items = catalog_service.policy_dunning_steps.list(
            db_session,
            policy_set_id=str(policy.id),
            order_by="day_offset",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 2
        assert items[0].day_offset <= items[1].day_offset

    def test_update_dunning_step(self, db_session):
        policy = catalog_service.policy_sets.create(
            db_session, PolicySetCreate(name="DS Upd Policy")
        )
        step = catalog_service.policy_dunning_steps.create(
            db_session,
            PolicyDunningStepCreate(
                policy_set_id=policy.id,
                day_offset=7,
                action=DunningAction.notify,
            ),
        )
        updated = catalog_service.policy_dunning_steps.update(
            db_session,
            str(step.id),
            PolicyDunningStepUpdate(day_offset=10),
        )
        assert updated.day_offset == 10

    def test_delete_dunning_step(self, db_session):
        policy = catalog_service.policy_sets.create(
            db_session, PolicySetCreate(name="DS Del Policy")
        )
        step = catalog_service.policy_dunning_steps.create(
            db_session,
            PolicyDunningStepCreate(
                policy_set_id=policy.id,
                day_offset=7,
                action=DunningAction.reject,
            ),
        )
        catalog_service.policy_dunning_steps.delete(db_session, str(step.id))
        with pytest.raises(HTTPException):
            catalog_service.policy_dunning_steps.get(db_session, str(step.id))


# ===========================================================================
# Offer-AddOn Linking
# ===========================================================================


class TestOfferAddOnLinks:
    def test_create_offer_addon_link(self, db_session):
        offer = _make_offer(db_session)
        addon = _make_addon(db_session)
        link = catalog_service.offer_addons.create(
            db_session,
            offer_id=str(offer.id),
            add_on_id=str(addon.id),
            is_required=True,
            min_quantity=1,
            max_quantity=5,
        )
        assert link.id is not None
        assert link.is_required is True
        assert link.min_quantity == 1
        assert link.max_quantity == 5

    def test_create_offer_addon_link_offer_not_found(self, db_session):
        addon = _make_addon(db_session)
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offer_addons.create(
                db_session,
                offer_id=str(uuid.uuid4()),
                add_on_id=str(addon.id),
            )
        assert exc_info.value.status_code == 404

    def test_create_offer_addon_link_addon_not_found(self, db_session):
        offer = _make_offer(db_session)
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offer_addons.create(
                db_session,
                offer_id=str(offer.id),
                add_on_id=str(uuid.uuid4()),
            )
        assert exc_info.value.status_code == 404

    def test_create_duplicate_link_raises(self, db_session):
        offer = _make_offer(db_session)
        addon = _make_addon(db_session)
        catalog_service.offer_addons.create(
            db_session,
            offer_id=str(offer.id),
            add_on_id=str(addon.id),
        )
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offer_addons.create(
                db_session,
                offer_id=str(offer.id),
                add_on_id=str(addon.id),
            )
        assert exc_info.value.status_code == 400

    def test_get_offer_addon_link(self, db_session):
        offer = _make_offer(db_session)
        addon = _make_addon(db_session)
        link = catalog_service.offer_addons.create(
            db_session,
            offer_id=str(offer.id),
            add_on_id=str(addon.id),
        )
        fetched = catalog_service.offer_addons.get(db_session, str(link.id))
        assert fetched.id == link.id

    def test_get_by_offer_and_addon(self, db_session):
        offer = _make_offer(db_session)
        addon = _make_addon(db_session)
        link = catalog_service.offer_addons.create(
            db_session,
            offer_id=str(offer.id),
            add_on_id=str(addon.id),
        )
        fetched = catalog_service.offer_addons.get_by_offer_and_addon(
            db_session, str(offer.id), str(addon.id)
        )
        assert fetched is not None
        assert fetched.id == link.id

    def test_get_by_offer_and_addon_not_found(self, db_session):
        result = catalog_service.offer_addons.get_by_offer_and_addon(
            db_session, str(uuid.uuid4()), str(uuid.uuid4())
        )
        assert result is None

    def test_list_offer_addon_links(self, db_session):
        offer = _make_offer(db_session)
        addon1 = _make_addon(db_session)
        addon2 = _make_addon(db_session)
        catalog_service.offer_addons.create(
            db_session, offer_id=str(offer.id), add_on_id=str(addon1.id)
        )
        catalog_service.offer_addons.create(
            db_session, offer_id=str(offer.id), add_on_id=str(addon2.id)
        )
        items = catalog_service.offer_addons.list(
            db_session,
            offer_id=str(offer.id),
        )
        assert len(items) >= 2

    def test_update_offer_addon_link(self, db_session):
        offer = _make_offer(db_session)
        addon = _make_addon(db_session)
        link = catalog_service.offer_addons.create(
            db_session,
            offer_id=str(offer.id),
            add_on_id=str(addon.id),
            is_required=False,
        )
        updated = catalog_service.offer_addons.update(
            db_session,
            str(link.id),
            is_required=True,
            max_quantity=10,
        )
        assert updated.is_required is True
        assert updated.max_quantity == 10

    def test_delete_offer_addon_link(self, db_session):
        offer = _make_offer(db_session)
        addon = _make_addon(db_session)
        link = catalog_service.offer_addons.create(
            db_session,
            offer_id=str(offer.id),
            add_on_id=str(addon.id),
        )
        result = catalog_service.offer_addons.delete(db_session, str(link.id))
        assert result is True

    def test_delete_nonexistent_link(self, db_session):
        result = catalog_service.offer_addons.delete(db_session, str(uuid.uuid4()))
        assert result is False

    def test_sync_offer_addons(self, db_session):
        offer = _make_offer(db_session)
        addon1 = _make_addon(db_session)
        addon2 = _make_addon(db_session)
        addon3 = _make_addon(db_session)

        # Initial sync with addon1 and addon2
        catalog_service.offer_addons.sync(
            db_session,
            offer_id=str(offer.id),
            addon_configs=[
                {"add_on_id": str(addon1.id), "is_required": True},
                {"add_on_id": str(addon2.id), "is_required": False, "max_quantity": 3},
            ],
        )
        links = catalog_service.offer_addons.list(db_session, offer_id=str(offer.id))
        assert len(links) == 2

        # Sync again: remove addon1, add addon3, keep addon2
        result = catalog_service.offer_addons.sync(
            db_session,
            offer_id=str(offer.id),
            addon_configs=[
                {"add_on_id": str(addon2.id), "is_required": True, "max_quantity": 5},
                {"add_on_id": str(addon3.id)},
            ],
        )
        assert len(result) == 2
        links_after = catalog_service.offer_addons.list(
            db_session, offer_id=str(offer.id)
        )
        addon_ids_after = {str(link.add_on_id) for link in links_after}
        assert str(addon1.id) not in addon_ids_after
        assert str(addon2.id) in addon_ids_after
        assert str(addon3.id) in addon_ids_after


# ===========================================================================
# Subscriptions
# ===========================================================================


class TestSubscriptions:
    def test_create_subscription_pending(self, db_session, subscriber, catalog_offer):
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.pending,
            ),
        )
        assert sub.id is not None
        assert sub.status == SubscriptionStatus.pending

    def test_create_rejects_account_offer_billing_mode_drift(
        self, db_session, subscriber
    ):
        subscriber.billing_mode = BillingMode.prepaid
        offer = _make_offer(db_session, billing_mode=BillingMode.postpaid)
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.create(
                db_session,
                SubscriptionCreate(subscriber_id=subscriber.id, offer_id=offer.id),
            )

        assert exc_info.value.status_code == 409
        assert "account_offer_billing_mode_mismatch" in exc_info.value.detail

    def test_create_rejects_explicit_billing_mode_override(
        self, db_session, subscriber, catalog_offer
    ):
        subscriber.billing_mode = BillingMode.prepaid
        catalog_offer.billing_mode = BillingMode.prepaid
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.create(
                db_session,
                SubscriptionCreate(
                    subscriber_id=subscriber.id,
                    offer_id=catalog_offer.id,
                    billing_mode=BillingMode.postpaid,
                ),
            )

        assert exc_info.value.status_code == 409
        assert "requested_billing_mode_mismatch" in exc_info.value.detail

    def test_update_rejects_billing_mode_drift(
        self, db_session, subscriber, catalog_offer
    ):
        subscriber.billing_mode = BillingMode.prepaid
        catalog_offer.billing_mode = BillingMode.prepaid
        db_session.commit()
        subscription = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
            ),
        )

        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.update(
                db_session,
                str(subscription.id),
                SubscriptionUpdate(billing_mode=BillingMode.postpaid),
            )

        assert exc_info.value.status_code == 409
        assert "requested_billing_mode_mismatch" in exc_info.value.detail

    def test_create_subscription_inherits_offer_radius_profile(
        self, db_session, subscriber
    ):
        offer = _make_offer(db_session)
        profile = _make_radius_profile(
            db_session, download_speed=50000, upload_speed=25000
        )
        catalog_service.offer_radius_profiles.create(
            db_session,
            OfferRadiusProfileCreate(offer_id=offer.id, profile_id=profile.id),
        )

        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=offer.id,
                status=SubscriptionStatus.pending,
            ),
        )

        assert sub.radius_profile_id == profile.id

    def test_create_subscription_active_sets_start_at(
        self, db_session, subscriber, catalog_offer
    ):
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
            ),
        )
        assert sub.status == SubscriptionStatus.active
        assert sub.start_at is not None
        assert sub.next_billing_at is not None

    def test_get_subscription(self, db_session, subscriber, catalog_offer):
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
            ),
        )
        fetched = catalog_service.subscriptions.get(db_session, str(sub.id))
        assert fetched.id == sub.id

    def test_get_subscription_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_subscriptions(self, db_session, subscriber, catalog_offer):
        catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
            ),
        )
        items = catalog_service.subscriptions.list(
            db_session,
            subscriber_id=str(subscriber.id),
            offer_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_generic_update_rejects_subscription_status(
        self, db_session, subscriber, catalog_offer
    ):
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.pending,
            ),
        )
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.update(
                db_session,
                str(sub.id),
                SubscriptionUpdate(status=SubscriptionStatus.active),
            )
        assert exc_info.value.status_code == 409
        assert "lifecycle fields are read-only" in str(exc_info.value.detail)
        db_session.refresh(sub)
        assert sub.status == SubscriptionStatus.pending

    def test_plan_change_clears_stale_offer_version_id(self, db_session, subscriber):
        """Changing offer drops the previous offer's pinned version so price
        resolution doesn't read the OLD plan's OfferVersionPrice."""
        first_offer = _make_offer(db_session, name="Plan A")
        second_offer = _make_offer(db_session, name="Plan B")
        version = catalog_service.offer_versions.create(
            db_session,
            OfferVersionCreate(
                offer_id=first_offer.id,
                version_number=1,
                name="Plan A v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=first_offer.id,
                offer_version_id=version.id,
                status=SubscriptionStatus.pending,
            ),
        )
        assert sub.offer_version_id == version.id

        updated = catalog_service.subscriptions.update(
            db_session,
            str(sub.id),
            SubscriptionUpdate(offer_id=second_offer.id),
        )

        assert updated.offer_id == second_offer.id
        assert updated.offer_version_id is None

    def test_update_preserves_existing_end_at(
        self, db_session, subscriber, catalog_offer
    ):
        """A routine update must not recompute end_at from start_at + contract
        term and resurrect a past contract-end as a customer-visible expiry."""
        from app.models.catalog import ContractTerm

        now = datetime.now(UTC)
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.pending,
                start_at=now - timedelta(days=400),
                contract_term=ContractTerm.twelve_month,
                # Explicit end_at deliberately later than start_at + 12 months
                # (which would land in the past).
                end_at=now + timedelta(days=330),
            ),
        )
        assert sub.end_at is not None

        # A routine update that does NOT supply end_at exercises the recompute
        # block (status-independent); pending keeps it off the activation path.
        updated = catalog_service.subscriptions.update(
            db_session,
            str(sub.id),
            SubscriptionUpdate(service_description="routine edit"),
        )

        end_at = updated.end_at
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=UTC)
        # Preserved (future), NOT recomputed to start_at + 12mo (~35 days ago).
        assert end_at > now

    def test_offer_change_rejects_account_billing_mode_drift(
        self, db_session, subscriber
    ):
        from app.models.catalog import BillingMode

        prepaid_offer = _make_offer(
            db_session, name="Prepaid", billing_mode=BillingMode.prepaid
        )
        postpaid_offer = _make_offer(
            db_session, name="Postpaid", billing_mode=BillingMode.postpaid
        )
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=prepaid_offer.id,
                billing_mode=BillingMode.prepaid,
                status=SubscriptionStatus.pending,
            ),
        )
        assert sub.billing_mode == BillingMode.prepaid

        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.update(
                db_session,
                str(sub.id),
                SubscriptionUpdate(offer_id=postpaid_offer.id),
            )

        assert exc_info.value.status_code == 409
        assert "account_offer_billing_mode_mismatch" in exc_info.value.detail
        db_session.refresh(sub)
        assert sub.offer_id == prepaid_offer.id
        assert sub.billing_mode == BillingMode.prepaid

    def test_proration_uses_effective_old_price_not_catalog(
        self, db_session, subscriber
    ):
        """Mid-cycle change credits the negotiated rate the customer pays, not
        the raw catalog price (which over-credited discounted/override subs)."""
        from app.services.catalog.subscriptions import _calculate_proration

        offer_a = _make_offer(db_session, name="Plan A")
        offer_b = _make_offer(db_session, name="Plan B")
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=offer_a.id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
            ),
        )
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=offer_b.id,
                price_type=PriceType.recurring,
                amount=Decimal("80.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
            ),
        )
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=offer_a.id,
                status=SubscriptionStatus.pending,
            ),
        )
        # Negotiated rate (₦50) below the ₦100 catalog price, plus a mid-cycle anchor.
        sub.unit_price = Decimal("50.00")
        sub.next_billing_at = datetime.now(UTC) + timedelta(days=15)
        db_session.flush()

        result = _calculate_proration(db_session, sub, str(offer_b.id))

        # Old side reflects the ₦50 the customer actually pays, not catalog ₦100.
        assert result["old_price"] == Decimal("50.00")
        assert result["new_price"] == Decimal("80.00")
        # Credit (on ₦50) is below the charge (on ₦80) for the same remaining ratio.
        assert result["credit_amount"] < result["charge_amount"]

    def test_update_subscription_offer_changes_inherited_radius_profile(
        self, db_session, subscriber
    ):
        first_offer = _make_offer(db_session, name="Plan A")
        second_offer = _make_offer(db_session, name="Plan B")
        first_profile = _make_radius_profile(db_session, name="Plan A Profile")
        second_profile = _make_radius_profile(db_session, name="Plan B Profile")
        catalog_service.offer_radius_profiles.create(
            db_session,
            OfferRadiusProfileCreate(
                offer_id=first_offer.id, profile_id=first_profile.id
            ),
        )
        catalog_service.offer_radius_profiles.create(
            db_session,
            OfferRadiusProfileCreate(
                offer_id=second_offer.id, profile_id=second_profile.id
            ),
        )

        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=first_offer.id,
                status=SubscriptionStatus.pending,
            ),
        )
        assert sub.radius_profile_id == first_profile.id

        updated = catalog_service.subscriptions.update(
            db_session,
            str(sub.id),
            SubscriptionUpdate(offer_id=second_offer.id),
        )

        assert updated.offer_id == second_offer.id
        assert updated.radius_profile_id == second_profile.id
        assert updated.start_at is not None

    def test_update_subscription_offer_changes_equivalent_legacy_radius_profile(
        self, db_session, subscriber
    ):
        first_offer = _make_offer(
            db_session,
            name="Unlimited Basic",
            speed_download_mbps=6,
            speed_upload_mbps=6,
        )
        second_offer = _make_offer(
            db_session,
            name="Unlimited Standard",
            speed_download_mbps=10,
            speed_upload_mbps=10,
        )
        first_profile = _make_radius_profile(
            db_session,
            name="Unlimited Basic Profile",
            download_speed=6000,
            upload_speed=6000,
        )
        legacy_profile = _make_radius_profile(
            db_session,
            name="6 Mbps Fiber",
            download_speed=6000,
            upload_speed=6000,
        )
        second_profile = _make_radius_profile(
            db_session,
            name="Unlimited Standard Profile",
            download_speed=10000,
            upload_speed=10000,
        )
        catalog_service.offer_radius_profiles.create(
            db_session,
            OfferRadiusProfileCreate(
                offer_id=first_offer.id, profile_id=first_profile.id
            ),
        )
        catalog_service.offer_radius_profiles.create(
            db_session,
            OfferRadiusProfileCreate(
                offer_id=second_offer.id, profile_id=second_profile.id
            ),
        )

        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=first_offer.id,
                status=SubscriptionStatus.pending,
                radius_profile_id=legacy_profile.id,
            ),
        )
        assert sub.radius_profile_id == legacy_profile.id

        updated = catalog_service.subscriptions.update(
            db_session,
            str(sub.id),
            SubscriptionUpdate(offer_id=second_offer.id),
        )

        assert updated.offer_id == second_offer.id
        assert updated.radius_profile_id == second_profile.id

    def test_update_subscription_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.update(
                db_session,
                str(uuid.uuid4()),
                SubscriptionUpdate(service_description="test"),
            )
        assert exc_info.value.status_code == 404

    def test_delete_subscription(self, db_session, subscriber, catalog_offer):
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
            ),
        )
        catalog_service.subscriptions.delete(db_session, str(sub.id))
        with pytest.raises(HTTPException):
            catalog_service.subscriptions.get(db_session, str(sub.id))

    def test_delete_subscription_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_enforce_single_active_subscription(
        self, db_session, subscriber, catalog_offer
    ):
        """Second active/pending subscription for same subscriber should fail."""
        catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.pending,
            ),
        )
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.create(
                db_session,
                SubscriptionCreate(
                    subscriber_id=subscriber.id,
                    offer_id=catalog_offer.id,
                    status=SubscriptionStatus.pending,
                ),
            )
        assert exc_info.value.status_code == 400
        assert "already has an active subscription" in str(exc_info.value.detail)

    def test_generic_update_rejects_cancellation_fields(
        self, db_session, subscriber, catalog_offer
    ):
        """Cancellation is owned by the subscription lifecycle command."""
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.pending,
            ),
        )
        now = datetime.now(UTC)
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscriptions.update(
                db_session,
                str(sub.id),
                SubscriptionUpdate(
                    status=SubscriptionStatus.canceled,
                    canceled_at=now,
                ),
            )
        assert exc_info.value.status_code == 409
        db_session.refresh(sub)
        assert sub.status == SubscriptionStatus.pending
        assert sub.canceled_at is None

    def test_terminal_status_cannot_be_resurrected(
        self, db_session, subscriber, catalog_offer
    ):
        """A canceled/disabled/expired subscription cannot be flipped back to
        active via the raw update path (terminal-state resurrection guard)."""
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.canceled,
                canceled_at=datetime.now(UTC),
            ),
        )

        for revived in (
            SubscriptionStatus.active,
            SubscriptionStatus.suspended,
            SubscriptionStatus.pending,
        ):
            with pytest.raises(HTTPException) as exc_info:
                catalog_service.subscriptions.update(
                    db_session,
                    str(sub.id),
                    SubscriptionUpdate(status=revived),
                )
            assert exc_info.value.status_code == 409
            assert "lifecycle fields are read-only" in str(exc_info.value.detail)

        # Re-fetch: status must still be terminal, untouched.
        db_session.refresh(sub)
        assert sub.status == SubscriptionStatus.canceled

    def test_terminal_status_noop_update_allowed(
        self, db_session, subscriber, catalog_offer
    ):
        """Editing a non-status field on a terminal subscription is allowed
        (no transition occurs)."""
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.canceled,
                canceled_at=datetime.now(UTC),
            ),
        )
        updated = catalog_service.subscriptions.update(
            db_session,
            str(sub.id),
            SubscriptionUpdate(service_description="closed account note"),
        )
        assert updated.status == SubscriptionStatus.canceled
        assert updated.service_description == "closed account note"

    def test_disabled_transition_resolves_locks_and_preserves_ips(
        self, db_session, subscriber, catalog_offer, monkeypatch
    ):
        """Disabled resolves locks but keeps service assignments for restore."""
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services import account_lifecycle as al

        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
            ),
        )
        lock = EnforcementLock(
            subscription_id=sub.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.overdue,
            source="test",
            is_active=True,
        )
        db_session.add(lock)
        db_session.flush()

        released = []
        monkeypatch.setattr(
            al,
            "_release_service_ips",
            lambda db, subscription: released.append(str(subscription.id)),
        )

        al.disable_subscription(
            db_session,
            str(sub.id),
            reason="pytest disable",
            source="pytest",
        )
        db_session.commit()

        db_session.refresh(lock)
        assert lock.is_active is False
        assert lock.resolved_at is not None
        assert released == []

    def test_expire_subscriptions(self, db_session, subscriber, catalog_offer):
        """Subscriptions past end_at should be expired."""
        now = datetime.now(UTC)
        past = now - timedelta(days=1)
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
                start_at=past - timedelta(days=30),
                end_at=past,
            ),
        )
        result = catalog_service.subscriptions.expire_subscriptions(
            db_session, run_at=now
        )
        assert result["subscriptions_expired"] >= 1
        db_session.refresh(sub)
        assert sub.status == SubscriptionStatus.expired

    def test_expire_subscriptions_dry_run(self, db_session, subscriber, catalog_offer):
        now = datetime.now(UTC)
        past = now - timedelta(days=1)
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
                start_at=past - timedelta(days=30),
                end_at=past,
            ),
        )
        result = catalog_service.subscriptions.expire_subscriptions(
            db_session, run_at=now, dry_run=True
        )
        assert result["subscriptions_expired"] >= 1
        assert result["dry_run"] is True
        db_session.refresh(sub)
        # Should remain active in dry run
        assert sub.status == SubscriptionStatus.active

    def test_subscription_contract_term_sets_end_at(
        self, db_session, subscriber, catalog_offer
    ):
        start = datetime.now(UTC)
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
                start_at=start,
                contract_term=ContractTerm.twelve_month,
            ),
        )
        assert sub.end_at is not None
        # end_at should be approximately 12 months from start
        delta = sub.end_at - start
        assert 360 <= delta.days <= 370


# ===========================================================================
# Subscription AddOns
# ===========================================================================


class TestSubscriptionAddOns:
    def test_create_subscription_addon(self, db_session, subscriber, catalog_offer):
        addon = _make_addon(db_session)
        # Link addon to offer first
        catalog_service.offer_addons.create(
            db_session,
            offer_id=str(catalog_offer.id),
            add_on_id=str(addon.id),
        )
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
            ),
        )
        sub_addon = catalog_service.subscription_add_ons.create(
            db_session,
            SubscriptionAddOnCreate(
                subscription_id=sub.id,
                add_on_id=addon.id,
                quantity=2,
            ),
        )
        assert sub_addon.id is not None
        assert sub_addon.quantity == 2

    def test_create_subscription_addon_not_linked_to_offer(
        self, db_session, subscriber, catalog_offer
    ):
        """Addon not linked to the subscription's offer should fail."""
        unlinked_addon = _make_addon(db_session)
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
            ),
        )
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.subscription_add_ons.create(
                db_session,
                SubscriptionAddOnCreate(
                    subscription_id=sub.id,
                    add_on_id=unlinked_addon.id,
                    quantity=1,
                ),
            )
        assert exc_info.value.status_code == 400

    def test_list_subscription_addons(self, db_session, subscriber, catalog_offer):
        addon = _make_addon(db_session)
        catalog_service.offer_addons.create(
            db_session,
            offer_id=str(catalog_offer.id),
            add_on_id=str(addon.id),
        )
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
            ),
        )
        catalog_service.subscription_add_ons.create(
            db_session,
            SubscriptionAddOnCreate(
                subscription_id=sub.id,
                add_on_id=addon.id,
            ),
        )
        items = catalog_service.subscription_add_ons.list(
            db_session,
            subscription_id=str(sub.id),
            add_on_id=None,
            order_by="start_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(items) >= 1

    def test_update_subscription_addon(self, db_session, subscriber, catalog_offer):
        addon = _make_addon(db_session)
        catalog_service.offer_addons.create(
            db_session,
            offer_id=str(catalog_offer.id),
            add_on_id=str(addon.id),
        )
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
            ),
        )
        sub_addon = catalog_service.subscription_add_ons.create(
            db_session,
            SubscriptionAddOnCreate(
                subscription_id=sub.id,
                add_on_id=addon.id,
                quantity=1,
            ),
        )
        updated = catalog_service.subscription_add_ons.update(
            db_session,
            str(sub_addon.id),
            SubscriptionAddOnUpdate(quantity=3),
        )
        assert updated.quantity == 3

    def test_delete_subscription_addon(self, db_session, subscriber, catalog_offer):
        addon = _make_addon(db_session)
        catalog_service.offer_addons.create(
            db_session,
            offer_id=str(catalog_offer.id),
            add_on_id=str(addon.id),
        )
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
            ),
        )
        sub_addon = catalog_service.subscription_add_ons.create(
            db_session,
            SubscriptionAddOnCreate(
                subscription_id=sub.id,
                add_on_id=addon.id,
            ),
        )
        catalog_service.subscription_add_ons.delete(db_session, str(sub_addon.id))
        with pytest.raises(HTTPException):
            catalog_service.subscription_add_ons.get(db_session, str(sub_addon.id))


# ===========================================================================
# Offer Validation
# ===========================================================================


class TestOfferValidation:
    def test_validate_active_offer(self, db_session):
        offer = _make_offer(db_session)
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("5000"),
            ),
        )
        result = catalog_service.offer_validation.validate(
            db_session,
            OfferValidationRequest(offer_id=offer.id),
        )
        assert result.valid is True
        assert result.recurring_total == Decimal("5000")
        assert len(result.prices) >= 1

    def test_validate_inactive_offer(self, db_session):
        offer = _make_offer(db_session, status=OfferStatus.inactive)
        result = catalog_service.offer_validation.validate(
            db_session,
            OfferValidationRequest(offer_id=offer.id),
        )
        assert result.valid is False
        assert any("not active" in e for e in result.errors)

    def test_validate_offer_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.offer_validation.validate(
                db_session,
                OfferValidationRequest(offer_id=uuid.uuid4()),
            )
        assert exc_info.value.status_code == 404

    def test_validate_offer_with_addon_prices(self, db_session):
        offer = _make_offer(db_session)
        addon = _make_addon(db_session)
        catalog_service.offer_addons.create(
            db_session,
            offer_id=str(offer.id),
            add_on_id=str(addon.id),
        )
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("5000"),
            ),
        )
        catalog_service.add_on_prices.create(
            db_session,
            AddOnPriceCreate(
                add_on_id=addon.id,
                price_type=PriceType.recurring,
                amount=Decimal("1000"),
            ),
        )
        result = catalog_service.offer_validation.validate(
            db_session,
            OfferValidationRequest(
                offer_id=offer.id,
                add_ons=[
                    ValidationAddOnRequest(add_on_id=addon.id, quantity=2),
                ],
            ),
        )
        assert result.valid is True
        # 5000 recurring from offer + 1000*2 from addon
        assert result.recurring_total == Decimal("7000")

    def test_validate_offer_missing_required_addon(self, db_session):
        offer = _make_offer(db_session)
        addon = _make_addon(db_session)
        catalog_service.offer_addons.create(
            db_session,
            offer_id=str(offer.id),
            add_on_id=str(addon.id),
            is_required=True,
        )
        result = catalog_service.offer_validation.validate(
            db_session,
            OfferValidationRequest(
                offer_id=offer.id,
                add_ons=[],  # missing required addon
            ),
        )
        assert result.valid is False
        assert any("required" in e.lower() for e in result.errors)

    def test_validate_with_one_time_and_usage_prices(self, db_session):
        offer = _make_offer(db_session)
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=offer.id,
                price_type=PriceType.one_time,
                amount=Decimal("1000"),
            ),
        )
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=offer.id,
                price_type=PriceType.usage,
                amount=Decimal("50"),
            ),
        )
        result = catalog_service.offer_validation.validate(
            db_session,
            OfferValidationRequest(offer_id=offer.id),
        )
        assert result.valid is True
        assert result.one_time_total == Decimal("1000")
        assert result.usage_total == Decimal("50")
        assert result.recurring_total == Decimal("0")


# ===========================================================================
# Helper function tests
# ===========================================================================


class TestSubscriptionHelpers:
    def test_compute_next_billing_at_monthly(self):
        from app.services.catalog.subscriptions import _compute_next_billing_at

        start = datetime(2025, 1, 15, tzinfo=UTC)
        next_billing = _compute_next_billing_at(start, BillingCycle.monthly)
        assert next_billing.month == 2
        assert next_billing.day == 15

    def test_compute_next_billing_at_annual(self):
        from app.services.catalog.subscriptions import _compute_next_billing_at

        start = datetime(2025, 3, 1, tzinfo=UTC)
        next_billing = _compute_next_billing_at(start, BillingCycle.annual)
        assert next_billing.year == 2026
        assert next_billing.month == 3

    def test_compute_next_billing_at_daily(self):
        from app.services.catalog.subscriptions import _compute_next_billing_at

        start = datetime(2025, 6, 10, tzinfo=UTC)
        next_billing = _compute_next_billing_at(start, BillingCycle.daily)
        assert next_billing.day == 11

    def test_compute_next_billing_at_weekly(self):
        from app.services.catalog.subscriptions import _compute_next_billing_at

        start = datetime(2025, 6, 10, tzinfo=UTC)
        next_billing = _compute_next_billing_at(start, BillingCycle.weekly)
        assert (next_billing - start).days == 7

    def test_compute_contract_end_at_twelve_month(self):
        from app.services.catalog.subscriptions import _compute_contract_end_at

        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = _compute_contract_end_at(start, ContractTerm.twelve_month)
        assert end is not None
        assert end.year == 2026
        assert end.month == 1

    def test_compute_contract_end_at_twentyfour_month(self):
        from app.services.catalog.subscriptions import _compute_contract_end_at

        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = _compute_contract_end_at(start, ContractTerm.twentyfour_month)
        assert end is not None
        assert end.year == 2027
        assert end.month == 1

    def test_compute_contract_end_at_month_to_month(self):
        from app.services.catalog.subscriptions import _compute_contract_end_at

        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = _compute_contract_end_at(start, ContractTerm.month_to_month)
        assert end is None

    def test_add_months_end_of_month(self):
        from app.services.catalog.subscriptions import _add_months

        # Jan 31 + 1 month should be Feb 28 (not Feb 31 which doesn't exist)
        dt = datetime(2025, 1, 31, tzinfo=UTC)
        result = _add_months(dt, 1)
        assert result.month == 2
        assert result.day == 28


class TestSubscriptionUnitPriceDefault:
    """create() snapshots the offer's recurring price into unit_price.

    Regression: subscriptions created without an explicit unit_price left the
    field null, so the admin/customer billing summaries showed "Price not set"
    and a ₦0 monthly recurring total even though invoices billed the real
    offer price.
    """

    def test_defaults_unit_price_from_offer_recurring_price(
        self, db_session, subscriber, catalog_offer
    ):
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=catalog_offer.id,
                amount=Decimal("15000"),
                price_type=PriceType.recurring,
            ),
        )
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(account_id=subscriber.id, offer_id=catalog_offer.id),
        )
        assert sub.unit_price == Decimal("15000")

    def test_explicit_unit_price_is_preserved(
        self, db_session, subscriber, catalog_offer
    ):
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=catalog_offer.id,
                amount=Decimal("15000"),
                price_type=PriceType.recurring,
            ),
        )
        sub = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                account_id=subscriber.id,
                offer_id=catalog_offer.id,
                unit_price=Decimal("9999"),
            ),
        )
        assert sub.unit_price == Decimal("9999")


class TestPlanChangePolicyFeeDirection:
    """The upgrade/downgrade fee direction must follow the EFFECTIVE prices the
    customer pays, not raw catalog prices (M2)."""

    def test_fee_direction_uses_effective_prices(self, db_session):
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType
        from app.services.catalog.subscriptions import _apply_plan_change_policy

        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="upgrade_fee",
                value_type=SettingValueType.string,
                value_text="10",
                is_active=True,
            )
        )
        db_session.commit()
        base = {"net_amount": Decimal("10.00"), "credit_amount": Decimal("0.00")}

        # Effective: negotiated ₦50 → new ₦80 is an UPGRADE → upgrade fee applies.
        up = _apply_plan_change_policy(
            db_session, dict(base), old_price=Decimal("50"), new_price=Decimal("80")
        )
        assert up.get("fee_amount") == Decimal("10.00")

        # Raw catalog ₦100 → ₦80 would read as a downgrade → no upgrade fee.
        down = _apply_plan_change_policy(
            db_session, dict(base), old_price=Decimal("100"), new_price=Decimal("80")
        )
        assert not down.get("fee_amount")


class TestPublicIpAddonPrefixMatch:
    def test_ambiguous_prefix_warns_and_returns_first(self, db_session, caplog):
        import logging

        from app.models.catalog import AddOn, AddOnPrice, AddOnType, PriceType
        from app.services.web_catalog_subscriptions import (
            _priced_public_ip_addon_by_prefix,
        )

        for name in ("AAA /29", "BBB /29"):
            add_on = AddOn(
                name=name,
                addon_type=AddOnType.extra_ip,
                is_active=True,
                ip_is_public=True,
                ip_prefix_length=29,
            )
            db_session.add(add_on)
            db_session.flush()
            db_session.add(
                AddOnPrice(
                    add_on_id=add_on.id,
                    price_type=PriceType.recurring,
                    amount=Decimal("25.00"),
                    currency="NGN",
                    is_active=True,
                )
            )
        db_session.commit()

        with caplog.at_level(logging.WARNING):
            result = _priced_public_ip_addon_by_prefix(db_session, 29)

        assert result is not None
        assert result.name == "AAA /29"  # deterministic (first by name)
        assert "ambiguous_public_ip_addon_prefix" in caplog.text
