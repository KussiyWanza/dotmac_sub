"""Subscription management services.

Provides services for Subscriptions and SubscriptionAddOns.
"""

import logging
from calendar import monthrange
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import (
    AccessCredential,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    ContractTerm,
    NasDevice,
    NasDeviceStatus,
    OfferPrice,
    OfferRadiusProfile,
    OfferVersionPrice,
    PriceType,
    RadiusProfile,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.schemas.catalog import (
    SubscriptionAddOnCreate,
    SubscriptionAddOnUpdate,
    SubscriptionCreate,
    SubscriptionUpdate,
)
from app.services import settings_spec
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    round_money,
    validate_enum,
)
from app.services.crud import CRUDManager
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.query_builders import apply_optional_equals
from app.services.response import ListResponseMixin
from app.validators import catalog as catalog_validators

logger = logging.getLogger(__name__)


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _subscription_billing_mode_for_write(
    db: Session,
    *,
    subscriber_id: str,
    offer_id: str,
    requested_mode: BillingMode | None,
) -> BillingMode:
    from app.services.billing_profile import (
        BillingModeWriteRejected,
        resolve_subscription_billing_mode_for_write,
    )

    try:
        return resolve_subscription_billing_mode_for_write(
            db,
            account_id=subscriber_id,
            offer_id=offer_id,
            requested_mode=requested_mode,
        )
    except BillingModeWriteRejected as exc:
        raise HTTPException(
            status_code=409,
            detail=(f"Subscription billing mode is not aligned: {exc.reason.value}"),
        ) from exc


def _add_months(value: datetime, months: int) -> datetime:
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _resolve_billing_cycle(
    db: Session,
    offer_id: str,
    offer_version_id: str | None,
    override: BillingCycle | None = None,
) -> BillingCycle:
    # Subscription-owned cadence wins over the offer price (SOT precedence:
    # subscription.billing_cycle -> offer/version price -> offer header -> monthly).
    if override is not None:
        return override
    if offer_version_id:
        version_price: OfferVersionPrice | None = (
            db.query(OfferVersionPrice)
            .filter(OfferVersionPrice.offer_version_id == offer_version_id)
            .filter(OfferVersionPrice.price_type == PriceType.recurring)
            .filter(OfferVersionPrice.is_active.is_(True))
            .first()
        )
        if version_price and version_price.billing_cycle:
            return version_price.billing_cycle
    offer_price: OfferPrice | None = (
        db.query(OfferPrice)
        .filter(OfferPrice.offer_id == offer_id)
        .filter(OfferPrice.price_type == PriceType.recurring)
        .filter(OfferPrice.is_active.is_(True))
        .first()
    )
    if offer_price and offer_price.billing_cycle:
        return offer_price.billing_cycle
    offer = db.get(CatalogOffer, offer_id)
    return (
        offer.billing_cycle if offer and offer.billing_cycle else BillingCycle.monthly
    )


def _compute_next_billing_at(start_at: datetime, cycle: BillingCycle) -> datetime:
    """Compute the next billing date based on the billing cycle.

    Args:
        start_at: The reference date (subscription start or last billing date)
        cycle: The billing cycle (daily, weekly, monthly, quarterly, annual)

    Returns:
        The next billing date
    """
    if cycle == BillingCycle.daily:
        return start_at + timedelta(days=1)
    if cycle == BillingCycle.weekly:
        return start_at + timedelta(weeks=1)
    if cycle == BillingCycle.quarterly:
        return _add_months(start_at, 3)
    if cycle == BillingCycle.annual:
        return _add_months(start_at, 12)
    # Default to monthly
    return _add_months(start_at, 1)


def _compute_contract_end_at(start_at: datetime, term: ContractTerm) -> datetime | None:
    if term == ContractTerm.twelve_month:
        return _add_months(start_at, 12)
    if term == ContractTerm.twentyfour_month:
        return _add_months(start_at, 24)
    return None


def _sync_credentials_to_radius(db: Session, subscriber_id) -> None:
    """Reconcile internal/external RADIUS state for active subscriptions."""
    try:
        from app.services.radius import reconcile_subscription_connectivity

        active_subscriptions = (
            db.query(Subscription)
            .filter(Subscription.subscriber_id == subscriber_id)
            .filter(Subscription.status == SubscriptionStatus.active)
            .all()
        )
        for subscription in active_subscriptions:
            reconcile_subscription_connectivity(
                db, str(subscription.id)
            ).require_projected()
    except Exception as exc:
        logger.warning(
            "Failed to reconcile RADIUS state for subscriber %s: %s",
            subscriber_id,
            exc,
        )


def _select_nas_for_subscriber(db: Session, subscriber_id: str):
    """Select an appropriate NAS device for a subscriber based on their POP site.

    Selection priority:
    1. Active NAS devices in subscriber's POP site
    2. Prefer NAS with lowest current_subscriber_count (load balancing)
    3. Fall back to None if no suitable NAS found

    Returns:
        NasDevice ID or None
    """
    from app.models.catalog import NasDevice, NasDeviceStatus
    from app.models.network_monitoring import PopSite
    from app.models.subscriber import Subscriber

    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber or not subscriber.pop_site_id:
        return None

    pop_site = db.get(PopSite, subscriber.pop_site_id)
    if not pop_site:
        return None

    # Find active NAS devices in this POP site, ordered by load
    nas_device = (
        db.query(NasDevice)
        .filter(NasDevice.pop_site_id == pop_site.id)
        .filter(NasDevice.is_active.is_(True))
        .filter(NasDevice.status == NasDeviceStatus.active)
        .order_by(
            NasDevice.current_subscriber_count.asc().nullsfirst(),
            NasDevice.created_at.asc(),
        )
        .first()
    )

    if nas_device:
        logger.debug(
            "Auto-selected NAS device %s for subscriber %s (POP: %s)",
            nas_device.id,
            subscriber_id,
            pop_site.name,
        )
        return nas_device.id

    logger.debug(
        "No active NAS device found in POP %s for subscriber %s",
        pop_site.name,
        subscriber_id,
    )
    return None


def _auto_generate_pppoe(
    db: Session,
    subscription: Subscription,
) -> None:
    """Auto-generate PPPoE credentials for newly activated subscriptions."""
    from app.models.catalog import AccessCredential
    from app.services.pppoe_credentials import auto_generate_pppoe_credential

    profile_id = subscription.radius_profile_id
    auto_generate_pppoe_credential(
        db,
        str(subscription.subscriber_id),
        radius_profile_id=str(profile_id) if profile_id else None,
        subscription_id=str(subscription.id),
    )
    active_credential = (
        db.query(AccessCredential)
        .filter(
            AccessCredential.subscriber_id == subscription.subscriber_id,
            AccessCredential.subscription_id == subscription.id,
            AccessCredential.is_active.is_(True),
        )
        .first()
    )
    if active_credential is None:
        raise RuntimeError(
            f"PPPoE credential is required before activating subscription {subscription.id}."
        )
    # Keep subscription.login in sync with the credential actually minted. The
    # canonical and sequence-fallback paths can yield different usernames, so we
    # bind login to the real credential rather than re-deriving it (which would
    # leave login empty/stale for non-canonical subscriber numbers).
    if not str(subscription.login or "").strip():
        subscription.login = active_credential.username


def _resolve_offer_radius_profile_id(db: Session, offer_id: str | None):
    if not offer_id:
        return None
    link = (
        db.query(OfferRadiusProfile)
        .filter(OfferRadiusProfile.offer_id == offer_id)
        .first()
    )
    return link.profile_id if link else None


def _speed_kbps_from_mbps(value: int | None) -> int | None:
    if value is None:
        return None
    return int(value) * 1000


def _profile_speeds_match(
    profile: RadiusProfile | None,
    *,
    download_kbps: int | None,
    upload_kbps: int | None,
) -> bool:
    if profile is None or download_kbps is None:
        return False
    if profile.download_speed != download_kbps:
        return False
    if upload_kbps is not None and profile.upload_speed != upload_kbps:
        return False
    return True


def _radius_profile_matches_offer(
    db: Session,
    profile_id,
    offer_id,
) -> bool:
    if not profile_id or not offer_id:
        return False
    profile = db.get(RadiusProfile, profile_id)
    offer = db.get(CatalogOffer, offer_id)
    if not profile or not offer:
        return False
    return _profile_speeds_match(
        profile,
        download_kbps=_speed_kbps_from_mbps(offer.speed_download_mbps),
        upload_kbps=_speed_kbps_from_mbps(offer.speed_upload_mbps),
    )


def _radius_profiles_are_speed_equivalent(
    db: Session,
    profile_id,
    expected_profile_id,
) -> bool:
    if not profile_id or not expected_profile_id:
        return False
    profile = db.get(RadiusProfile, profile_id)
    expected = db.get(RadiusProfile, expected_profile_id)
    if not profile or not expected or expected.download_speed is None:
        return False
    return _profile_speeds_match(
        profile,
        download_kbps=expected.download_speed,
        upload_kbps=expected.upload_speed,
    )


def _radius_profile_inherits_previous_offer(
    db: Session,
    profile_id,
    previous_offer_id,
    previous_default_profile_id,
) -> bool:
    if profile_id is None:
        return True
    if previous_default_profile_id and profile_id == previous_default_profile_id:
        return True
    return _radius_profiles_are_speed_equivalent(
        db,
        profile_id,
        previous_default_profile_id,
    ) or _radius_profile_matches_offer(db, profile_id, previous_offer_id)


def apply_offer_radius_profile(
    db: Session,
    subscription: Subscription,
    *,
    previous_offer_id=None,
    target_profile_id=None,
    force: bool = False,
    sync_credentials: bool = True,
):
    """Keep subscription and inherited credentials aligned to the offer profile."""
    previous_default = _resolve_offer_radius_profile_id(
        db, str(previous_offer_id) if previous_offer_id else None
    )
    resolved_target = (
        target_profile_id
        if target_profile_id is not None
        else _resolve_offer_radius_profile_id(db, str(subscription.offer_id))
    )

    inherited_subscription = _radius_profile_inherits_previous_offer(
        db,
        subscription.radius_profile_id,
        previous_offer_id,
        previous_default,
    )
    if force or inherited_subscription:
        subscription.radius_profile_id = resolved_target

    if sync_credentials:
        credentials = (
            db.query(AccessCredential)
            .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
            .filter(AccessCredential.is_active.is_(True))
            .all()
        )
        for credential in credentials:
            inherited_credential = _radius_profile_inherits_previous_offer(
                db,
                credential.radius_profile_id,
                previous_offer_id,
                previous_default,
            )
            if force or inherited_credential:
                credential.radius_profile_id = resolved_target

    return resolved_target


def _validate_plan_change(
    db: Session,
    subscription: Subscription,
    new_offer_id: str,
) -> None:
    """Validate that a plan change is allowed.

    Checks:
    - New offer exists and is active
    - Service type compatibility (residential ↔ residential only)
    - Regional availability (if offer has region_zone_id)
    - Billing mode compatibility (prepaid ↔ prepaid, postpaid ↔ postpaid)
    """
    from app.services.subscription_billing_treatments import (
        subscription_has_open_billing_treatment,
    )

    if subscription_has_open_billing_treatment(db, subscription.id):
        raise HTTPException(
            status_code=409,
            detail=(
                "This service has an approved complimentary or sponsored billing "
                "treatment. Revoke and reapprove that treatment before changing "
                "the plan."
            ),
        )

    new_offer = db.get(CatalogOffer, new_offer_id)
    if not new_offer:
        raise HTTPException(status_code=404, detail="Target offer not found")

    old_offer = db.get(CatalogOffer, subscription.offer_id)

    # Service type check: don't allow residential → business cross-change
    if old_offer and new_offer:
        old_type = getattr(old_offer, "service_type", None)
        new_type = getattr(new_offer, "service_type", None)
        if old_type and new_type and old_type != new_type:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot change from {old_type.value} to {new_type.value} plan. "
                f"Upgrade/downgrade must be within the same service class.",
            )

    # Billing mode check: prepaid stays prepaid, postpaid stays postpaid
    sub_mode = subscription.billing_mode
    if sub_mode and new_offer:
        new_mode = getattr(new_offer, "billing_mode", None)
        if new_mode and sub_mode != new_mode:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot change from {sub_mode.value} to {new_mode.value} billing. "
                f"Plan changes must stay within the same billing mode.",
            )

    # Region availability check
    if new_offer.region_zone_id:
        from app.models.subscriber import Subscriber

        subscriber = db.get(Subscriber, subscription.subscriber_id)
        if subscriber and hasattr(subscriber, "pop_site_id") and subscriber.pop_site_id:
            from app.models.network_monitoring import PopSite

            pop = db.get(PopSite, subscriber.pop_site_id)
            if (
                pop
                and pop.zone_id
                and str(pop.zone_id) != str(new_offer.region_zone_id)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"Offer '{new_offer.name}' is not available in your service region.",
                )


def _billing_cycle_start(next_billing_at: datetime, cycle: BillingCycle) -> datetime:
    if cycle == BillingCycle.daily:
        return next_billing_at - timedelta(days=1)
    if cycle == BillingCycle.weekly:
        return next_billing_at - timedelta(weeks=1)
    if cycle == BillingCycle.quarterly:
        return _add_months(next_billing_at, -3)
    if cycle == BillingCycle.annual:
        return _add_months(next_billing_at, -12)
    return _add_months(next_billing_at, -1)


def _offer_recurring_price_amount(db: Session, offer_id) -> Decimal:
    """Return the active recurring amount for an offer, or zero."""
    price = (
        db.query(OfferPrice.amount)
        .filter(
            OfferPrice.offer_id == offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    return Decimal(str(price[0])) if price else Decimal("0")


def _plan_change_text_setting(db: Session, key: str, default: str) -> str:
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    text = str(value or "").strip()
    return text if text else default


def _plan_change_decimal_setting(
    db: Session, key: str, default: str = "0.00"
) -> Decimal:
    try:
        return Decimal(_plan_change_text_setting(db, key, default))
    except Exception:
        return Decimal(default)


def _apply_plan_change_policy(
    db: Session,
    proration: dict,
    *,
    old_price: Decimal,
    new_price: Decimal,
) -> dict:
    """Apply billing-domain plan-change policy settings to proration output."""
    result = dict(proration)
    refund_policy = _plan_change_text_setting(db, "refund_policy", "none").lower()
    invoice_timing = _plan_change_text_setting(
        db, "invoice_timing", "immediate"
    ).lower()
    minimum_invoice_amount = _plan_change_decimal_setting(db, "minimum_invoice_amount")
    fee_tax_rate = _plan_change_decimal_setting(db, "fee_tax_rate")

    net_amount = Decimal(str(result.get("net_amount", "0")))
    credit_amount = Decimal(str(result.get("credit_amount", "0")))

    if net_amount < 0 and refund_policy == "none":
        result["credit_amount"] = Decimal("0.00")
        result["net_amount"] = Decimal("0.00")
        net_amount = Decimal("0.00")
        credit_amount = Decimal("0.00")

    fee_amount = Decimal("0.00")
    if new_price > old_price:
        fee_amount = _plan_change_decimal_setting(db, "upgrade_fee")
    elif new_price < old_price:
        fee_amount = _plan_change_decimal_setting(db, "downgrade_fee")

    if fee_amount > 0:
        tax_multiplier = Decimal("1.00") + (fee_tax_rate / Decimal("100"))
        fee_total = (fee_amount * tax_multiplier).quantize(Decimal("0.01"))
        result["fee_amount"] = fee_total
        net_amount = (Decimal(str(result.get("net_amount", "0"))) + fee_total).quantize(
            Decimal("0.01")
        )
        result["net_amount"] = net_amount

    result["invoice_timing"] = invoice_timing
    result["minimum_invoice_amount"] = minimum_invoice_amount
    result["generate_now"] = (
        invoice_timing == "immediate"
        and abs(Decimal(str(result.get("net_amount", "0")))) >= minimum_invoice_amount
    )
    result["credit_amount"] = credit_amount
    return result


def _calculate_proration(
    db: Session,
    subscription: Subscription,
    new_offer_id: str,
    *,
    effective_at: datetime | None = None,
) -> dict:
    """Calculate proration amounts for a mid-cycle plan change.

    Uses exact remaining and total cycle seconds with Decimal arithmetic.

    Returns dict with:
    - credit_amount: unused portion of current plan to refund
    - charge_amount: prorated charge for new plan
    - net_amount: charge - credit (positive = customer owes, negative = credit)
    - days_remaining: days left in current billing cycle
    - days_in_cycle: total days in billing cycle
    """
    now = _ensure_utc(effective_at) or datetime.now(UTC)
    next_billing = subscription.next_billing_at
    if not next_billing:
        return {
            "effective_at": now,
            "credit_amount": Decimal("0.00"),
            "charge_amount": Decimal("0.00"),
            "net_amount": Decimal("0.00"),
            "days_remaining": 0,
            "days_in_cycle": 0,
            "remaining_cycle_seconds": 0,
            "total_cycle_seconds": 0,
            "remaining_ratio": Decimal("0"),
        }

    if next_billing.tzinfo is None:
        next_billing = next_billing.replace(tzinfo=UTC)
    cycle = _resolve_billing_cycle(
        db,
        str(subscription.offer_id),
        str(subscription.offer_version_id) if subscription.offer_version_id else None,
    )
    cycle_start = _billing_cycle_start(next_billing, cycle)
    total_cycle_seconds = max(
        int((next_billing - cycle_start).total_seconds()),
        1,
    )
    remaining_cycle_seconds = max(
        int((next_billing - now).total_seconds()),
        0,
    )

    if remaining_cycle_seconds <= 0:
        return {
            "effective_at": now,
            "credit_amount": Decimal("0.00"),
            "charge_amount": Decimal("0.00"),
            "net_amount": Decimal("0.00"),
            "days_remaining": 0,
            "days_in_cycle": max(total_cycle_seconds // 86400, 0),
            "remaining_cycle_seconds": 0,
            "total_cycle_seconds": total_cycle_seconds,
            "remaining_ratio": Decimal("0"),
        }

    # Old and new recurring prices, at the rate the customer actually pays.
    # The old side is the subscription's *effective* price (version-aware
    # catalog, then unit_price override, then any active discount) so the credit
    # matches what was billed — a negotiated/discounted/version-priced customer
    # was over-credited when this read the raw catalog OfferPrice. The new side
    # is the new offer's catalog price with the subscription's discount applied
    # but NOT the old per-offer override (which is re-snapshotted on the change).
    from app.services import billing_automation

    resolved_old, _, _ = billing_automation._resolve_price(db, subscription)
    old_catalog = (
        Decimal(str(resolved_old))
        if resolved_old is not None
        else _offer_recurring_price_amount(db, subscription.offer_id)
    )
    new_catalog = _offer_recurring_price_amount(db, new_offer_id)
    old_price = billing_automation._effective_unit_price(subscription, old_catalog, now)
    new_price = billing_automation._effective_unit_price(
        subscription, new_catalog, now, include_override=False
    )
    remaining_ratio = Decimal(str(remaining_cycle_seconds)) / Decimal(
        str(total_cycle_seconds)
    )
    credit_amount = round_money(old_price * remaining_ratio)
    charge_amount = round_money(new_price * remaining_ratio)
    net_amount = round_money(charge_amount - credit_amount)
    days_remaining = max(remaining_cycle_seconds // 86400, 0)
    days_in_cycle = max(total_cycle_seconds // 86400, 0)

    return {
        "effective_at": now,
        "credit_amount": credit_amount,
        "charge_amount": charge_amount,
        "net_amount": net_amount,
        "days_remaining": days_remaining,
        "days_in_cycle": days_in_cycle,
        "remaining_cycle_seconds": remaining_cycle_seconds,
        "total_cycle_seconds": total_cycle_seconds,
        "remaining_ratio": remaining_ratio,
        "old_price": round_money(old_price),
        "new_price": round_money(new_price),
    }


def _emit_offer_change_event(
    db: Session,
    subscription: Subscription,
    previous_offer_id: str,
) -> None:
    """Emit upgrade or downgrade event when the offer changes on an active subscription.

    Determines direction by comparing recurring price of old vs new offer.
    Also triggers RADIUS credential sync so speed changes take effect.
    """
    from app.models.catalog import OfferPrice, PriceType

    old_price_row = (
        db.query(OfferPrice.amount)
        .filter(
            OfferPrice.offer_id == previous_offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    new_price_row = (
        db.query(OfferPrice.amount)
        .filter(
            OfferPrice.offer_id == str(subscription.offer_id),
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    old_price = old_price_row[0] if old_price_row else 0
    new_price = new_price_row[0] if new_price_row else 0
    is_upgrade = new_price > old_price

    old_offer = db.get(CatalogOffer, previous_offer_id)
    new_offer = subscription.offer

    payload = {
        "subscription_id": str(subscription.id),
        "previous_offer_id": previous_offer_id,
        "previous_offer_name": old_offer.name if old_offer else None,
        "new_offer_id": str(subscription.offer_id),
        "new_offer_name": new_offer.name if new_offer else None,
        "direction": "upgrade" if is_upgrade else "downgrade",
    }

    event_type = (
        EventType.subscription_upgraded
        if is_upgrade
        else EventType.subscription_downgraded
    )
    emit_event(
        db,
        event_type,
        payload,
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
    )
    logger.info(
        "Subscription %s %s: %s -> %s",
        subscription.id,
        "upgraded" if is_upgrade else "downgraded",
        old_offer.name if old_offer else previous_offer_id,
        new_offer.name if new_offer else subscription.offer_id,
    )

    # Sync RADIUS credentials so the new speed profile takes effect
    _sync_credentials_to_radius(db, subscription.subscriber_id)
    try:
        from app.services.enforcement import update_subscription_sessions

        update_subscription_sessions(db, str(subscription.id), reason="plan_change")
    except Exception:
        logger.warning(
            "Failed to update sessions after plan change for %s",
            subscription.id,
        )


def _create_service_order_for_subscription(db: Session, subscription: Subscription):
    """Create a service order for a new subscription that needs provisioning."""
    from app.models.provisioning import ServiceOrderStatus
    from app.schemas.provisioning import ServiceOrderCreate
    from app.services import provisioning as provisioning_service

    # Account roles removed during consolidation; no contact linkage available here.
    requested_by_contact_id = None

    try:
        idempotency_key = f"subscription:{subscription.id}:new_install"
        payload = ServiceOrderCreate(
            account_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            requested_by_contact_id=requested_by_contact_id,
            status=ServiceOrderStatus.submitted,
            idempotency_key=idempotency_key,
            notes=f"Auto-created for subscription: {subscription.offer.name if subscription.offer else subscription.id}",
        )
        provisioning_service.service_orders.create(db, payload)
    except Exception:
        # Don't fail subscription creation if service order creation fails
        logger.warning(
            "Auto service-order creation failed for subscription %s",
            subscription.id,
            exc_info=True,
        )


class Subscriptions(ListResponseMixin):
    @staticmethod
    def list_nonterminal_for_nas_devices(
        db: Session,
        nas_device_ids: Collection[object],
    ) -> list[Subscription]:
        """Return services that still depend on the supplied NAS inventory."""
        if not nas_device_ids:
            return []
        from app.services.subscription_lifecycle_policy import (
            TERMINAL_SERVICE_STATUSES,
        )

        return list(
            db.scalars(
                select(Subscription)
                .where(Subscription.provisioning_nas_device_id.in_(nas_device_ids))
                .where(Subscription.status.not_in(TERMINAL_SERVICE_STATUSES))
                .order_by(Subscription.id)
            ).all()
        )

    @staticmethod
    def stage_provisioning_nas_assignment(
        db: Session,
        subscription_id: str,
        nas_device_id: str,
    ) -> Subscription:
        """Stage an authoritative subscription-to-NAS relink."""
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == coerce_uuid(subscription_id))
            .with_for_update()
        )
        if subscription is None:
            raise ValueError("Subscription not found")
        nas = db.scalar(
            select(NasDevice)
            .where(NasDevice.id == coerce_uuid(nas_device_id))
            .with_for_update()
        )
        if nas is None:
            raise ValueError("Target NAS not found")
        if not nas.is_active or nas.status != NasDeviceStatus.active:
            raise ValueError("Target NAS is not active")
        subscription.provisioning_nas_device_id = nas.id
        return subscription

    @staticmethod
    def create(
        db: Session,
        payload: SubscriptionCreate,
        *,
        create_service_order: bool = True,
    ):
        catalog_validators.validate_subscription_links(
            db,
            str(payload.subscriber_id),
            str(payload.offer_id),
            str(payload.offer_version_id) if payload.offer_version_id else None,
            str(payload.service_address_id) if payload.service_address_id else None,
        )
        catalog_validators.validate_offer_active(db, str(payload.offer_id))
        reference_at = payload.start_at or datetime.now(UTC)
        if payload.offer_version_id:
            catalog_validators.validate_offer_version_active(
                db,
                str(payload.offer_version_id),
                str(payload.offer_id),
                reference_at,
            )
        catalog_validators.validate_subscription_dates(
            payload.status,
            payload.start_at,
            payload.end_at,
            payload.next_billing_at,
            payload.canceled_at,
        )
        catalog_validators.enforce_single_active_subscription(
            db, str(payload.subscriber_id), payload.status
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_subscription_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, SubscriptionStatus, "status"
                )
        if "contract_term" not in fields_set:
            default_contract_term = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_contract_term"
            )
            if default_contract_term:
                data["contract_term"] = validate_enum(
                    default_contract_term, ContractTerm, "contract_term"
                )
        data["billing_mode"] = _subscription_billing_mode_for_write(
            db,
            subscriber_id=str(payload.subscriber_id),
            offer_id=str(payload.offer_id),
            requested_mode=(
                data.get("billing_mode") if "billing_mode" in fields_set else None
            ),
        )
        if (
            "start_at" not in fields_set
            and data.get("status") == SubscriptionStatus.active
        ):
            data["start_at"] = datetime.now(UTC)
        start_at = data.get("start_at")
        # Own the effective cadence on the subscription (SOT precedence:
        # contracted/sales cadence -> offer price -> monthly). Snapshot it so the
        # row is self-describing and the recurring biller reads the subscription,
        # not the offer, for owned subs.
        if data.get("offer_id"):
            offer_version_id = data.get("offer_version_id")
            cycle = _resolve_billing_cycle(
                db,
                str(data["offer_id"]),
                str(offer_version_id) if offer_version_id else None,
                override=data.get("billing_cycle"),
            )
            # Snapshot whenever no contracted cadence was given (unset, or passed
            # as None by a caller that always forwards the field): None means
            # "inherit the offer", which we materialize here so the row is
            # self-describing.
            if not data.get("billing_cycle"):
                data["billing_cycle"] = cycle
            if (
                "next_billing_at" not in fields_set
                and start_at
                and data.get("status") == SubscriptionStatus.active
            ):
                data["next_billing_at"] = _compute_next_billing_at(start_at, cycle)
        if "end_at" not in fields_set and start_at and data.get("contract_term"):
            end_at = _compute_contract_end_at(start_at, data["contract_term"])
            if end_at:
                data["end_at"] = end_at

        # Snapshot the offer's recurring price when no explicit unit_price is
        # given, so the subscription carries its effective monthly amount (the
        # invoice already resolves the offer price, but the customer/admin
        # billing summaries read subscription.unit_price and otherwise show
        # "Price not set" / a ₦0 monthly recurring total).
        if "unit_price" not in fields_set or data.get("unit_price") is None:
            data["unit_price"] = _offer_recurring_price_amount(
                db, str(payload.offer_id)
            )

        # Auto-select NAS device from subscriber's POP site if not provided
        if "provisioning_nas_device_id" not in fields_set or not data.get(
            "provisioning_nas_device_id"
        ):
            nas_device_id = _select_nas_for_subscriber(db, str(payload.subscriber_id))
            if nas_device_id:
                data["provisioning_nas_device_id"] = nas_device_id

        subscription = Subscription(**data)
        apply_offer_radius_profile(
            db,
            subscription,
            target_profile_id=data.get("radius_profile_id"),
            force="radius_profile_id" in fields_set
            or not bool(data.get("radius_profile_id")),
            sync_credentials=False,
        )
        db.add(subscription)
        activating = subscription.status == SubscriptionStatus.active
        try:
            from app.models.subscriber import Subscriber, SubscriberStatus
            from app.services.account_lifecycle import (
                clear_account_lifecycle_override,
                compute_account_status,
            )

            subscriber = db.get(Subscriber, str(payload.subscriber_id))
            if subscriber and subscriber.lifecycle_override_status is not None:
                if subscriber.lifecycle_override_status not in {
                    SubscriberStatus.active,
                    SubscriberStatus.new,
                }:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Account lifecycle restriction must be cleared before "
                            "creating a subscription"
                        ),
                    )
                clear_account_lifecycle_override(
                    db,
                    str(subscriber.id),
                    reason="Subscription created",
                    source="catalog:subscription_create",
                )
            if activating:
                db.flush()
                _auto_generate_pppoe(db, subscription)
            else:
                db.flush()
            compute_account_status(db, str(payload.subscriber_id))
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(subscription)

        # Auto-create Service Order for pending subscriptions that need provisioning
        if create_service_order and subscription.status == SubscriptionStatus.pending:
            _create_service_order_for_subscription(db, subscription)

        # Emit subscription.created event
        emit_event(
            db,
            EventType.subscription_created,
            {
                "subscription_id": str(subscription.id),
                "offer_id": str(subscription.offer_id),
                "offer_name": subscription.offer.name if subscription.offer else None,
                "status": subscription.status.value if subscription.status else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )

        # If created as active, credentials were minted in the create transaction.
        if activating:
            # CRITICAL: Sync credentials to RADIUS BEFORE emitting events
            # This ensures provisioning handlers can access RadiusUser records
            _sync_credentials_to_radius(db, subscription.subscriber_id)

            emit_event(
                db,
                EventType.subscription_activated,
                {
                    "subscription_id": str(subscription.id),
                    "offer_name": subscription.offer.name
                    if subscription.offer
                    else None,
                    "from_status": None,
                    "to_status": "active",
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )

        # SQLite drops tzinfo even when DateTime(timezone=True), and emit_event()
        # commits may expire the instance. Normalize to UTC right before returning
        # so callers/tests can do arithmetic with aware datetimes.
        def _ensure_utc(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

        subscription.start_at = _ensure_utc(subscription.start_at)
        subscription.end_at = _ensure_utc(subscription.end_at)
        subscription.next_billing_at = _ensure_utc(subscription.next_billing_at)
        subscription.canceled_at = _ensure_utc(subscription.canceled_at)

        return subscription

    @staticmethod
    def get(db: Session, subscription_id: str):
        subscription = db.get(
            Subscription,
            subscription_id,
            options=[
                selectinload(Subscription.offer),
                selectinload(Subscription.add_ons).selectinload(
                    SubscriptionAddOn.add_on
                ),
            ],
        )
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return subscription

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        offer_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Subscription).options(
            selectinload(Subscription.offer),
            selectinload(Subscription.subscriber),
            selectinload(Subscription.add_ons).selectinload(SubscriptionAddOn.add_on),
        )
        query = apply_optional_equals(
            query,
            {
                Subscription.subscriber_id: subscriber_id,
                Subscription.offer_id: offer_id,
            },
        )
        if status:
            query = query.filter(
                Subscription.status
                == validate_enum(status, SubscriptionStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Subscription.created_at,
                "status": Subscription.status,
                "start_at": Subscription.start_at,
                "next_billing_at": Subscription.next_billing_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session,
        subscription_id: str,
        payload: SubscriptionUpdate,
        *,
        skip_proration_artifacts: bool = False,
        plan_change_operation_key: str | None = None,
        plan_change_preview_fingerprint: str | None = None,
        plan_change_effective_at: datetime | None = None,
        plan_change_request_id: str | None = None,
        plan_change_actor_id: str | None = None,
    ):
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        data = payload.model_dump(exclude_unset=True)
        lifecycle_fields = {
            "status",
            "start_at",
            "end_at",
            "next_billing_at",
            "canceled_at",
            "cancel_reason",
        }
        requested_lifecycle_fields = lifecycle_fields.intersection(data)
        if requested_lifecycle_fields:
            fields = ", ".join(sorted(requested_lifecycle_fields))
            raise HTTPException(
                status_code=409,
                detail=(
                    "Subscription lifecycle fields are read-only in generic "
                    f"updates ({fields}); use the subscription lifecycle command."
                ),
            )
        requested_offer_id = data.get("offer_id")
        if (
            requested_offer_id is not None
            and str(requested_offer_id) != str(subscription.offer_id)
            and subscription.status == SubscriptionStatus.active
            and subscription.billing_mode == BillingMode.prepaid
            and not skip_proration_artifacts
        ):
            # Acquire the account lock before capturing old state. A concurrent
            # wallet debit or plan change may have committed while this request
            # was waiting, so refresh before deriving the quote and mutation.
            from app.services.billing._common import lock_account

            lock_account(db, str(subscription.subscriber_id))
            db.refresh(subscription)

        # Track state after any lock wait for event emission and idempotency.
        previous_status = subscription.status
        previous_offer_id = subscription.offer_id
        previous_profile_id = subscription.radius_profile_id
        subscriber_id = str(data.get("subscriber_id", subscription.subscriber_id))
        offer_id = str(data.get("offer_id", subscription.offer_id))
        offer_version_id = data.get("offer_version_id", subscription.offer_version_id)
        # A plan change to a different offer must not carry the previous offer's
        # pinned version: a stale offer_version_id makes price resolution read the
        # OLD offer's OfferVersionPrice (wrong-plan price, silently). When the
        # caller changes offer_id without explicitly choosing a version, drop the
        # pin so pricing falls back to the new offer's price.
        if (
            "offer_id" in data
            and str(data["offer_id"]) != str(subscription.offer_id)
            and "offer_version_id" not in data
            and offer_version_id is not None
        ):
            offer_version_id = None
            data["offer_version_id"] = None
        service_address_id = data.get(
            "service_address_id", subscription.service_address_id
        )
        catalog_validators.validate_subscription_links(
            db,
            subscriber_id,
            offer_id,
            str(offer_version_id) if offer_version_id else None,
            str(service_address_id) if service_address_id else None,
        )
        catalog_validators.validate_offer_active(db, offer_id)

        if {"subscriber_id", "offer_id", "billing_mode"}.intersection(data):
            data["billing_mode"] = _subscription_billing_mode_for_write(
                db,
                subscriber_id=subscriber_id,
                offer_id=offer_id,
                requested_mode=data.get("billing_mode"),
            )

        # Plan change validation and proration
        offer_changing = (
            "offer_id" in data
            and str(data["offer_id"]) != str(subscription.offer_id)
            and subscription.status == SubscriptionStatus.active
        )
        if offer_changing:
            _validate_plan_change(db, subscription, str(data["offer_id"]))
            is_prepaid = subscription.billing_mode == BillingMode.prepaid
            if is_prepaid and not skip_proration_artifacts:
                from app.services.prepaid_plan_changes import (
                    PrepaidPlanChangePreviewRequired,
                    PrepaidPlanChangePreviewStale,
                    PrepaidPlanChangeRejected,
                    prepare_immediate_prepaid_plan_change,
                )

                target_offer = db.get(CatalogOffer, str(data["offer_id"]))
                if target_offer is None:
                    raise HTTPException(status_code=404, detail="Offer not found")
                old_offer = db.get(CatalogOffer, previous_offer_id)
                try:
                    prepared = prepare_immediate_prepaid_plan_change(
                        db,
                        subscription,
                        target_offer,
                        old_offer_name=(
                            old_offer.name if old_offer else "Previous Plan"
                        ),
                        operation_key=plan_change_operation_key,
                        effective_at=plan_change_effective_at,
                        expected_preview_fingerprint=(plan_change_preview_fingerprint),
                    )
                except (
                    PrepaidPlanChangePreviewRequired,
                    PrepaidPlanChangePreviewStale,
                ) as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                except PrepaidPlanChangeRejected as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=exc.decision.rejection_detail(),
                    ) from exc
                logger.info(
                    "Prepared prepaid plan change subscription=%s debit=%s credit=%s replayed=%s",
                    subscription.id,
                    prepared.ledger_entry is not None,
                    prepared.credit_note is not None,
                    prepared.replayed,
                )
                if plan_change_request_id:
                    from app.models.audit import AuditActorType
                    from app.models.subscription_change import (
                        SubscriptionChangeRequest,
                    )
                    from app.services.audit_adapter import stage_audit_event

                    change_request = db.get(
                        SubscriptionChangeRequest,
                        coerce_uuid(plan_change_request_id),
                    )
                    if change_request is None:
                        raise HTTPException(
                            status_code=409,
                            detail="Plan-change confirmation request was not found",
                        )
                    if (
                        str(change_request.subscription_id) != str(subscription.id)
                        or str(change_request.requested_offer_id)
                        != str(target_offer.id)
                        or change_request.confirmation_preview_fingerprint
                        != prepared.decision.fingerprint
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="Plan-change confirmation does not match this change",
                        )
                    change_request.confirmation_snapshot = (
                        prepared.decision.as_evidence_dict()
                    )
                    change_request.confirmed_at = datetime.now(UTC)
                    change_request.account_adjustment_id = (
                        prepared.account_adjustment.id
                        if prepared.account_adjustment
                        else None
                    )
                    change_request.credit_note_id = (
                        prepared.credit_note.id if prepared.credit_note else None
                    )
                    change_request.ledger_entry_id = (
                        prepared.ledger_entry.id if prepared.ledger_entry else None
                    )
                    stage_audit_event(
                        db,
                        action="confirm_immediate_plan_change",
                        entity_type="subscription_change_request",
                        entity_id=str(change_request.id),
                        actor_type=(
                            AuditActorType.user
                            if plan_change_actor_id
                            else AuditActorType.system
                        ),
                        actor_id=plan_change_actor_id,
                        metadata={
                            **prepared.decision.as_evidence_dict(),
                            "account_adjustment_id": (
                                str(prepared.account_adjustment.id)
                                if prepared.account_adjustment
                                else None
                            ),
                            "credit_note_id": (
                                str(prepared.credit_note.id)
                                if prepared.credit_note
                                else None
                            ),
                            "ledger_entry_id": (
                                str(prepared.ledger_entry.id)
                                if prepared.ledger_entry
                                else None
                            ),
                            "replayed": prepared.replayed,
                        },
                    )

        # When the offer changes, refresh the snapshotted recurring price to the
        # new offer (unless an explicit unit_price was supplied) so the customer
        # and admin billing summaries reflect the new plan instead of the old
        # price. Mirrors the create-path default; covers change-plan apply and
        # admin plan edits for any status.
        if (
            "offer_id" in data
            and str(data["offer_id"]) != str(previous_offer_id)
            and "unit_price" not in data
        ):
            data["unit_price"] = _offer_recurring_price_amount(
                db, str(data["offer_id"])
            )

        status = subscription.status
        start_at = _ensure_utc(data.get("start_at", subscription.start_at))
        end_at = _ensure_utc(data.get("end_at", subscription.end_at))
        next_billing_at = _ensure_utc(
            data.get("next_billing_at", subscription.next_billing_at)
        )
        canceled_at = _ensure_utc(data.get("canceled_at", subscription.canceled_at))
        reference_at = start_at or datetime.now(UTC)
        if offer_version_id:
            catalog_validators.validate_offer_version_active(
                db,
                str(offer_version_id),
                offer_id,
                reference_at,
            )
        catalog_validators.validate_subscription_dates(
            status,
            start_at,
            end_at,
            next_billing_at,
            canceled_at,
        )
        catalog_validators.enforce_single_active_subscription(
            db, subscriber_id, status, subscription_id
        )
        if (
            status == SubscriptionStatus.active
            and not start_at
            and {"offer_id", "billing_cycle"}.intersection(data)
        ):
            start_at = datetime.now(UTC)
            data["start_at"] = start_at
        elif "offer_id" in data and not start_at:
            # Preserve historical behavior where plan changes establish
            # subscription start time if it was previously unset.
            start_at = datetime.now(UTC)
            data["start_at"] = start_at
        # Service-change cadence transition (SOT): an explicit billing_cycle in
        # the update becomes the new owned cadence; otherwise keep the sub's own.
        cadence_override = data.get("billing_cycle", subscription.billing_cycle)
        cadence_changing = (
            "billing_cycle" in data
            and data["billing_cycle"] != subscription.billing_cycle
        )
        if status == SubscriptionStatus.active and start_at:
            cycle = _resolve_billing_cycle(
                db,
                offer_id,
                str(offer_version_id) if offer_version_id else None,
                override=cadence_override,
            )
            existing_next = _ensure_utc(data.get("next_billing_at") or next_billing_at)
            now = datetime.now(UTC)
            # Recompute next_billing_at when:
            # 1. Not provided in form data, OR
            # 2. Resuming from suspension, OR
            # 3. The existing value is more than 60 days in the past (stale migration data)
            stale = existing_next and (now - existing_next).days > 60
            resuming = previous_status == SubscriptionStatus.suspended
            # A pure cadence change re-anchors on the NEXT cycle: keep the current
            # next_billing_at so the in-flight period bills as scheduled; the new
            # cadence applies when the biller next advances the anchor.
            if (
                (offer_changing or cadence_changing)
                and previous_status == SubscriptionStatus.active
                and existing_next
                and "next_billing_at" not in data
                and not stale
            ):
                data["next_billing_at"] = existing_next
            elif "next_billing_at" not in data or resuming or stale:
                billing_anchor = now if (resuming or stale) else start_at
                data["next_billing_at"] = _compute_next_billing_at(
                    billing_anchor, cycle
                )
        if start_at and "end_at" not in data:
            term = data.get("contract_term", subscription.contract_term)
            term_changing = (
                "contract_term" in data
                and data["contract_term"] != subscription.contract_term
            )
            # Only (re)derive end_at from the contract term when it isn't set yet
            # or the term itself is explicitly changing. Re-deriving on every
            # update (e.g. a plan change that carries no end_at) silently
            # overwrote a manually set/extended end_at and could resurrect a past
            # contract-end as a customer-visible expiry.
            if subscription.end_at is None or term_changing:
                new_end_at = _compute_contract_end_at(start_at, term)
                if new_end_at:
                    data["end_at"] = new_end_at

        # Auto-select NAS device when activating if not already set
        if (
            status == SubscriptionStatus.active
            and not subscription.provisioning_nas_device_id
            and "provisioning_nas_device_id" not in data
        ):
            nas_device_id = _select_nas_for_subscriber(db, subscriber_id)
            if nas_device_id:
                data["provisioning_nas_device_id"] = nas_device_id

        for key, value in data.items():
            setattr(subscription, key, value)
        if "radius_profile_id" in data:
            apply_offer_radius_profile(
                db,
                subscription,
                previous_offer_id=previous_offer_id,
                target_profile_id=data["radius_profile_id"],
                force=True,
            )
        elif "offer_id" in data or subscription.radius_profile_id is None:
            apply_offer_radius_profile(
                db,
                subscription,
                previous_offer_id=previous_offer_id,
            )
        db.commit()
        db.refresh(subscription)

        if (
            previous_status == SubscriptionStatus.active
            and previous_profile_id != subscription.radius_profile_id
        ):
            _sync_credentials_to_radius(db, subscription.subscriber_id)
            try:
                from app.services.enforcement import update_subscription_sessions

                update_subscription_sessions(
                    db, str(subscription.id), reason="profile_change"
                )
            except Exception:
                logger.warning(
                    "Failed to refresh active sessions for subscription %s after profile change",
                    subscription.id,
                    exc_info=True,
                )

        # Emit upgrade/downgrade events when offer changes on an active subscription
        if (
            previous_offer_id
            and str(previous_offer_id) != str(subscription.offer_id)
            and subscription.status == SubscriptionStatus.active
        ):
            _emit_offer_change_event(db, subscription, str(previous_offer_id))

        return subscription

    @staticmethod
    def delete(db: Session, subscription_id: str):
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        subscriber_id = subscription.subscriber_id
        offer_id = subscription.offer_id
        db.delete(subscription)
        db.commit()
        emit_event(
            db,
            EventType.subscription_deleted,
            {
                "subscription_id": str(subscription_id),
                "subscriber_id": str(subscriber_id) if subscriber_id else None,
                "offer_id": str(offer_id) if offer_id else None,
            },
        )

    @staticmethod
    def expire_subscriptions(
        db: Session,
        run_at: datetime | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Expire subscriptions that have passed their end_at date.

        This should be run periodically (e.g., daily) to ensure subscriptions
        that have reached their contract end date are properly transitioned
        to expired status.

        Args:
            db: Database session
            run_at: Reference time (defaults to now)
            dry_run: If True, don't actually make changes

        Returns:
            Summary dict with counts
        """
        run_at = run_at or datetime.now(UTC)

        # Find subscriptions that should be expired
        subscriptions_to_expire = (
            db.query(Subscription)
            .filter(Subscription.end_at.is_not(None))
            .filter(Subscription.end_at <= run_at)
            .filter(
                Subscription.status.in_(
                    [SubscriptionStatus.active, SubscriptionStatus.suspended]
                )
            )
            .all()
        )

        expired_count = 0
        skipped_count = 0
        for subscription in subscriptions_to_expire:
            if not dry_run:
                try:
                    from app.services.account_lifecycle import expire_subscription

                    expire_subscription(db, str(subscription.id))
                except ValueError as e:
                    logger.info(
                        "Skipped expiring subscription %s: %s",
                        subscription.id,
                        e,
                    )
                    skipped_count += 1
                    continue

            expired_count += 1

        if not dry_run:
            db.commit()

        return {
            "run_at": run_at,
            "subscriptions_matched": len(subscriptions_to_expire),
            "subscriptions_expired": expired_count,
            "subscriptions_skipped": skipped_count,
            "dry_run": dry_run,
        }


class SubscriptionAddOns(CRUDManager[SubscriptionAddOn]):
    model = SubscriptionAddOn
    not_found_detail = "Subscription add-on not found"

    @staticmethod
    def create(db: Session, payload: SubscriptionAddOnCreate):
        catalog_validators.validate_subscription_add_on(
            db, str(payload.subscription_id), str(payload.add_on_id), payload.quantity
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "quantity" not in fields_set:
            default_quantity = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_addon_quantity"
            )
            if default_quantity:
                data["quantity"] = default_quantity
        subscription_add_on = SubscriptionAddOn(**data)
        db.add(subscription_add_on)
        db.commit()
        db.refresh(subscription_add_on)
        return subscription_add_on

    @classmethod
    def get(cls, db: Session, subscription_add_on_id: str):
        return super().get(db, subscription_add_on_id)

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        add_on_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriptionAddOn)
        query = apply_optional_equals(
            query,
            {
                SubscriptionAddOn.subscription_id: subscription_id,
                SubscriptionAddOn.add_on_id: add_on_id,
            },
        )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "start_at": SubscriptionAddOn.start_at,
                "end_at": SubscriptionAddOn.end_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session, subscription_add_on_id: str, payload: SubscriptionAddOnUpdate
    ):
        subscription_add_on = db.get(SubscriptionAddOn, subscription_add_on_id)
        if not subscription_add_on:
            raise HTTPException(status_code=404, detail="Subscription add-on not found")
        data = payload.model_dump(exclude_unset=True)
        subscription_id = data.get(
            "subscription_id", subscription_add_on.subscription_id
        )
        add_on_id = data.get("add_on_id", subscription_add_on.add_on_id)
        quantity = data.get("quantity", subscription_add_on.quantity)
        catalog_validators.validate_subscription_add_on(
            db, str(subscription_id), str(add_on_id), quantity
        )
        for key, value in data.items():
            setattr(subscription_add_on, key, value)
        db.commit()
        db.refresh(subscription_add_on)
        return subscription_add_on

    @classmethod
    def delete(cls, db: Session, subscription_add_on_id: str):
        return super().delete(db, subscription_add_on_id)
