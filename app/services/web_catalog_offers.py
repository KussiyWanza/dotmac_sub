"""Service helpers for admin catalog offer web routes."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from pydantic import ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.catalog import (
    PLAN_FAMILY_VALUES,
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    ContractTerm,
    GuaranteedSpeedType,
    NasVendor,
    OfferPrice,
    OfferStatus,
    PlanCategory,
    PriceBasis,
    PriceUnit,
    RadiusProfile,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.offer_availability import (
    OfferBillingModeAvailability,
    OfferCategoryAvailability,
    OfferLocationAvailability,
    OfferResellerAvailability,
)
from app.schemas.catalog import (
    CatalogOfferCreate,
    CatalogOfferUpdate,
    OfferPriceCreate,
    OfferPriceUpdate,
    OfferRadiusProfileCreate,
    OfferRadiusProfileUpdate,
    RadiusProfileCreate,
    RadiusProfileUpdate,
)
from app.schemas.status_presentation import StatusTone
from app.services import catalog as catalog_service
from app.services import catalog_billing_governance, settings_spec
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.catalog.subscriptions import apply_offer_radius_profile
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.network.profile_sync import (
    enqueue_offer_profile_sync_tasks_for_existing_bundles,
)
from app.services.radius import reconcile_subscription_connectivity
from app.services.ui_contracts import Kpi, StateValue

logger = logging.getLogger(__name__)

PLAN_KIND_STANDARD = "standard"
PLAN_KIND_IP_ADDRESS = "ip_address"
PLAN_KIND_DEVICE_REPLACEMENT = "device_replacement"
PLAN_KINDS = [PLAN_KIND_STANDARD, PLAN_KIND_IP_ADDRESS, PLAN_KIND_DEVICE_REPLACEMENT]
IP_BLOCK_SIZES = ["/32", "/30", "/29", "/28", "/27", "/26", "/25", "/24"]


def parse_offer_description_metadata(
    description: str | None,
) -> tuple[dict[str, str | None], str | None]:
    text = str(description or "").strip()
    metadata: dict[str, str | None] = {"plan_kind": None, "ip_block_size": None}
    if not text:
        return metadata, None

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("[plan_kind:") and lowered.endswith("]"):
            metadata["plan_kind"] = stripped[11:-1].strip() or None
            continue
        if lowered.startswith("[ip_block_size:") and lowered.endswith("]"):
            metadata["ip_block_size"] = stripped[15:-1].strip() or None
            continue
        cleaned_lines.append(stripped)
    cleaned_text = "\n".join(line for line in cleaned_lines if line).strip() or None
    return metadata, cleaned_text


def normalize_offer_description(
    *,
    description: str | None,
    plan_kind: str | None,
    ip_block_size: str | None,
) -> str | None:
    metadata, cleaned = parse_offer_description_metadata(description)
    resolved_plan_kind = (
        str(plan_kind or metadata.get("plan_kind") or PLAN_KIND_STANDARD)
        .strip()
        .lower()
    )
    if resolved_plan_kind not in PLAN_KINDS:
        resolved_plan_kind = PLAN_KIND_STANDARD

    resolved_ip_block = (
        str(ip_block_size or metadata.get("ip_block_size") or "").strip() or None
    )
    lines: list[str] = []
    if resolved_plan_kind != PLAN_KIND_STANDARD:
        lines.append(f"[plan_kind:{resolved_plan_kind}]")
    if resolved_plan_kind == PLAN_KIND_IP_ADDRESS and resolved_ip_block:
        lines.append(f"[ip_block_size:{resolved_ip_block}]")
    if cleaned:
        lines.append(cleaned)
    return "\n".join(lines).strip() or None


def get_offer_availability(
    db: Session,
    offer_id: str,
) -> dict[str, object]:
    """Load availability records for the offer detail page."""
    return {
        "reseller_availability": (
            db.query(OfferResellerAvailability)
            .filter(OfferResellerAvailability.offer_id == offer_id)
            .all()
        ),
        "location_availability": (
            db.query(OfferLocationAvailability)
            .filter(OfferLocationAvailability.offer_id == offer_id)
            .all()
        ),
        "category_availability": (
            db.query(OfferCategoryAvailability)
            .filter(OfferCategoryAvailability.offer_id == offer_id)
            .all()
        ),
        "billing_mode_availability": (
            db.query(OfferBillingModeAvailability)
            .filter(OfferBillingModeAvailability.offer_id == offer_id)
            .all()
        ),
    }


def default_offer_form(db: Session | None = None) -> dict[str, object]:
    """Return default values for offer create form.

    Visibility flags default OFF unless the ``new_offer_visible_by_default``
    catalog setting opts in: a fresh offer silently landing on the customer
    portal is a real incident pattern (~40 e2e offers went customer-visible).
    """
    visible_default = False
    if db is not None:
        raw = settings_spec.resolve_value(
            db, SettingDomain.catalog, "new_offer_visible_by_default"
        )
        visible_default = str(raw).lower() in {"true", "1", "on", "yes"}
    return {
        "name": "",
        "code": "",
        "service_type": ServiceType.residential.value,
        "access_type": AccessType.fiber.value,
        "price_basis": PriceBasis.flat.value,
        "billing_cycle": BillingCycle.monthly.value,
        "billing_mode": "",
        "contract_term": ContractTerm.month_to_month.value,
        "region_zone_id": "",
        "usage_allowance_id": "",
        "sla_profile_id": "",
        "radius_profile_id": "",
        "policy_set_id": "",
        "with_vat": False,
        "vat_percent": "",
        "speed_download_mbps": "",
        "speed_upload_mbps": "",
        "guaranteed_speed_limit_at": "",
        "guaranteed_speed": GuaranteedSpeedType.none.value,
        "aggregation": "",
        "priority": "",
        "available_for_services": visible_default,
        "show_on_customer_portal": visible_default,
        "olt_profile_auto_sync_enabled": False,
        "plan_category": PlanCategory.internet.value,
        "hide_on_admin_portal": False,
        "service_description": "",
        "burst_profile": "",
        "prepaid_period": "",
        "plan_family": "",
        "allowed_change_plan_ids": "",
        "status": "active",
        "description": "",
        "is_active": True,
        "plan_kind": PLAN_KIND_STANDARD,
        "ip_block_size": "",
        "price_id": "",
        "price_type": "recurring",
        "price_amount": "",
        "price_currency": "NGN",
        "price_billing_cycle": BillingCycle.monthly.value,
        "price_unit": PriceUnit.month.value,
        "price_description": "",
    }


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def parse_offer_form(form: FormData) -> dict[str, object]:
    """Parse offer form payload from request form."""
    return {
        "name": _form_str(form, "name").strip(),
        "code": _form_str(form, "code").strip(),
        "service_type": _form_str(form, "service_type").strip(),
        "access_type": _form_str(form, "access_type").strip(),
        "price_basis": _form_str(form, "price_basis").strip(),
        "billing_cycle": _form_str(form, "billing_cycle").strip(),
        "billing_mode": _form_str(form, "billing_mode").strip(),
        "contract_term": _form_str(form, "contract_term").strip(),
        "region_zone_id": _form_str(form, "region_zone_id").strip(),
        "usage_allowance_id": _form_str(form, "usage_allowance_id").strip(),
        "sla_profile_id": _form_str(form, "sla_profile_id").strip(),
        "radius_profile_id": _form_str(form, "radius_profile_id").strip(),
        "policy_set_id": _form_str(form, "policy_set_id").strip(),
        "with_vat": form.get("with_vat") == "true",
        "vat_percent": _form_str(form, "vat_percent").strip(),
        "speed_download_mbps": _form_str(form, "speed_download_mbps").strip(),
        "speed_upload_mbps": _form_str(form, "speed_upload_mbps").strip(),
        "guaranteed_speed_limit_at": _form_str(
            form, "guaranteed_speed_limit_at"
        ).strip(),
        "guaranteed_speed": _form_str(form, "guaranteed_speed").strip(),
        "aggregation": _form_str(form, "aggregation").strip(),
        "priority": _form_str(form, "priority").strip(),
        "available_for_services": form.get("available_for_services") == "true",
        "show_on_customer_portal": form.get("show_on_customer_portal") == "true",
        "olt_profile_auto_sync_enabled": form.get("olt_profile_auto_sync_enabled")
        == "true",
        "plan_category": _form_str(form, "plan_category").strip()
        or PlanCategory.internet.value,
        "hide_on_admin_portal": form.get("hide_on_admin_portal") == "true",
        "service_description": _form_str(form, "service_description").strip(),
        "burst_profile": _form_str(form, "burst_profile").strip(),
        "prepaid_period": _form_str(form, "prepaid_period").strip(),
        "plan_family": _form_str(form, "plan_family").strip(),
        "allowed_change_plan_ids": _form_str(form, "allowed_change_plan_ids").strip(),
        "status": _form_str(form, "status").strip(),
        "description": _form_str(form, "description").strip(),
        "is_active": form.get("is_active") == "true",
        "plan_kind": _form_str(form, "plan_kind").strip() or PLAN_KIND_STANDARD,
        "ip_block_size": _form_str(form, "ip_block_size").strip(),
        "price_id": _form_str(form, "price_id").strip(),
        "price_type": _form_str(form, "price_type").strip() or "recurring",
        "price_amount": _form_str(form, "price_amount").strip(),
        "price_currency": _form_str(form, "price_currency", "NGN").strip(),
        "price_billing_cycle": _form_str(form, "price_billing_cycle").strip(),
        "price_unit": _form_str(form, "price_unit").strip(),
        "price_description": _form_str(form, "price_description").strip(),
    }


def validate_offer_form(offer: dict[str, object]) -> str | None:
    """Validate required offer form fields."""
    plan_kind = str(offer.get("plan_kind") or PLAN_KIND_STANDARD).strip().lower()
    if plan_kind not in PLAN_KINDS:
        return "Plan kind is invalid."
    if (
        plan_kind == PLAN_KIND_IP_ADDRESS
        and not str(offer.get("ip_block_size") or "").strip()
    ):
        return "IP block size is required for IP address plans."

    price_type = str(offer.get("price_type") or "recurring").strip().lower()
    if price_type not in {"recurring", "one_time", "usage"}:
        return "Price type is invalid."
    if not offer.get("price_amount"):
        return "Price amount is required."
    return None


def build_offer_payload_data(offer: dict[str, object]) -> dict[str, object]:
    """Build payload dict for CatalogOffer create/update schemas."""
    payload_data = {
        "name": offer["name"],
        "service_type": offer["service_type"],
        "access_type": offer["access_type"],
        "price_basis": offer["price_basis"],
        "is_active": offer["is_active"],
        "with_vat": offer["with_vat"],
        "available_for_services": offer["available_for_services"],
        "show_on_customer_portal": offer["show_on_customer_portal"],
        "olt_profile_auto_sync_enabled": offer["olt_profile_auto_sync_enabled"],
        "plan_category": offer.get("plan_category") or PlanCategory.internet.value,
        "hide_on_admin_portal": offer.get("hide_on_admin_portal", False),
        "description": normalize_offer_description(
            description=str(offer.get("description") or "").strip() or None,
            plan_kind=str(offer.get("plan_kind") or PLAN_KIND_STANDARD),
            ip_block_size=str(offer.get("ip_block_size") or "").strip() or None,
        ),
    }
    optional_fields = [
        "code",
        "billing_cycle",
        "billing_mode",
        "contract_term",
        "region_zone_id",
        "usage_allowance_id",
        "sla_profile_id",
        "policy_set_id",
        "vat_percent",
        "speed_download_mbps",
        "speed_upload_mbps",
        "guaranteed_speed_limit_at",
        "guaranteed_speed",
        "aggregation",
        "priority",
        "service_description",
        "burst_profile",
        "prepaid_period",
        "plan_family",
        "allowed_change_plan_ids",
        "status",
    ]
    for key in optional_fields:
        value = offer.get(key)
        if value:
            payload_data[key] = value
    return payload_data


def upsert_radius_profile_link(db: Session, offer_id: str, profile_id: str) -> None:
    """Create or update radius profile link for offer."""
    links = catalog_service.offer_radius_profiles.list(
        db=db,
        offer_id=offer_id,
        profile_id=None,
        order_by="offer_id",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    if links:
        catalog_service.offer_radius_profiles.update(
            db=db,
            link_id=links[0].id,
            payload=OfferRadiusProfileUpdate(profile_id=coerce_uuid(profile_id)),
        )
    else:
        catalog_service.offer_radius_profiles.create(
            db=db,
            payload=OfferRadiusProfileCreate(
                offer_id=coerce_uuid(offer_id),
                profile_id=coerce_uuid(profile_id),
            ),
        )


def get_linked_radius_profile_id(db: Session, offer_id: str) -> str | None:
    """Return the currently linked RADIUS profile ID for an offer, if any."""
    links = catalog_service.offer_radius_profiles.list(
        db=db,
        offer_id=offer_id,
        profile_id=None,
        order_by="offer_id",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    if not links:
        return None
    profile_id = getattr(links[0], "profile_id", None)
    return str(profile_id) if profile_id else None


def generated_radius_profile_code_for_offer(offer_id: str) -> str:
    """Return the stable code used for auto-generated offer RADIUS profiles."""
    return f"offer-{offer_id}"


#: ``CatalogOffer.speed_*_mbps`` is megabits; ``RadiusProfile.*_speed`` is
#: kilobits (see the column comments on both). Nothing converted between them,
#: so a 100 Mbps offer generated a ``100k/100k`` rate limit — a 1000x throttle
#: that ``sync_offer_radius_profile_to_subscriptions`` then pushed to every
#: subscription on the offer. The unit boundary lives here and nowhere else.
_KBPS_PER_MBPS = 1000


def _coerce_offer_speed(value: object) -> int | None:
    """Offer megabits -> profile kilobits, the unit RadiusProfile stores."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        mbps = int(text)
    except (TypeError, ValueError):
        return None
    return mbps * _KBPS_PER_MBPS


def _build_offer_generated_rate_limit(
    download_speed: int | None, upload_speed: int | None
) -> str | None:
    """MikroTik rate-limit string from speeds already expressed in kilobits.

    Emitted rx/tx — NAS-perspective, so upload first, download second. See
    ``app.services.bandwidth.to_subscriber_directions`` for the canonical
    statement of that convention.
    """
    if not download_speed and not upload_speed:
        return None
    return f"{upload_speed or 0}k/{download_speed or 0}k"


def ensure_generated_radius_profile_for_offer(
    db: Session, offer: CatalogOffer
) -> RadiusProfile:
    """Create or update the auto-generated MikroTik profile for an offer."""
    profile_code = generated_radius_profile_code_for_offer(str(offer.id))
    download_speed = _coerce_offer_speed(offer.speed_download_mbps)
    upload_speed = _coerce_offer_speed(offer.speed_upload_mbps)
    payload_data = {
        "name": str(offer.name or "").strip() or f"Offer {offer.id}",
        "code": profile_code,
        "vendor": NasVendor.mikrotik,
        "download_speed": download_speed,
        "upload_speed": upload_speed,
        "mikrotik_rate_limit": _build_offer_generated_rate_limit(
            download_speed, upload_speed
        ),
        "description": f"Auto-generated from offer {offer.id}",
        "is_active": bool(offer.is_active),
    }
    existing_profile = (
        db.query(RadiusProfile).filter(RadiusProfile.code == profile_code).first()
    )
    if existing_profile:
        return catalog_service.radius_profiles.update(
            db=db,
            profile_id=str(existing_profile.id),
            payload=RadiusProfileUpdate.model_validate(payload_data),
        )
    return catalog_service.radius_profiles.create(
        db=db,
        payload=RadiusProfileCreate.model_validate(payload_data),
    )


def sync_offer_radius_profile_to_subscriptions(
    db: Session,
    *,
    offer_id: str,
    previous_profile_id: str | None,
    new_profile_id: str | None,
) -> None:
    """Apply a changed offer profile to inherited subscriptions and resync active sessions."""
    offer_subscriptions = (
        db.query(Subscription)
        .filter(Subscription.offer_id == coerce_uuid(offer_id))
        .all()
    )
    active_subscription_ids: list[str] = []
    previous_profile_uuid = (
        coerce_uuid(previous_profile_id) if previous_profile_id else None
    )
    new_profile_uuid = coerce_uuid(new_profile_id) if new_profile_id else None
    for subscription in offer_subscriptions:
        inherited_profile = (
            subscription.radius_profile_id is None
            or subscription.radius_profile_id == previous_profile_uuid
            or subscription.radius_profile_id == new_profile_uuid
        )
        if inherited_profile:
            apply_offer_radius_profile(
                db,
                subscription,
                target_profile_id=new_profile_uuid,
                force=True,
            )
            if subscription.status == SubscriptionStatus.active:
                active_subscription_ids.append(str(subscription.id))
    db.commit()
    for subscription_id in active_subscription_ids:
        try:
            reconcile_subscription_connectivity(db, subscription_id).require_projected()
        except Exception:
            logger.warning(
                "Failed to reconcile subscription %s after offer profile sync.",
                subscription_id,
                exc_info=True,
            )


def ensure_offer_radius_profile(
    db: Session,
    offer: CatalogOffer,
    *,
    explicit_profile_id: str | None = None,
    previous_profile_id: str | None = None,
) -> str | None:
    """Ensure an offer has a linked RADIUS profile and sync inherited subscriptions."""
    profile_id = str(explicit_profile_id or "").strip()
    if not profile_id:
        generated_profile = ensure_generated_radius_profile_for_offer(db, offer)
        profile_id = str(generated_profile.id)
    upsert_radius_profile_link(db, str(offer.id), profile_id)
    sync_offer_radius_profile_to_subscriptions(
        db,
        offer_id=str(offer.id),
        previous_profile_id=previous_profile_id,
        new_profile_id=profile_id,
    )
    return profile_id


def backfill_offer_radius_profiles(
    db: Session,
    *,
    force_offer_ids: set[str] | None = None,
) -> dict[str, int]:
    """Create and link generated profiles for offers that need them."""
    forced = {str(item) for item in (force_offer_ids or set()) if str(item)}
    offers = db.scalars(select(CatalogOffer).order_by(CatalogOffer.name.asc())).all()
    created_or_updated = 0
    linked = 0
    for offer in offers:
        previous_profile_id = get_linked_radius_profile_id(db, str(offer.id))
        if previous_profile_id and str(offer.id) not in forced:
            continue
        ensure_offer_radius_profile(
            db,
            offer,
            previous_profile_id=previous_profile_id,
        )
        created_or_updated += 1
        linked += 1
    return {"offers_processed": created_or_updated, "links_updated": linked}


def create_recurring_price(
    db: Session,
    offer_id: str,
    offer: dict[str, object],
    *,
    actor_id: str | None = None,
    actor_type: str | None = None,
):
    """Create recurring offer price if amount is provided."""
    if not offer.get("price_amount"):
        return None
    price_payload = {
        "offer_id": offer_id,
        "price_type": str(offer.get("price_type") or "recurring"),
        "amount": offer["price_amount"],
        "currency": offer["price_currency"],
    }
    if offer.get("price_billing_cycle"):
        price_payload["billing_cycle"] = offer["price_billing_cycle"]
    if offer.get("price_unit"):
        price_payload["unit"] = offer["price_unit"]
    if offer.get("price_description"):
        price_payload["description"] = offer["price_description"]
    return catalog_service.offer_prices.create(
        db=db,
        payload=OfferPriceCreate.model_validate(price_payload),
        actor_id=actor_id,
        actor_type=actor_type,
    )


def upsert_recurring_price(
    db: Session,
    offer_id: str,
    offer: dict[str, object],
    *,
    actor_id: str | None = None,
    actor_type: str | None = None,
):
    """Create/update recurring offer price if amount is provided."""
    if not offer.get("price_amount"):
        return None, None
    price_payload = {
        "price_type": str(offer.get("price_type") or "recurring"),
        "amount": offer["price_amount"],
        "currency": offer["price_currency"],
    }
    if offer.get("price_billing_cycle"):
        price_payload["billing_cycle"] = offer["price_billing_cycle"]
    if offer.get("price_unit"):
        price_payload["unit"] = offer["price_unit"]
    if offer.get("price_description"):
        price_payload["description"] = offer["price_description"]
    if offer.get("price_id"):
        updated = catalog_service.offer_prices.update(
            db=db,
            price_id=str(offer["price_id"]),
            payload=OfferPriceUpdate.model_validate(price_payload),
            actor_id=actor_id,
            actor_type=actor_type,
        )
        return updated, "updated"
    price_payload["offer_id"] = offer_id
    created = catalog_service.offer_prices.create(
        db=db,
        payload=OfferPriceCreate.model_validate(price_payload),
        actor_id=actor_id,
        actor_type=actor_type,
    )
    return created, "created"


def offer_edit_form_data(
    db: Session, offer_id: str, offer: CatalogOffer
) -> tuple[dict[str, object], list]:
    """Build offer edit form values from persisted offer + related records."""
    links = catalog_service.offer_radius_profiles.list(
        db=db,
        offer_id=offer_id,
        profile_id=None,
        order_by="offer_id",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    radius_profile_id = links[0].profile_id if links else ""
    offer_addon_links = catalog_service.offer_addons.list(
        db=db, offer_id=offer_id, limit=200
    )
    prices = catalog_service.offer_prices.list(
        db=db,
        offer_id=offer_id,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    price = next(
        (item for item in prices if item.price_type.value == "recurring"), None
    )
    if not price and prices:
        price = prices[0]
    offer_data = {
        "name": offer.name,
        "code": offer.code or "",
        "service_type": offer.service_type,
        "access_type": offer.access_type,
        "price_basis": offer.price_basis,
        "billing_cycle": offer.billing_cycle,
        "billing_mode": offer.billing_mode.value
        if offer.billing_mode
        else BillingMode.prepaid.value,
        "contract_term": offer.contract_term,
        "region_zone_id": offer.region_zone_id or "",
        "usage_allowance_id": offer.usage_allowance_id or "",
        "sla_profile_id": offer.sla_profile_id or "",
        "radius_profile_id": radius_profile_id or "",
        "policy_set_id": offer.policy_set_id or "",
        "with_vat": offer.with_vat,
        "vat_percent": offer.vat_percent or "",
        "speed_download_mbps": offer.speed_download_mbps or "",
        "speed_upload_mbps": offer.speed_upload_mbps or "",
        "guaranteed_speed_limit_at": offer.guaranteed_speed_limit_at or "",
        "guaranteed_speed": offer.guaranteed_speed.value
        if offer.guaranteed_speed
        else GuaranteedSpeedType.none.value,
        "aggregation": offer.aggregation or "",
        "priority": offer.priority or "",
        "available_for_services": offer.available_for_services,
        "show_on_customer_portal": offer.show_on_customer_portal,
        "olt_profile_auto_sync_enabled": offer.olt_profile_auto_sync_enabled,
        "plan_category": offer.plan_category.value
        if offer.plan_category
        else PlanCategory.internet.value,
        "hide_on_admin_portal": offer.hide_on_admin_portal
        if offer.hide_on_admin_portal is not None
        else False,
        "service_description": offer.service_description or "",
        "burst_profile": offer.burst_profile or "",
        "prepaid_period": offer.prepaid_period or "",
        "allowed_change_plan_ids": offer.allowed_change_plan_ids or "",
        "status": offer.status,
        "description": offer.description or "",
        "is_active": offer.is_active,
        "plan_kind": PLAN_KIND_STANDARD,
        "ip_block_size": "",
        "price_id": str(price.id) if price else "",
        "price_type": price.price_type.value
        if price and price.price_type
        else "recurring",
        "price_amount": price.amount if price else "",
        "price_currency": price.currency if price else "NGN",
        "price_billing_cycle": price.billing_cycle.value
        if price and price.billing_cycle
        else "",
        "price_unit": price.unit.value if price and price.unit else "",
        "price_description": price.description if price and price.description else "",
    }
    metadata, cleaned_description = parse_offer_description_metadata(offer.description)
    plan_kind = str(metadata.get("plan_kind") or PLAN_KIND_STANDARD).strip().lower()
    if plan_kind not in PLAN_KINDS:
        plan_kind = PLAN_KIND_STANDARD
    offer_data["plan_kind"] = plan_kind
    offer_data["ip_block_size"] = metadata.get("ip_block_size") or ""
    offer_data["description"] = cleaned_description or ""
    return offer_data, offer_addon_links


def get_offer_or_none(db: Session, offer_id: str) -> CatalogOffer | None:
    """Return an offer for route-level 404 handling."""
    try:
        return catalog_service.offers.get(db=db, offer_id=offer_id)
    except Exception:
        return None


def offer_detail_context(db: Session, offer_id: str) -> dict[str, object] | None:
    """Build the offer detail page context, or None when missing."""
    offer = get_offer_or_none(db, offer_id)
    if offer is None:
        return None

    prices = catalog_service.offer_prices.list(
        db=db,
        offer_id=offer_id,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    subscriptions = catalog_service.subscriptions.list(
        db=db,
        subscriber_id=None,
        offer_id=offer_id,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    plan_meta, cleaned_description = parse_offer_description_metadata(offer.description)
    plan_kind = str(plan_meta.get("plan_kind") or PLAN_KIND_STANDARD)
    fup_policy_rel = getattr(offer, "fup_policy", None)
    if isinstance(fup_policy_rel, list):
        fup_policy_obj = fup_policy_rel[0] if fup_policy_rel else None
    else:
        fup_policy_obj = fup_policy_rel

    context: dict[str, object] = {
        "offer": offer,
        "offer_clean_description": cleaned_description,
        "offer_plan_kind": plan_kind,
        "offer_ip_block_size": plan_meta.get("ip_block_size"),
        "fup_rules_count": len(getattr(fup_policy_obj, "rules", []) or []),
        "fup_return_to": f"/admin/catalog/offers/{offer_id}#plan-fup",
        "prices": prices,
        "subscriptions": subscriptions,
        "activities": build_audit_activities(
            db, "catalog_offer", str(offer_id), limit=10
        ),
    }
    context.update(get_offer_availability(db, str(offer.id)))
    return context


def offer_edit_form_context(
    db: Session,
    offer_id: str,
) -> dict[str, object] | None:
    """Build offer edit form context, or None when missing."""
    offer = get_offer_or_none(db, offer_id)
    if offer is None:
        return None
    offer_data, offer_addon_links = offer_edit_form_data(db, offer_id, offer)
    context = offer_form_context(
        db,
        offer_data,
        offer_addon_links=offer_addon_links,
    )
    context["action_url"] = f"/admin/catalog/offers/{offer_id}/edit"
    return context


def offer_usage_graph_modal_context(
    db: Session,
    offer_id: str,
    *,
    period: str,
) -> dict[str, object]:
    """Build context for the offer usage graph modal."""
    if period not in {"daily", "weekly", "monthly", "quarterly", "annual"}:
        period = "monthly"
    return {
        "offer": get_offer_or_none(db, offer_id),
        "offer_id": offer_id,
        "graph": plan_usage_graph_data(db, offer_id, period=period),
    }


def create_offer_payload(offer: dict[str, object]) -> CatalogOfferCreate:
    return CatalogOfferCreate.model_validate(build_offer_payload_data(offer))


def update_offer_payload(offer: dict[str, object]) -> CatalogOfferUpdate:
    return CatalogOfferUpdate.model_validate(build_offer_payload_data(offer))


def parse_addon_links_from_form(form: FormData) -> list[dict[str, object]]:
    """Parse addon link configurations from form data.

    Form fields are expected in the format:
    - addon_link_{addon_id}: 'true' if addon is selected
    - addon_required_{addon_id}: 'true' if addon is required
    - addon_min_qty_{addon_id}: minimum quantity (optional)
    - addon_max_qty_{addon_id}: maximum quantity (optional)
    """
    addon_configs: list[dict[str, object]] = []
    for key in form.keys():
        if key.startswith("addon_link_"):
            addon_id = key.replace("addon_link_", "")
            if form.get(key) == "true":
                is_required = form.get(f"addon_required_{addon_id}") == "true"
                min_qty_str = _form_str(form, f"addon_min_qty_{addon_id}").strip()
                max_qty_str = _form_str(form, f"addon_max_qty_{addon_id}").strip()

                config: dict[str, object] = {
                    "add_on_id": addon_id,
                    "is_required": is_required,
                    "min_quantity": int(min_qty_str) if min_qty_str else None,
                    "max_quantity": int(max_qty_str) if max_qty_str else None,
                }
                addon_configs.append(config)
    return addon_configs


def build_addon_links_map(
    offer_addon_links: list | None,
) -> dict[str, dict[str, object]]:
    """Build addon links map for the offer form template."""
    addon_links_map: dict[str, dict[str, object]] = {}
    if offer_addon_links:
        for link in offer_addon_links:
            addon_links_map[str(link.add_on_id)] = {
                "is_required": link.is_required,
                "min_quantity": link.min_quantity,
                "max_quantity": link.max_quantity,
            }
    return addon_links_map


def supported_billing_cycles() -> list[str]:
    """Return the full billing-cycle set exposed by the admin offer UI."""
    return [item.value for item in BillingCycle]


def plan_family_values(db: Session) -> list[str]:
    """Plan families for offer classification, settings-driven with the
    built-in tuple as default. Stored as a comma-separated string so operators
    can add families (e.g. "business") without a deploy."""
    raw = settings_spec.resolve_value(db, SettingDomain.catalog, "plan_families")
    if raw:
        values = [
            token.strip().lower().replace(" ", "_") for token in str(raw).split(",")
        ]
        deduped = list(dict.fromkeys(v for v in values if v))
        if deduped:
            return deduped
    return list(PLAN_FAMILY_VALUES)


def offer_form_context(
    db: Session,
    offer: dict[str, object],
    error: str | None = None,
    offer_addon_links: list | None = None,
) -> dict[str, object]:
    """Build context dict for the offer create/edit form template.

    Returns all reference data (enums, related entities) needed by the form.
    """
    default_billing_mode = (
        settings_spec.resolve_value(db, SettingDomain.catalog, "default_billing_mode")
        or BillingMode.prepaid.value
    )
    if not offer.get("billing_mode"):
        offer["billing_mode"] = default_billing_mode

    region_zones = catalog_service.region_zones.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    usage_allowances = catalog_service.usage_allowances.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    sla_profiles = catalog_service.sla_profiles.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    radius_profiles = catalog_service.radius_profiles.list(
        db=db,
        vendor=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    policy_sets = catalog_service.policy_sets.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0
    )
    add_ons = catalog_service.add_ons.list(
        db=db,
        is_active=True,
        addon_type=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    addon_links_map = build_addon_links_map(offer_addon_links)

    all_offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    context: dict[str, object] = {
        "offer": offer,
        "region_zones": region_zones,
        "all_offers": all_offers,
        "usage_allowances": usage_allowances,
        "sla_profiles": sla_profiles,
        "radius_profiles": radius_profiles,
        "policy_sets": policy_sets,
        "add_ons": add_ons,
        "addon_links_map": addon_links_map,
        "service_types": [item.value for item in ServiceType],
        "access_types": [item.value for item in AccessType],
        "price_bases": [item.value for item in PriceBasis],
        "billing_cycles": supported_billing_cycles(),
        "billing_modes": [item.value for item in BillingMode],
        "contract_terms": [item.value for item in ContractTerm],
        "plan_families": plan_family_values(db),
        "offer_statuses": [item.value for item in OfferStatus],
        "price_units": [item.value for item in PriceUnit],
        "price_types": ["recurring", "one_time"],
        "guaranteed_speed_types": [item.value for item in GuaranteedSpeedType],
        "plan_categories": [item.value for item in PlanCategory],
        "plan_kinds": PLAN_KINDS,
        "ip_block_sizes": IP_BLOCK_SIZES,
        "action_url": "/admin/catalog/offers",
    }
    if error:
        context["error"] = error
    return context


def _overview_cohort_url(*, status: str | None = None) -> str:
    """Drill-down into the offer overview list filtered exactly as the tile counts."""
    query = urlencode([("status", status)]) if status else ""
    return f"/admin/catalog?{query}" if query else "/admin/catalog"


def _subscriptions_cohort_url(*, status: str | None = None) -> str:
    """Drill-down into the subscriptions list filtered exactly as the tile counts."""
    query = urlencode([("status", status)]) if status else ""
    return (
        f"/admin/catalog/subscriptions?{query}"
        if query
        else "/admin/catalog/subscriptions"
    )


def _stat_int(stats: dict[str, object], key: str) -> int:
    value = stats.get(key)
    if isinstance(value, (int, float, Decimal, str)):
        return int(value)
    return 0


def catalog_overview_kpis(stats: dict[str, object]) -> dict[str, Kpi]:
    """Project the catalog overview headline counts as KPI-parity tiles.

    Each ``cohort_url`` drills into the exact filtered list that produced the
    number, so a tile total and the list it opens can never diverge. The
    subscription tile crosses into the subscriptions list, where its ``active``
    filter matches the count query.
    """
    return {
        "active_offers": Kpi(
            label="Active Offers",
            value=StateValue.present(_stat_int(stats, "active_count")),
            cohort_url=_overview_cohort_url(status=OfferStatus.active.value),
            tone=StatusTone.positive,
        ),
        "total_plans": Kpi(
            label="Total Plans",
            value=StateValue.present(_stat_int(stats, "total_count")),
            cohort_url=_overview_cohort_url(),
        ),
        "active_subscriptions": Kpi(
            label="Active Subscriptions",
            value=StateValue.present(_stat_int(stats, "total_subscriptions")),
            cohort_url=_subscriptions_cohort_url(
                status=SubscriptionStatus.active.value
            ),
            tone=StatusTone.info,
        ),
        "archived": Kpi(
            label="Archived",
            value=StateValue.present(_stat_int(stats, "archived_count")),
            cohort_url=_overview_cohort_url(status=OfferStatus.archived.value),
        ),
    }


def dashboard_stats(db: Session) -> dict[str, object]:
    """Return catalog dashboard KPIs and chart data from core service.

    The four headline counts are projected as ``Kpi`` contracts under ``kpis``;
    chart datasets stay as raw dicts for the client-side chart renderers.
    """
    stats = catalog_service.offers.get_dashboard_stats(db)
    stats["kpis"] = catalog_overview_kpis(stats)
    return stats


def overview_page_data(
    db: Session,
    *,
    status: str | None = None,
    plan_kind: str | None = None,
    plan_category: str | None = None,
    plan_family: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, object]:
    """Build page data for the catalog overview route.

    Returns offers, subscription counts, pagination, and enum value lists.
    """
    stmt = select(CatalogOffer)
    if search:
        stmt = stmt.where(
            CatalogOffer.name.ilike(f"%{search}%")
            | CatalogOffer.code.ilike(f"%{search}%")
        )
    if status:
        stmt = stmt.where(CatalogOffer.status == OfferStatus(status))
    normalized_plan_category = str(plan_category or "").strip().lower()
    if normalized_plan_category and normalized_plan_category in {
        pc.value for pc in PlanCategory
    }:
        stmt = stmt.where(
            CatalogOffer.plan_category == PlanCategory(normalized_plan_category)
        )
    normalized_plan_family = str(plan_family or "").strip().lower()
    if normalized_plan_family == "unclassified":
        stmt = stmt.where(
            (CatalogOffer.plan_family.is_(None)) | (CatalogOffer.plan_family == "")
        )
    elif normalized_plan_family:
        stmt = stmt.where(
            func.lower(CatalogOffer.plan_family) == normalized_plan_family
        )
    normalized_plan_kind = str(plan_kind or "").strip().lower()
    if normalized_plan_kind in {PLAN_KIND_IP_ADDRESS, PLAN_KIND_DEVICE_REPLACEMENT}:
        stmt = stmt.where(
            CatalogOffer.description.ilike(f"%[plan_kind:{normalized_plan_kind}]%")
        )
    elif normalized_plan_kind == PLAN_KIND_STANDARD:
        stmt = stmt.where(
            (CatalogOffer.description.is_(None))
            | (
                (~CatalogOffer.description.ilike("%[plan_kind:ip_address]%"))
                & (~CatalogOffer.description.ilike("%[plan_kind:device_replacement]%"))
            )
        )

    total: int = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page

    offers = db.scalars(
        stmt.order_by(CatalogOffer.created_at.desc()).limit(per_page).offset(offset)
    ).all()
    offer_plan_metadata = {}
    for offer in offers:
        metadata, _ = parse_offer_description_metadata(offer.description)
        plan = str(metadata.get("plan_kind") or PLAN_KIND_STANDARD).strip().lower()
        if plan not in PLAN_KINDS:
            plan = PLAN_KIND_STANDARD
        offer_plan_metadata[str(offer.id)] = {
            "plan_kind": plan,
            "ip_block_size": metadata.get("ip_block_size"),
        }

    offer_ids = [offer.id for offer in offers]
    offer_subscription_counts: dict[str, int] = {}
    offer_active_subscription_counts: dict[str, int] = {}
    if offer_ids:
        rows = db.execute(
            select(Subscription.offer_id, func.count(Subscription.id))
            .where(Subscription.offer_id.in_(offer_ids))
            .group_by(Subscription.offer_id)
        ).all()
        offer_subscription_counts = {str(row[0]): row[1] for row in rows}

        active_rows = db.execute(
            select(Subscription.offer_id, func.count(Subscription.id))
            .where(Subscription.offer_id.in_(offer_ids))
            .where(Subscription.status == SubscriptionStatus.active)
            .group_by(Subscription.offer_id)
        ).all()
        offer_active_subscription_counts = {str(row[0]): row[1] for row in active_rows}

    radius_profiles = catalog_service.radius_profiles.list(
        db=db,
        vendor=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    return {
        "offers": offers,
        "offer_subscription_counts": offer_subscription_counts,
        "offer_active_subscription_counts": offer_active_subscription_counts,
        "status": status,
        "plan_kind": normalized_plan_kind or "",
        "plan_category": normalized_plan_category or "",
        "plan_family": normalized_plan_family or "",
        "plan_families": plan_family_values(db),
        "plan_categories": [item.value for item in PlanCategory],
        "search": search,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "radius_profiles": radius_profiles,
        "service_types": [item.value for item in ServiceType],
        "access_types": [item.value for item in AccessType],
        "price_bases": [item.value for item in PriceBasis],
        "billing_cycles": supported_billing_cycles(),
        "contract_terms": [item.value for item in ContractTerm],
        "offer_statuses": [item.value for item in OfferStatus],
        "offer_plan_metadata": offer_plan_metadata,
    }


class OfferNameConflict(DomainError):
    """Two offers on sale under one name (adapter: re-render with the message).

    Typed rather than an HTTPException: this module is a service, and the
    transport-boundary guard in tests/architecture forbids services reaching
    for FastAPI. The form handlers below catch it and re-render, which is a
    better answer for an operator than a 400 anyway.
    """


class MissingSpeedReductionRule(DomainError):
    """A capped family put on sale with nothing that reduces speed."""


def assert_sellable_capped_offer_can_enforce(
    db: Session,
    *,
    offer_id: str | None,
    plan_family: str | None,
    available_for_services: bool,
) -> None:
    """Refuse to put a capped-family offer on sale with no speed-reduction rule.

    ``home_flex`` is defined by its cap: past the allowance you keep working at
    a reduced speed rather than losing service (§1). Production carries five
    home_flex offers and **63 subscribers with zero FUP rules between them** —
    a product sold on a limit that has never once been enforced. The failure is
    silent at every layer, which is why it survived: the offer looks complete,
    the family is set, and nothing reads back as missing.

    **What this proves, precisely:** that an active ``reduce_speed`` rule
    exists under an active policy. It does **not** prove the ladder is complete
    or that the threshold is reachable — a rule can sit behind a chain whose
    prerequisite never fires, or inside a time window that never opens. Named
    for the weaker claim on purpose; calling it a ladder check would overstate
    it and invite someone to trust it for more.

    Checked at the point of sale rather than at creation, because an offer must
    exist before rules can be attached to it. The companion guard in
    ``fup._assert_not_last_enforcing_rule`` keeps the invariant afterwards.

    Deliberately does **not** check thresholds. Whether the cap is 4 GB/day or
    40 is a commercial decision, and a validator that invented a default would
    be a worse outcome than one that refuses: it would silently enforce a
    number nobody chose.
    """
    from app.services.fup import (
        FUP_REQUIRED_FAMILIES,
        active_speed_reduction_rule_ids,
    )

    if not available_for_services:
        return
    if (plan_family or "") not in FUP_REQUIRED_FAMILIES:
        return
    if not offer_id:
        # Nothing to look up yet; the update path re-checks once it exists.
        return
    if active_speed_reduction_rule_ids(db, offer_id):
        return

    raise MissingSpeedReductionRule(
        code="catalog.offer.missing_speed_reduction_rule",
        message=(
            f"A {plan_family} offer cannot be made available for sale without "
            "an active FUP rule that reduces speed. Add one on the offer's FUP "
            "screen first — the cap is what defines this family."
        ),
        details={"offer_id": str(offer_id), "plan_family": plan_family},
    )


def assert_sellable_name_is_unique(
    db: Session, name: str, *, exclude_offer_id: str | None = None
) -> None:
    """Refuse a second sellable offer with the same name.

    A name is not an identity, but every picker presents it as one. Production
    carried two active offers both called "25 Mbps Fiber" — one at N537,500 and
    one at N0.00 — and two subscriptions were created against the free one on
    the same day. A separate pair both called "Unlimited Pro" caused a 50 Mbps
    plan with three live customers to be withdrawn from sale by a maintenance
    script matching on name.

    Scoped to *sellable* offers rather than all of them: a retired offer keeping
    its name is harmless and preserves history. The ambiguity only matters where
    someone is choosing.

    The database enforces the same rule, since imports do not come through here.
    This exists so an operator gets a sentence instead of a constraint violation.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return
    query = db.query(CatalogOffer).filter(
        CatalogOffer.name == cleaned,
        CatalogOffer.is_active.is_(True),
        CatalogOffer.available_for_services.is_(True),
    )
    if exclude_offer_id:
        query = query.filter(CatalogOffer.id != exclude_offer_id)
    clash = query.first()
    if clash is not None:
        raise OfferNameConflict(
            code="catalog.offer.duplicate_sellable_name",
            message=(
                f"Another offer available for sale is already named "
                f"{cleaned!r} (code {clash.code or '—'}). Two sellable offers "
                "with one name cannot be told apart when choosing a plan. "
                "Rename this one, or withdraw the other from sale first."
            ),
            details={"conflicting_offer_id": str(clash.id)},
        )


def create_offer_with_audit(
    db: Session,
    offer: dict[str, object],
    form: FormData,
    request: object,
    actor_id: str | None,
) -> object:
    """Create offer, link radius profile, sync addons, create price, and log audit.

    Returns the created offer ORM object.
    """
    assert_sellable_name_is_unique(db, str(offer.get("name") or ""))
    payload = create_offer_payload(offer)
    created_offer = catalog_service.offers.create(
        db=db,
        payload=payload,
        actor_id=actor_id,
        actor_type="user",
    )

    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="catalog_offer",
        entity_id=str(created_offer.id),
        actor_id=actor_id,
        metadata={
            "name": created_offer.name,
            "service_type": created_offer.service_type.value
            if created_offer.service_type
            else None,
        },
    )

    ensure_offer_radius_profile(
        db,
        created_offer,
        explicit_profile_id=str(offer.get("radius_profile_id") or "").strip() or None,
    )

    addon_configs = parse_addon_links_from_form(form)
    if addon_configs:
        catalog_service.offer_addons.sync(
            db=db,
            offer_id=str(created_offer.id),
            addon_configs=addon_configs,
        )

    if offer["price_amount"]:
        created_price = create_recurring_price(
            db,
            str(created_offer.id),
            offer,
            actor_id=actor_id,
            actor_type="user",
        )
        if created_price:
            log_audit_event(
                db=db,
                request=request,
                action="price_created",
                entity_type="catalog_offer",
                entity_id=str(created_offer.id),
                actor_id=actor_id,
                metadata={
                    "price_amount": str(created_price.amount),
                    "currency": created_price.currency,
                },
            )

    return created_offer


def update_offer_with_audit(
    db: Session,
    offer_id: str,
    existing_offer: object,
    offer_data: dict[str, object],
    form: FormData,
    request: object,
    actor_id: str | None,
) -> object:
    """Update offer, sync radius/addons/price, and log audit.

    Returns the updated offer ORM object.
    """
    # Checked on update too: a rename, or putting a withdrawn offer back on
    # sale, can create the same collision as a fresh create.
    assert_sellable_name_is_unique(
        db,
        str(offer_data.get("name") or getattr(existing_offer, "name", "") or ""),
        exclude_offer_id=offer_id,
    )
    # Checked here rather than on create: rules hang off the offer, so it must
    # exist before it can have a ladder. This is the transition that matters —
    # putting a capped product on sale.
    assert_sellable_capped_offer_can_enforce(
        db,
        offer_id=offer_id,
        plan_family=str(
            offer_data.get("plan_family")
            or getattr(existing_offer, "plan_family", "")
            or ""
        ),
        available_for_services=bool(
            offer_data.get(
                "available_for_services",
                getattr(existing_offer, "available_for_services", False),
            )
        ),
    )
    price_id = str(offer_data.get("price_id") or "").strip()
    if price_id and offer_data.get("price_amount"):
        existing_price = db.get(OfferPrice, coerce_uuid(price_id))
        if existing_price is not None:
            candidate_price = {
                "price_type": str(offer_data.get("price_type") or "recurring"),
                "amount": offer_data["price_amount"],
                "currency": offer_data["price_currency"],
                "billing_cycle": offer_data.get("price_billing_cycle"),
                # The edit form represents an unset optional unit as "" while
                # the database stores it as NULL. Normalize before governance
                # comparison so a non-financial offer edit does not look like
                # a protected price-unit mutation.
                "unit": offer_data.get("price_unit") or None,
            }
            price_changes = catalog_billing_governance.billing_field_changes(
                existing_price,
                candidate_price,
            )
            catalog_billing_governance.assert_offer_price_update_safe(
                db,
                existing_price,
                price_changes,
            )

    before_snapshot = model_to_dict(existing_offer)
    previous_profile_id = get_linked_radius_profile_id(db, offer_id)
    payload = update_offer_payload(offer_data)
    updated_offer = catalog_service.offers.update(
        db=db,
        offer_id=offer_id,
        payload=payload,
        actor_id=actor_id,
        actor_type="user",
    )
    after_snapshot = model_to_dict(updated_offer)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    staged_profile_sync_tasks = []
    if _should_stage_profile_sync_tasks(changes):
        staged_profile_sync_tasks = (
            enqueue_offer_profile_sync_tasks_for_existing_bundles(
                db,
                offer=updated_offer,
                trigger="catalog_offer_update",
                requested_by=actor_id,
            )
        )
        if staged_profile_sync_tasks:
            metadata = metadata or {}
            metadata["profile_sync_tasks_staged"] = len(staged_profile_sync_tasks)  # type: ignore[assignment]

    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="catalog_offer",
        entity_id=str(updated_offer.id),
        actor_id=actor_id,
        metadata=metadata,
    )

    ensure_offer_radius_profile(
        db,
        updated_offer,
        explicit_profile_id=str(offer_data.get("radius_profile_id") or "").strip()
        or None,
        previous_profile_id=previous_profile_id,
    )

    addon_configs = parse_addon_links_from_form(form)
    catalog_service.offer_addons.sync(
        db=db,
        offer_id=offer_id,
        addon_configs=addon_configs,
    )

    price, price_action = upsert_recurring_price(
        db,
        offer_id,
        offer_data,
        actor_id=actor_id,
        actor_type="user",
    )
    if price and price_action:
        log_audit_event(
            db=db,
            request=request,
            action=f"price_{price_action}",
            entity_type="catalog_offer",
            entity_id=str(offer_id),
            actor_id=actor_id,
            metadata={
                "price_amount": str(price.amount),
                "currency": price.currency,
            },
        )

    return updated_offer


def _should_stage_profile_sync_tasks(changes: dict[str, object]) -> bool:
    watched_fields = {
        "name",
        "code",
        "status",
        "is_active",
        "access_type",
        "plan_category",
        "speed_download_mbps",
        "speed_upload_mbps",
        "olt_profile_auto_sync_enabled",
    }
    return bool(watched_fields.intersection(changes))


def handle_offer_create_form(
    db: Session,
    *,
    form: FormData,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Validate and create a catalog offer from the admin form."""
    return_to_raw = form.get("return_to")
    return_to = return_to_raw.strip() if isinstance(return_to_raw, str) else ""
    offer = parse_offer_form(form)
    error = validate_offer_form(offer)
    if error:
        return {
            "form_context": offer_form_context(
                db,
                offer,
                error or "Please correct the highlighted fields.",
            ),
        }

    try:
        create_offer_with_audit(db, offer, form, request, actor_id)
        return {"redirect_url": return_to or "/admin/catalog/offers"}
    except OfferNameConflict as exc:
        # Re-render with the message: the operator needs to know which offer
        # collides so they can rename or withdraw it.
        error = exc.message
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]

    return {
        "form_context": offer_form_context(
            db,
            offer,
            error or "Please correct the highlighted fields.",
        ),
    }


def handle_offer_update_form(
    db: Session,
    *,
    offer_id: str,
    form: FormData,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Validate and update a catalog offer from the admin form."""
    existing_offer = get_offer_or_none(db, offer_id)
    if existing_offer is None:
        return {"not_found": True}

    offer_data = parse_offer_form(form)
    error = validate_offer_form(offer_data)
    if error:
        context = offer_form_context(db, offer_data, error)
        context["action_url"] = f"/admin/catalog/offers/{offer_id}/edit"
        return {"form_context": context}

    try:
        update_offer_with_audit(
            db,
            offer_id,
            existing_offer,
            offer_data,
            form,
            request,
            actor_id,
        )
        return {"redirect_url": f"/admin/catalog/offers/{offer_id}"}
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = offer_form_context(
        db,
        offer_data,
        error or "Please correct the highlighted fields.",
    )
    context["action_url"] = f"/admin/catalog/offers/{offer_id}/edit"
    return {"form_context": context}


def plan_usage_graph_data(
    db: Session,
    offer_id: str,
    *,
    period: str = "monthly",
    months: int = 12,
) -> dict[str, object]:
    """Return subscription count data for a plan over time.

    Groups subscriptions by created_at date bucket and returns labels + datasets
    suitable for Chart.js rendering.

    Args:
        db: Database session.
        offer_id: The catalog offer UUID.
        period: Grouping period — "daily", "weekly", "monthly", "quarterly", or "annual".
        months: How many months of history to include.

    Returns:
        Dict with labels, total_counts, active_counts, and summary stats.
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=months * 30)

    # Count total subscriptions created per period
    if period == "daily":
        date_trunc = func.date_trunc("day", Subscription.created_at)
    elif period == "weekly":
        date_trunc = func.date_trunc("week", Subscription.created_at)
    elif period == "quarterly":
        date_trunc = func.date_trunc("quarter", Subscription.created_at)
    elif period == "annual":
        date_trunc = func.date_trunc("year", Subscription.created_at)
    else:
        date_trunc = func.date_trunc("month", Subscription.created_at)

    stmt = (
        select(
            date_trunc.label("period"),
            func.count(Subscription.id).label("total"),
            func.count(
                case(
                    (Subscription.status == SubscriptionStatus.active, Subscription.id),
                )
            ).label("active"),
        )
        .where(Subscription.offer_id == offer_id)
        .where(Subscription.created_at >= start_date)
        .group_by("period")
        .order_by("period")
    )

    rows = db.execute(stmt).all()

    labels: list[str] = []
    total_counts: list[int] = []
    active_counts: list[int] = []

    for row in rows:
        period_date = row.period
        if period == "daily":
            labels.append(period_date.strftime("%b %d"))
        elif period == "weekly":
            labels.append(f"W{period_date.strftime('%U')} {period_date.strftime('%b')}")
        elif period == "quarterly":
            quarter = ((period_date.month - 1) // 3) + 1
            labels.append(f"Q{quarter} {period_date.year}")
        elif period == "annual":
            labels.append(period_date.strftime("%Y"))
        else:
            labels.append(period_date.strftime("%b %Y"))
        total_counts.append(row.total)
        active_counts.append(row.active)

    # Summary stats
    total_now = (
        db.scalar(
            select(func.count(Subscription.id)).where(Subscription.offer_id == offer_id)
        )
        or 0
    )
    active_now = (
        db.scalar(
            select(func.count(Subscription.id))
            .where(Subscription.offer_id == offer_id)
            .where(Subscription.status == SubscriptionStatus.active)
        )
        or 0
    )
    max_total = max(total_counts) if total_counts else 0
    avg_total = round(sum(total_counts) / len(total_counts), 1) if total_counts else 0

    return {
        "labels": labels,
        "total_counts": total_counts,
        "active_counts": active_counts,
        "total_now": total_now,
        "active_now": active_now,
        "max_total": max_total,
        "avg_total": avg_total,
        "period": period,
    }
