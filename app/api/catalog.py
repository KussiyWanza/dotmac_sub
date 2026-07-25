from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.catalog import (
    AccessCredentialCreate,
    AccessCredentialRead,
    AccessCredentialUpdate,
    AddOnCreate,
    AddOnPriceCreate,
    AddOnPriceRead,
    AddOnPriceUpdate,
    AddOnRead,
    AddOnUpdate,
    CatalogOfferCreate,
    CatalogOfferRead,
    CatalogOfferUpdate,
    NasDeviceCreate,
    NasDeviceRead,
    NasDeviceUpdate,
    OfferPriceCreate,
    OfferPriceRead,
    OfferPriceUpdate,
    OfferRadiusProfileCreate,
    OfferRadiusProfileRead,
    OfferRadiusProfileUpdate,
    OfferValidationRequest,
    OfferValidationResponse,
    OfferVersionCreate,
    OfferVersionPriceCreate,
    OfferVersionPriceRead,
    OfferVersionPriceUpdate,
    OfferVersionRead,
    OfferVersionUpdate,
    PolicyDunningStepCreate,
    PolicyDunningStepRead,
    PolicyDunningStepUpdate,
    PolicySetCreate,
    PolicySetRead,
    PolicySetUpdate,
    RadiusAttributeCreate,
    RadiusAttributeRead,
    RadiusAttributeUpdate,
    RadiusProfileCreate,
    RadiusProfileRead,
    RadiusProfileUpdate,
    RegionZoneCreate,
    RegionZoneRead,
    RegionZoneUpdate,
    SlaProfileCreate,
    SlaProfileRead,
    SlaProfileUpdate,
    SubscriptionAddOnCreate,
    SubscriptionAddOnRead,
    SubscriptionAddOnUpdate,
    SubscriptionCreate,
    SubscriptionRead,
    SubscriptionTechnicalUpdate,
    SubscriptionUpdate,
    UsageAllowanceCreate,
    UsageAllowanceRead,
    UsageAllowanceUpdate,
)
from app.schemas.common import ListResponse
from app.services import catalog as catalog_service
from app.services.auth_dependencies import require_method_permission, require_permission

router = APIRouter(
    dependencies=[Depends(require_method_permission("catalog:read", "catalog:write"))]
)

_require_billing_catalog_write = require_permission("catalog:billing_write")


def _actor(auth: dict) -> tuple[str | None, str | None]:
    return auth.get("principal_id"), auth.get("principal_type")


@router.post(
    "/region-zones",
    response_model=RegionZoneRead,
    status_code=status.HTTP_201_CREATED,
    tags=["region-zones"],
)
def create_region_zone(payload: RegionZoneCreate, db: Session = Depends(get_db)):
    return catalog_service.region_zones.create(db, payload)


@router.get(
    "/region-zones/{zone_id}",
    response_model=RegionZoneRead,
    tags=["region-zones"],
)
def get_region_zone(zone_id: str, db: Session = Depends(get_db)):
    return catalog_service.region_zones.get(db, zone_id)


@router.get(
    "/region-zones",
    response_model=ListResponse[RegionZoneRead],
    tags=["region-zones"],
)
def list_region_zones(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.region_zones.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/region-zones/{zone_id}",
    response_model=RegionZoneRead,
    tags=["region-zones"],
)
def update_region_zone(
    zone_id: str, payload: RegionZoneUpdate, db: Session = Depends(get_db)
):
    return catalog_service.region_zones.update(db, zone_id, payload)


@router.delete(
    "/region-zones/{zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["region-zones"],
)
def delete_region_zone(zone_id: str, db: Session = Depends(get_db)):
    catalog_service.region_zones.delete(db, zone_id)


@router.post(
    "/usage-allowances",
    response_model=UsageAllowanceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["usage-allowances"],
)
def create_usage_allowance(
    payload: UsageAllowanceCreate, db: Session = Depends(get_db)
):
    return catalog_service.usage_allowances.create(db, payload)


@router.get(
    "/usage-allowances/{allowance_id}",
    response_model=UsageAllowanceRead,
    tags=["usage-allowances"],
)
def get_usage_allowance(allowance_id: str, db: Session = Depends(get_db)):
    return catalog_service.usage_allowances.get(db, allowance_id)


@router.get(
    "/usage-allowances",
    response_model=ListResponse[UsageAllowanceRead],
    tags=["usage-allowances"],
)
def list_usage_allowances(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.usage_allowances.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/usage-allowances/{allowance_id}",
    response_model=UsageAllowanceRead,
    tags=["usage-allowances"],
)
def update_usage_allowance(
    allowance_id: str, payload: UsageAllowanceUpdate, db: Session = Depends(get_db)
):
    return catalog_service.usage_allowances.update(db, allowance_id, payload)


@router.delete(
    "/usage-allowances/{allowance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["usage-allowances"],
)
def delete_usage_allowance(allowance_id: str, db: Session = Depends(get_db)):
    catalog_service.usage_allowances.delete(db, allowance_id)


@router.post(
    "/sla-profiles",
    response_model=SlaProfileRead,
    status_code=status.HTTP_201_CREATED,
    tags=["sla-profiles"],
)
def create_sla_profile(payload: SlaProfileCreate, db: Session = Depends(get_db)):
    return catalog_service.sla_profiles.create(db, payload)


@router.get(
    "/sla-profiles/{profile_id}",
    response_model=SlaProfileRead,
    tags=["sla-profiles"],
)
def get_sla_profile(profile_id: str, db: Session = Depends(get_db)):
    return catalog_service.sla_profiles.get(db, profile_id)


@router.get(
    "/sla-profiles",
    response_model=ListResponse[SlaProfileRead],
    tags=["sla-profiles"],
)
def list_sla_profiles(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.sla_profiles.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/sla-profiles/{profile_id}",
    response_model=SlaProfileRead,
    tags=["sla-profiles"],
)
def update_sla_profile(
    profile_id: str, payload: SlaProfileUpdate, db: Session = Depends(get_db)
):
    return catalog_service.sla_profiles.update(db, profile_id, payload)


@router.delete(
    "/sla-profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sla-profiles"],
)
def delete_sla_profile(profile_id: str, db: Session = Depends(get_db)):
    catalog_service.sla_profiles.delete(db, profile_id)


@router.post(
    "/policy-sets",
    response_model=PolicySetRead,
    status_code=status.HTTP_201_CREATED,
    tags=["policy-sets"],
)
def create_policy_set(payload: PolicySetCreate, db: Session = Depends(get_db)):
    return catalog_service.policy_sets.create(db, payload)


@router.get(
    "/policy-sets/{policy_id}",
    response_model=PolicySetRead,
    tags=["policy-sets"],
)
def get_policy_set(policy_id: str, db: Session = Depends(get_db)):
    return catalog_service.policy_sets.get(db, policy_id)


@router.get(
    "/policy-sets",
    response_model=ListResponse[PolicySetRead],
    tags=["policy-sets"],
)
def list_policy_sets(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.policy_sets.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/policy-sets/{policy_id}",
    response_model=PolicySetRead,
    tags=["policy-sets"],
)
def update_policy_set(
    policy_id: str, payload: PolicySetUpdate, db: Session = Depends(get_db)
):
    return catalog_service.policy_sets.update(db, policy_id, payload)


@router.delete(
    "/policy-sets/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["policy-sets"],
)
def delete_policy_set(policy_id: str, db: Session = Depends(get_db)):
    catalog_service.policy_sets.delete(db, policy_id)


@router.post(
    "/add-ons",
    response_model=AddOnRead,
    status_code=status.HTTP_201_CREATED,
    tags=["add-ons"],
)
def create_add_on(payload: AddOnCreate, db: Session = Depends(get_db)):
    return catalog_service.add_ons.create(db, payload)


@router.get(
    "/add-ons/{add_on_id}",
    response_model=AddOnRead,
    tags=["add-ons"],
)
def get_add_on(add_on_id: str, db: Session = Depends(get_db)):
    return catalog_service.add_ons.get(db, add_on_id)


@router.get(
    "/add-ons",
    response_model=ListResponse[AddOnRead],
    tags=["add-ons"],
)
def list_add_ons(
    is_active: bool | None = None,
    addon_type: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.add_ons.list_response(
        db, is_active, addon_type, order_by, order_dir, limit, offset
    )


@router.patch(
    "/add-ons/{add_on_id}",
    response_model=AddOnRead,
    tags=["add-ons"],
)
def update_add_on(add_on_id: str, payload: AddOnUpdate, db: Session = Depends(get_db)):
    return catalog_service.add_ons.update(db, add_on_id, payload)


@router.delete(
    "/add-ons/{add_on_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["add-ons"],
)
def delete_add_on(add_on_id: str, db: Session = Depends(get_db)):
    catalog_service.add_ons.delete(db, add_on_id)


@router.post(
    "/offer-prices",
    response_model=OfferPriceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["offer-prices"],
)
def create_offer_price(
    payload: OfferPriceCreate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.offer_prices.create(
        db, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.get(
    "/offer-prices/{price_id}",
    response_model=OfferPriceRead,
    tags=["offer-prices"],
)
def get_offer_price(price_id: str, db: Session = Depends(get_db)):
    return catalog_service.offer_prices.get(db, price_id)


@router.get(
    "/offer-prices",
    response_model=ListResponse[OfferPriceRead],
    tags=["offer-prices"],
)
def list_offer_prices(
    offer_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.offer_prices.list_response(
        db, offer_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/offer-prices/{price_id}",
    response_model=OfferPriceRead,
    tags=["offer-prices"],
)
def update_offer_price(
    price_id: str,
    payload: OfferPriceUpdate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.offer_prices.update(
        db, price_id, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.delete(
    "/offer-prices/{price_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["offer-prices"],
)
def delete_offer_price(
    price_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    catalog_service.offer_prices.delete(
        db, price_id, actor_id=actor_id, actor_type=actor_type
    )


@router.post(
    "/add-on-prices",
    response_model=AddOnPriceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["add-on-prices"],
)
def create_add_on_price(
    payload: AddOnPriceCreate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.add_on_prices.create(
        db, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.get(
    "/add-on-prices/{price_id}",
    response_model=AddOnPriceRead,
    tags=["add-on-prices"],
)
def get_add_on_price(price_id: str, db: Session = Depends(get_db)):
    return catalog_service.add_on_prices.get(db, price_id)


@router.get(
    "/add-on-prices",
    response_model=ListResponse[AddOnPriceRead],
    tags=["add-on-prices"],
)
def list_add_on_prices(
    add_on_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.add_on_prices.list_response(
        db, add_on_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/add-on-prices/{price_id}",
    response_model=AddOnPriceRead,
    tags=["add-on-prices"],
)
def update_add_on_price(
    price_id: str,
    payload: AddOnPriceUpdate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.add_on_prices.update(
        db, price_id, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.delete(
    "/add-on-prices/{price_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["add-on-prices"],
)
def delete_add_on_price(
    price_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    catalog_service.add_on_prices.delete(
        db, price_id, actor_id=actor_id, actor_type=actor_type
    )


@router.post(
    "/offers",
    response_model=CatalogOfferRead,
    status_code=status.HTTP_201_CREATED,
    tags=["offers"],
)
def create_offer(
    payload: CatalogOfferCreate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.offers.create(
        db, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.get(
    "/offers/{offer_id}",
    response_model=CatalogOfferRead,
    tags=["offers"],
)
def get_offer(offer_id: str, db: Session = Depends(get_db)):
    return catalog_service.offers.get(db, offer_id)


@router.get(
    "/offers",
    response_model=ListResponse[CatalogOfferRead],
    tags=["offers"],
)
def list_offers(
    service_type: str | None = None,
    access_type: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.offers.list_response(
        db,
        service_type,
        access_type,
        status,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/offers/{offer_id}",
    response_model=CatalogOfferRead,
    tags=["offers"],
)
def update_offer(
    offer_id: str,
    payload: CatalogOfferUpdate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.offers.update(
        db, offer_id, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.delete(
    "/offers/{offer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["offers"],
)
def delete_offer(
    offer_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    catalog_service.offers.delete(
        db, offer_id, actor_id=actor_id, actor_type=actor_type
    )
    db.commit()


@router.post(
    "/subscriptions",
    response_model=SubscriptionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscriptions"],
)
def create_subscription(payload: SubscriptionCreate, db: Session = Depends(get_db)):
    return catalog_service.subscriptions.create(db, payload)


@router.get(
    "/subscriptions/{subscription_id}",
    response_model=SubscriptionRead,
    tags=["subscriptions"],
)
def get_subscription(subscription_id: str, db: Session = Depends(get_db)):
    return catalog_service.subscriptions.get(db, subscription_id)


@router.get(
    "/subscriptions",
    response_model=ListResponse[SubscriptionRead],
    tags=["subscriptions"],
)
def list_subscriptions(
    subscriber_id: str | None = None,
    account_id: str | None = None,
    offer_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if not subscriber_id and account_id:
        subscriber_id = account_id
    return catalog_service.subscriptions.list_response(
        db, subscriber_id, offer_id, status, order_by, order_dir, limit, offset
    )


@router.patch(
    "/subscriptions/{subscription_id}",
    response_model=SubscriptionRead,
    tags=["subscriptions"],
)
def update_subscription(
    subscription_id: str,
    payload: SubscriptionTechnicalUpdate,
    db: Session = Depends(get_db),
):
    return catalog_service.subscriptions.update(
        db,
        subscription_id,
        SubscriptionUpdate.model_validate(payload.model_dump(exclude_unset=True)),
    )


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscriptions"],
)
def delete_subscription(subscription_id: str, db: Session = Depends(get_db)):
    catalog_service.subscriptions.delete(db, subscription_id)


@router.post(
    "/subscription-add-ons",
    response_model=SubscriptionAddOnRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscription-add-ons"],
)
def create_subscription_add_on(
    payload: SubscriptionAddOnCreate, db: Session = Depends(get_db)
):
    return catalog_service.subscription_add_ons.create(db, payload)


@router.get(
    "/subscription-add-ons/{subscription_add_on_id}",
    response_model=SubscriptionAddOnRead,
    tags=["subscription-add-ons"],
)
def get_subscription_add_on(subscription_add_on_id: str, db: Session = Depends(get_db)):
    return catalog_service.subscription_add_ons.get(db, subscription_add_on_id)


@router.get(
    "/subscription-add-ons",
    response_model=ListResponse[SubscriptionAddOnRead],
    tags=["subscription-add-ons"],
)
def list_subscription_add_ons(
    subscription_id: str | None = None,
    add_on_id: str | None = None,
    order_by: str = Query(default="start_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.subscription_add_ons.list_response(
        db, subscription_id, add_on_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/subscription-add-ons/{subscription_add_on_id}",
    response_model=SubscriptionAddOnRead,
    tags=["subscription-add-ons"],
)
def update_subscription_add_on(
    subscription_add_on_id: str,
    payload: SubscriptionAddOnUpdate,
    db: Session = Depends(get_db),
):
    return catalog_service.subscription_add_ons.update(
        db, subscription_add_on_id, payload
    )


@router.delete(
    "/subscription-add-ons/{subscription_add_on_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscription-add-ons"],
)
def delete_subscription_add_on(
    subscription_add_on_id: str, db: Session = Depends(get_db)
):
    catalog_service.subscription_add_ons.delete(db, subscription_add_on_id)


@router.post(
    "/offer-versions",
    response_model=OfferVersionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["offer-versions"],
)
def create_offer_version(
    payload: OfferVersionCreate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.offer_versions.create(
        db, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.get(
    "/offer-versions/{version_id}",
    response_model=OfferVersionRead,
    tags=["offer-versions"],
)
def get_offer_version(version_id: str, db: Session = Depends(get_db)):
    return catalog_service.offer_versions.get(db, version_id)


@router.get(
    "/offer-versions",
    response_model=ListResponse[OfferVersionRead],
    tags=["offer-versions"],
)
def list_offer_versions(
    offer_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.offer_versions.list_response(
        db, offer_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/offer-versions/{version_id}",
    response_model=OfferVersionRead,
    tags=["offer-versions"],
)
def update_offer_version(
    version_id: str,
    payload: OfferVersionUpdate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.offer_versions.update(
        db, version_id, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.delete(
    "/offer-versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["offer-versions"],
)
def delete_offer_version(
    version_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    catalog_service.offer_versions.delete(
        db, version_id, actor_id=actor_id, actor_type=actor_type
    )


@router.post(
    "/offer-version-prices",
    response_model=OfferVersionPriceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["offer-version-prices"],
)
def create_offer_version_price(
    payload: OfferVersionPriceCreate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.offer_version_prices.create(
        db, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.get(
    "/offer-version-prices/{price_id}",
    response_model=OfferVersionPriceRead,
    tags=["offer-version-prices"],
)
def get_offer_version_price(price_id: str, db: Session = Depends(get_db)):
    return catalog_service.offer_version_prices.get(db, price_id)


@router.get(
    "/offer-version-prices",
    response_model=ListResponse[OfferVersionPriceRead],
    tags=["offer-version-prices"],
)
def list_offer_version_prices(
    offer_version_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.offer_version_prices.list_response(
        db, offer_version_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/offer-version-prices/{price_id}",
    response_model=OfferVersionPriceRead,
    tags=["offer-version-prices"],
)
def update_offer_version_price(
    price_id: str,
    payload: OfferVersionPriceUpdate,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    return catalog_service.offer_version_prices.update(
        db, price_id, payload, actor_id=actor_id, actor_type=actor_type
    )


@router.delete(
    "/offer-version-prices/{price_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["offer-version-prices"],
)
def delete_offer_version_price(
    price_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(_require_billing_catalog_write),
):
    actor_id, actor_type = _actor(auth)
    catalog_service.offer_version_prices.delete(
        db, price_id, actor_id=actor_id, actor_type=actor_type
    )


@router.post(
    "/policy-dunning-steps",
    response_model=PolicyDunningStepRead,
    status_code=status.HTTP_201_CREATED,
    tags=["policy-dunning-steps"],
)
def create_policy_dunning_step(
    payload: PolicyDunningStepCreate, db: Session = Depends(get_db)
):
    return catalog_service.policy_dunning_steps.create(db, payload)


@router.get(
    "/policy-dunning-steps/{step_id}",
    response_model=PolicyDunningStepRead,
    tags=["policy-dunning-steps"],
)
def get_policy_dunning_step(step_id: str, db: Session = Depends(get_db)):
    return catalog_service.policy_dunning_steps.get(db, step_id)


@router.get(
    "/policy-dunning-steps",
    response_model=ListResponse[PolicyDunningStepRead],
    tags=["policy-dunning-steps"],
)
def list_policy_dunning_steps(
    policy_set_id: str | None = None,
    order_by: str = Query(default="day_offset"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.policy_dunning_steps.list_response(
        db, policy_set_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/policy-dunning-steps/{step_id}",
    response_model=PolicyDunningStepRead,
    tags=["policy-dunning-steps"],
)
def update_policy_dunning_step(
    step_id: str, payload: PolicyDunningStepUpdate, db: Session = Depends(get_db)
):
    return catalog_service.policy_dunning_steps.update(db, step_id, payload)


@router.delete(
    "/policy-dunning-steps/{step_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["policy-dunning-steps"],
)
def delete_policy_dunning_step(step_id: str, db: Session = Depends(get_db)):
    catalog_service.policy_dunning_steps.delete(db, step_id)


@router.post(
    "/nas-devices",
    response_model=NasDeviceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["nas-devices"],
    operation_id="catalog_create_nas_device",
)
def create_nas_device(payload: NasDeviceCreate, db: Session = Depends(get_db)):
    return catalog_service.nas_devices.create(db, payload)


@router.get(
    "/nas-devices/{device_id}",
    response_model=NasDeviceRead,
    tags=["nas-devices"],
    operation_id="catalog_get_nas_device",
)
def get_nas_device(device_id: str, db: Session = Depends(get_db)):
    return catalog_service.nas_devices.get(db, device_id)


@router.get(
    "/nas-devices",
    response_model=ListResponse[NasDeviceRead],
    tags=["nas-devices"],
    operation_id="catalog_list_nas_devices",
)
def list_nas_devices(
    vendor: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.nas_devices.list_response(
        db, vendor, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/nas-devices/{device_id}",
    response_model=NasDeviceRead,
    tags=["nas-devices"],
    operation_id="catalog_update_nas_device",
)
def update_nas_device(
    device_id: str, payload: NasDeviceUpdate, db: Session = Depends(get_db)
):
    return catalog_service.nas_devices.update(db, device_id, payload)


@router.delete(
    "/nas-devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["nas-devices"],
    operation_id="catalog_delete_nas_device",
)
def delete_nas_device(device_id: str, db: Session = Depends(get_db)):
    catalog_service.nas_devices.delete(db, device_id)


@router.post(
    "/radius-profiles",
    response_model=RadiusProfileRead,
    status_code=status.HTTP_201_CREATED,
    tags=["radius-profiles"],
)
def create_radius_profile(payload: RadiusProfileCreate, db: Session = Depends(get_db)):
    return catalog_service.radius_profiles.create(db, payload)


@router.get(
    "/radius-profiles/{profile_id}",
    response_model=RadiusProfileRead,
    tags=["radius-profiles"],
)
def get_radius_profile(profile_id: str, db: Session = Depends(get_db)):
    return catalog_service.radius_profiles.get(db, profile_id)


@router.get(
    "/radius-profiles",
    response_model=ListResponse[RadiusProfileRead],
    tags=["radius-profiles"],
)
def list_radius_profiles(
    vendor: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.radius_profiles.list_response(
        db, vendor, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/radius-profiles/{profile_id}",
    response_model=RadiusProfileRead,
    tags=["radius-profiles"],
)
def update_radius_profile(
    profile_id: str, payload: RadiusProfileUpdate, db: Session = Depends(get_db)
):
    return catalog_service.radius_profiles.update(db, profile_id, payload)


@router.delete(
    "/radius-profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["radius-profiles"],
)
def delete_radius_profile(profile_id: str, db: Session = Depends(get_db)):
    catalog_service.radius_profiles.delete(db, profile_id)


@router.post(
    "/radius-attributes",
    response_model=RadiusAttributeRead,
    status_code=status.HTTP_201_CREATED,
    tags=["radius-attributes"],
)
def create_radius_attribute(
    payload: RadiusAttributeCreate, db: Session = Depends(get_db)
):
    return catalog_service.radius_attributes.create(db, payload)


@router.get(
    "/radius-attributes/{attribute_id}",
    response_model=RadiusAttributeRead,
    tags=["radius-attributes"],
)
def get_radius_attribute(attribute_id: str, db: Session = Depends(get_db)):
    return catalog_service.radius_attributes.get(db, attribute_id)


@router.get(
    "/radius-attributes",
    response_model=ListResponse[RadiusAttributeRead],
    tags=["radius-attributes"],
)
def list_radius_attributes(
    profile_id: str | None = None,
    order_by: str = Query(default="attribute"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.radius_attributes.list_response(
        db, profile_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/radius-attributes/{attribute_id}",
    response_model=RadiusAttributeRead,
    tags=["radius-attributes"],
)
def update_radius_attribute(
    attribute_id: str, payload: RadiusAttributeUpdate, db: Session = Depends(get_db)
):
    return catalog_service.radius_attributes.update(db, attribute_id, payload)


@router.delete(
    "/radius-attributes/{attribute_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["radius-attributes"],
)
def delete_radius_attribute(attribute_id: str, db: Session = Depends(get_db)):
    catalog_service.radius_attributes.delete(db, attribute_id)


@router.post(
    "/offer-radius-profiles",
    response_model=OfferRadiusProfileRead,
    status_code=status.HTTP_201_CREATED,
    tags=["offer-radius-profiles"],
)
def create_offer_radius_profile(
    payload: OfferRadiusProfileCreate, db: Session = Depends(get_db)
):
    return catalog_service.offer_radius_profiles.create(db, payload)


@router.get(
    "/offer-radius-profiles/{link_id}",
    response_model=OfferRadiusProfileRead,
    tags=["offer-radius-profiles"],
)
def get_offer_radius_profile(link_id: str, db: Session = Depends(get_db)):
    return catalog_service.offer_radius_profiles.get(db, link_id)


@router.get(
    "/offer-radius-profiles",
    response_model=ListResponse[OfferRadiusProfileRead],
    tags=["offer-radius-profiles"],
)
def list_offer_radius_profiles(
    offer_id: str | None = None,
    profile_id: str | None = None,
    order_by: str = Query(default="offer_id"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return catalog_service.offer_radius_profiles.list_response(
        db, offer_id, profile_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/offer-radius-profiles/{link_id}",
    response_model=OfferRadiusProfileRead,
    tags=["offer-radius-profiles"],
)
def update_offer_radius_profile(
    link_id: str, payload: OfferRadiusProfileUpdate, db: Session = Depends(get_db)
):
    return catalog_service.offer_radius_profiles.update(db, link_id, payload)


@router.delete(
    "/offer-radius-profiles/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["offer-radius-profiles"],
)
def delete_offer_radius_profile(link_id: str, db: Session = Depends(get_db)):
    catalog_service.offer_radius_profiles.delete(db, link_id)


@router.post(
    "/access-credentials",
    response_model=AccessCredentialRead,
    status_code=status.HTTP_201_CREATED,
    tags=["access-credentials"],
)
def create_access_credential(
    payload: AccessCredentialCreate, db: Session = Depends(get_db)
):
    return catalog_service.access_credentials.create(db, payload)


@router.get(
    "/access-credentials/{credential_id}",
    response_model=AccessCredentialRead,
    tags=["access-credentials"],
)
def get_access_credential(credential_id: str, db: Session = Depends(get_db)):
    return catalog_service.access_credentials.get(db, credential_id)


@router.get(
    "/access-credentials",
    response_model=ListResponse[AccessCredentialRead],
    tags=["access-credentials"],
)
def list_access_credentials(
    subscriber_id: str | None = None,
    account_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if not subscriber_id and account_id:
        subscriber_id = account_id
    return catalog_service.access_credentials.list_response(
        db, subscriber_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/access-credentials/{credential_id}",
    response_model=AccessCredentialRead,
    tags=["access-credentials"],
)
def update_access_credential(
    credential_id: str, payload: AccessCredentialUpdate, db: Session = Depends(get_db)
):
    return catalog_service.access_credentials.update(db, credential_id, payload)


@router.delete(
    "/access-credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["access-credentials"],
)
def delete_access_credential(credential_id: str, db: Session = Depends(get_db)):
    catalog_service.access_credentials.delete(db, credential_id)


@router.post(
    "/offers/validate",
    response_model=OfferValidationResponse,
    status_code=status.HTTP_200_OK,
    tags=["offers"],
)
def validate_offer(payload: OfferValidationRequest, db: Session = Depends(get_db)):
    return catalog_service.offer_validation.validate(db, payload)
