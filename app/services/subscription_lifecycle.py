"""Canonical read model and command contract for subscription lifecycle changes.

This boundary does not own billing ledger writes, RADIUS projection, or catalog
mutation. It composes those owners into one preview that every UI, API, bulk
job, and scheduler can consume before calling the existing command executors.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.catalog import (
    CatalogOffer,
    OfferPrice,
    OfferStatus,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Address, Subscriber
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services.access_resolution import resolve_customer_access
from app.services.common import coerce_uuid, round_money
from app.services.subscription_lifecycle_policy import (
    BILLING_COLLECTIBLE_SERVICE_STATUSES,
    MRR_COUNTABLE_SERVICE_STATUSES,
    TERMINAL_SERVICE_STATUSES,
)

_SUSPENDED_EQUIVALENT_STATUSES = frozenset(
    {
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.stopped,
    }
)
RESTORE_CANDIDATE_STATUSES = _SUSPENDED_EQUIVALENT_STATUSES | frozenset(
    {SubscriptionStatus.disabled}
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class SubscriptionLifecycleError(ValueError):
    pass


class SubscriptionLifecycleHeadConflict(SubscriptionLifecycleError):
    pass


class SubscriptionCommandKind(str, enum.Enum):
    activate = "activate"
    suspend = "suspend"
    disable = "disable"
    restore = "restore"
    renew = "renew"
    cancel = "cancel"
    expire = "expire"
    change_plan = "change_plan"
    vacation_hold = "vacation_hold"
    vacation_resume = "vacation_resume"


class SubscriptionEffectiveTiming(str, enum.Enum):
    immediate = "immediate"
    next_cycle = "next_cycle"
    scheduled = "scheduled"


class SubscriptionCommandOutcomeStatus(str, enum.Enum):
    applied = "applied"
    scheduled = "scheduled"
    skipped = "skipped"
    rejected = "rejected"
    failed = "failed"
    superseded = "superseded"


class SubscriptionSessionAction(str, enum.Enum):
    none = "none"
    authorize = "authorize"
    disconnect = "disconnect"
    reauthorize = "reauthorize"
    deprovision = "deprovision"


def is_subscription_restore_candidate(status: SubscriptionStatus | None) -> bool:
    """Return owner-defined coarse eligibility for offering a restore preview."""
    return status in RESTORE_CANDIDATE_STATUSES


class ServiceChangeDeliveryMode(str, enum.Enum):
    """Operational delivery required by a catalog service change."""

    commercial_only = "commercial_only"
    remote_reprovision = "remote_reprovision"
    field_migration = "field_migration"


def classify_service_change_delivery(
    current_offer: CatalogOffer,
    target_offer: CatalogOffer,
    *,
    service_address_changed: bool = False,
) -> ServiceChangeDeliveryMode:
    """Classify delivery from provisionable catalog facts, never plan family.

    A commercial family is merchandising policy, not evidence of a physical
    access-network change. Changing access medium requires field fulfillment;
    changing a profile or speed on the same medium is remotely provisionable.
    """
    if service_address_changed or current_offer.access_type != target_offer.access_type:
        return ServiceChangeDeliveryMode.field_migration
    current_network_intent = (
        current_offer.default_ont_profile_id,
        current_offer.policy_set_id,
        current_offer.usage_allowance_id,
        current_offer.speed_download_mbps,
        current_offer.speed_upload_mbps,
        current_offer.guaranteed_speed_limit_at,
        current_offer.guaranteed_speed,
        current_offer.aggregation,
        current_offer.priority,
        current_offer.burst_profile,
    )
    target_network_intent = (
        target_offer.default_ont_profile_id,
        target_offer.policy_set_id,
        target_offer.usage_allowance_id,
        target_offer.speed_download_mbps,
        target_offer.speed_upload_mbps,
        target_offer.guaranteed_speed_limit_at,
        target_offer.guaranteed_speed,
        target_offer.aggregation,
        target_offer.priority,
        target_offer.burst_profile,
    )
    if current_network_intent != target_network_intent:
        return ServiceChangeDeliveryMode.remote_reprovision
    return ServiceChangeDeliveryMode.commercial_only


@dataclass(frozen=True)
class SubscriptionLifecycleCommand:
    subscription_id: str
    kind: SubscriptionCommandKind
    source: str
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    )
    effective_at: datetime | None = None
    target_offer_id: str | None = None
    target_service_address_id: str | None = None
    reason: str | None = None
    expected_head: str | None = None
    expected_financial_fingerprint: str | None = None
    expected_field_quote_fingerprint: str | None = None
    idempotency_key: str | None = None
    vacation_hold_days: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SubscriptionCommandKind):
            object.__setattr__(self, "kind", SubscriptionCommandKind(self.kind))
        if not isinstance(self.effective_timing, SubscriptionEffectiveTiming):
            object.__setattr__(
                self,
                "effective_timing",
                SubscriptionEffectiveTiming(self.effective_timing),
            )
        for name in ("subscription_id", "source"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise SubscriptionLifecycleError(f"{name} is required")
            object.__setattr__(self, name, value)
        if self.kind == SubscriptionCommandKind.change_plan:
            target_offer_id = str(self.target_offer_id or "").strip()
            if not target_offer_id:
                raise SubscriptionLifecycleError(
                    "target_offer_id is required for change_plan"
                )
            object.__setattr__(self, "target_offer_id", target_offer_id)
        elif self.target_offer_id is not None:
            raise SubscriptionLifecycleError(
                "target_offer_id is only valid for change_plan"
            )
        if self.kind == SubscriptionCommandKind.vacation_hold:
            if self.vacation_hold_days is None or self.vacation_hold_days < 1:
                raise SubscriptionLifecycleError(
                    "vacation_hold_days must be positive for vacation_hold"
                )
        elif self.vacation_hold_days is not None:
            raise SubscriptionLifecycleError(
                "vacation_hold_days is only valid for vacation_hold"
            )
        if self.target_service_address_id is not None:
            if self.kind != SubscriptionCommandKind.change_plan:
                raise SubscriptionLifecycleError(
                    "target_service_address_id is only valid for change_plan"
                )
            target_service_address_id = str(self.target_service_address_id).strip()
            object.__setattr__(
                self,
                "target_service_address_id",
                target_service_address_id or None,
            )
        if (
            self.effective_timing == SubscriptionEffectiveTiming.scheduled
            and self.effective_at is None
        ):
            raise SubscriptionLifecycleError(
                "effective_at is required for scheduled commands"
            )
        if (
            self.effective_timing == SubscriptionEffectiveTiming.next_cycle
            and self.effective_at is not None
        ):
            raise SubscriptionLifecycleError(
                "effective_at is not valid for next_cycle commands; use scheduled"
            )
        if self.reason is not None:
            object.__setattr__(self, "reason", self.reason.strip() or None)
        if self.expected_head is not None:
            object.__setattr__(
                self, "expected_head", self.expected_head.strip() or None
            )
        if self.expected_financial_fingerprint is not None:
            object.__setattr__(
                self,
                "expected_financial_fingerprint",
                self.expected_financial_fingerprint.strip() or None,
            )
        if self.expected_field_quote_fingerprint is not None:
            object.__setattr__(
                self,
                "expected_field_quote_fingerprint",
                self.expected_field_quote_fingerprint.strip() or None,
            )
        if self.idempotency_key is not None:
            object.__setattr__(
                self, "idempotency_key", self.idempotency_key.strip() or None
            )


@dataclass(frozen=True)
class SubscriptionLifecycleState:
    status: str
    offer_id: str
    offer_name: str | None
    billing_mode: str
    billing_collectible: bool
    mrr_countable: bool
    radius_access_state: str | None
    radius_allowed: bool
    radius_blocked: bool
    access_block_reason: str | None
    terminal: bool


@dataclass(frozen=True)
class PendingSubscriptionChange:
    request_id: str
    target_offer_id: str
    target_offer_name: str | None
    effective_at: datetime


@dataclass(frozen=True)
class SubscriptionLifecycleSnapshot:
    subscription_id: str
    account_id: str
    account_status: str | None
    account_enabled: bool
    head: str
    state: SubscriptionLifecycleState
    start_at: datetime | None
    end_at: datetime | None
    next_billing_at: datetime | None
    canceled_at: datetime | None
    pending_change: PendingSubscriptionChange | None


@dataclass(frozen=True)
class SubscriptionBillingImpact:
    action: str
    collectible_before: bool
    collectible_after: bool
    currency: str | None = None
    charge_amount: Decimal = Decimal("0.00")
    credit_amount: Decimal = Decimal("0.00")
    net_amount: Decimal = Decimal("0.00")
    available_balance: Decimal | None = None
    required_amount: Decimal | None = None
    shortfall: Decimal | None = None
    collection_blocking_balance: Decimal | None = None
    possible_prorated_credit: bool = False
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class SubscriptionAccessImpact:
    current_state: str | None
    proposed_state: str | None
    allowed_before: bool
    allowed_after: bool
    session_action: SubscriptionSessionAction
    block_reason_after: str | None


@dataclass(frozen=True)
class FieldDeliveryQuote:
    """Qualification and one-time charge for a physical service relocation."""

    target_service_address_id: str
    target_address_label: str
    access_type: str
    qualification_status: str
    qualification_reasons: tuple[str, ...]
    qualification_coverage_area_id: str | None
    fee_offer_id: str | None
    fee_offer_name: str | None
    fee_amount: Decimal
    currency: str
    fingerprint: str
    eligible: bool
    blocking_reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "target_service_address_id": self.target_service_address_id,
            "target_address_label": self.target_address_label,
            "access_type": self.access_type,
            "qualification_status": self.qualification_status,
            "qualification_reasons": list(self.qualification_reasons),
            "qualification_coverage_area_id": self.qualification_coverage_area_id,
            "fee_offer_id": self.fee_offer_id,
            "fee_offer_name": self.fee_offer_name,
            "fee_amount": self.fee_amount,
            "currency": self.currency,
            "preview_fingerprint": self.fingerprint,
            "eligible": self.eligible,
            "blocking_reason": self.blocking_reason,
            "payment_required_before_fulfillment": self.fee_amount > Decimal("0.00"),
        }


@dataclass(frozen=True)
class VacationHoldPolicyDecision:
    eligible: bool
    reasons: tuple[str, ...]
    max_days: int
    max_holds_per_year: int | None
    cooldown_days: int | None
    holds_this_year: int
    last_hold_at: datetime | None
    days_since_last: int | None
    active_lock_id: str | None
    active_lock_resume_at: datetime | None


def resolve_vacation_hold_policy(
    db: Session,
    subscription: Subscription,
    *,
    command_kind: SubscriptionCommandKind,
    requested_days: int | None = None,
    now: datetime | None = None,
) -> VacationHoldPolicyDecision:
    """Resolve vacation-hold eligibility from settings and lock evidence."""

    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import resolve_value

    effective_now = _aware_utc(now) or datetime.now(UTC)

    def _setting_int(key: str, default: int) -> int:
        value = resolve_value(db, SettingDomain.catalog, key)
        return int(value) if isinstance(value, (str, int, float)) else default

    max_days = _setting_int("max_suspend_days", 30)
    max_holds = _setting_int("max_suspend_holds_per_year", 0)
    cooldown_days = _setting_int("suspend_cooldown_days", 0)
    year_start = datetime(effective_now.year, 1, 1, tzinfo=UTC)
    holds = list(
        db.scalars(
            select(EnforcementLock)
            .where(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.reason == EnforcementReason.customer_hold,
            )
            .order_by(EnforcementLock.created_at.desc())
        ).all()
    )
    holds_this_year = sum(
        1
        for lock in holds
        if (_aware_utc(lock.created_at) or effective_now) >= year_start
    )
    last_hold_at = _aware_utc(holds[0].created_at) if holds else None
    days_since_last = (
        max(0, (effective_now - last_hold_at).days)
        if last_hold_at is not None
        else None
    )
    active = next((lock for lock in holds if lock.is_active), None)
    reasons: list[str] = []
    if command_kind == SubscriptionCommandKind.vacation_hold:
        if subscription.status != SubscriptionStatus.active:
            reasons.append("vacation_hold_requires_active_subscription")
        if requested_days is None or requested_days < 1 or requested_days > max_days:
            reasons.append("vacation_hold_duration_out_of_range")
        if max_holds > 0 and holds_this_year >= max_holds:
            reasons.append("vacation_hold_annual_limit_reached")
        if (
            cooldown_days > 0
            and days_since_last is not None
            and days_since_last < cooldown_days
        ):
            reasons.append("vacation_hold_cooldown_active")
    elif command_kind == SubscriptionCommandKind.vacation_resume:
        if subscription.status != SubscriptionStatus.suspended:
            reasons.append("vacation_resume_requires_suspended_subscription")
        if active is None:
            reasons.append("active_customer_hold_missing")
    else:
        reasons.append("unsupported_vacation_command")
    return VacationHoldPolicyDecision(
        eligible=not reasons,
        reasons=tuple(reasons),
        max_days=max_days,
        max_holds_per_year=max_holds if max_holds > 0 else None,
        cooldown_days=cooldown_days if cooldown_days > 0 else None,
        holds_this_year=holds_this_year,
        last_hold_at=last_hold_at,
        days_since_last=days_since_last,
        active_lock_id=str(active.id) if active is not None else None,
        active_lock_resume_at=_aware_utc(active.resume_at)
        if active is not None
        else None,
    )


@dataclass(frozen=True)
class SubscriptionLifecyclePreview:
    command: SubscriptionLifecycleCommand
    current: SubscriptionLifecycleSnapshot
    proposed: SubscriptionLifecycleState
    effective_at: datetime
    eligible: bool
    eligibility_reasons: tuple[str, ...]
    billing_impact: SubscriptionBillingImpact
    access_impact: SubscriptionAccessImpact
    delivery_mode: ServiceChangeDeliveryMode | None = None
    field_delivery_quote: FieldDeliveryQuote | None = None
    requires_confirmation: bool = True


@dataclass(frozen=True)
class SubscriptionCommandOutcome:
    command: SubscriptionLifecycleCommand
    status: SubscriptionCommandOutcomeStatus
    message: str
    previous_head: str
    current_head: str | None = None
    artifact_ids: tuple[str, ...] = ()
    error_code: str | None = None
    replayed: bool = False


def assert_subscription_transition(
    from_status: SubscriptionStatus | None,
    to_status: SubscriptionStatus | None,
) -> None:
    """Preserve terminal subscription states as immutable sinks."""
    if to_status is None or from_status == to_status:
        return
    if from_status in TERMINAL_SERVICE_STATUSES:
        raise SubscriptionLifecycleError(
            f"Illegal subscription transition {from_status.value} -> "
            f"{to_status.value}: {from_status.value} is terminal and cannot be "
            "reactivated"
        )


def assert_subscription_head(
    snapshot: SubscriptionLifecycleSnapshot,
    expected_head: str | None,
) -> None:
    if expected_head is not None and expected_head != snapshot.head:
        raise SubscriptionLifecycleHeadConflict(
            "Subscription changed after this action was reviewed; refresh and "
            "review the current state before applying it"
        )


def resolve_subscription_lifecycle(
    db: Session,
    subscription_id: str,
) -> SubscriptionLifecycleSnapshot:
    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == coerce_uuid(subscription_id))
        .options(
            selectinload(Subscription.subscriber),
            selectinload(Subscription.offer),
        )
    )
    if subscription is None:
        raise SubscriptionLifecycleError("Subscription not found")
    pending_change = db.scalar(
        select(SubscriptionChangeRequest)
        .where(SubscriptionChangeRequest.subscription_id == subscription.id)
        .where(
            SubscriptionChangeRequest.status.in_(
                {
                    SubscriptionChangeStatus.pending,
                    SubscriptionChangeStatus.approved,
                }
            )
        )
        .where(SubscriptionChangeRequest.applied_at.is_(None))
        .where(SubscriptionChangeRequest.is_active.is_(True))
        .order_by(SubscriptionChangeRequest.effective_date.asc())
        .options(selectinload(SubscriptionChangeRequest.requested_offer))
    )
    subscriber = subscription.subscriber
    return SubscriptionLifecycleSnapshot(
        subscription_id=str(subscription.id),
        account_id=str(subscription.subscriber_id),
        account_status=_enum_value(getattr(subscriber, "status", None)),
        account_enabled=bool(subscriber and subscriber.is_active),
        head=_subscription_head(
            db,
            subscription,
            subscriber=subscriber,
            pending_change=pending_change,
        ),
        state=_resolve_state(subscription, subscriber=subscriber),
        start_at=_aware_utc(subscription.start_at),
        end_at=_aware_utc(subscription.end_at),
        next_billing_at=_aware_utc(subscription.next_billing_at),
        canceled_at=_aware_utc(subscription.canceled_at),
        pending_change=(
            PendingSubscriptionChange(
                request_id=str(pending_change.id),
                target_offer_id=str(pending_change.requested_offer_id),
                target_offer_name=(
                    pending_change.requested_offer.name
                    if pending_change.requested_offer
                    else None
                ),
                effective_at=datetime.combine(
                    pending_change.effective_date,
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
            )
            if pending_change is not None
            else None
        ),
    )


def preview_subscription_command(
    db: Session,
    command: SubscriptionLifecycleCommand,
    *,
    now: datetime | None = None,
    current_balance: Decimal | None = None,
) -> SubscriptionLifecyclePreview:
    effective_now = _aware_utc(now) or datetime.now(UTC)
    current = resolve_subscription_lifecycle(db, command.subscription_id)
    assert_subscription_head(current, command.expected_head)
    subscription = db.get(Subscription, coerce_uuid(command.subscription_id))
    if subscription is None:  # pragma: no cover - resolver already proves existence
        raise SubscriptionLifecycleError("Subscription not found")
    target_offer = _target_offer(db, command)
    target_address = _target_service_address(db, subscription, command)
    address_changed = bool(
        target_address is not None
        and str(target_address.id) != str(subscription.service_address_id or "")
    )
    field_delivery_quote = (
        _resolve_field_delivery_quote(db, subscription, target_offer, target_address)
        if target_offer is not None and target_address is not None and address_changed
        else None
    )
    reasons = _eligibility_reasons(
        db,
        subscription,
        command,
        target_offer=target_offer,
        address_changed=address_changed,
        field_delivery_quote=field_delivery_quote,
        now=effective_now,
    )
    proposed_status = _proposed_status(subscription.status, command.kind)
    proposed_offer = target_offer or subscription.offer
    proposed = _resolve_state(
        subscription,
        subscriber=subscription.subscriber,
        status=proposed_status,
        offer=proposed_offer,
    )
    effective_at = _effective_at(current, command, now=effective_now)
    billing_impact, financial_reason = _billing_impact(
        db,
        subscription,
        command,
        current=current,
        proposed=proposed,
        target_offer=target_offer,
        effective_at=effective_at,
        current_balance=current_balance,
    )
    if financial_reason:
        reasons.append(financial_reason)
    access_impact = SubscriptionAccessImpact(
        current_state=current.state.radius_access_state,
        proposed_state=proposed.radius_access_state,
        allowed_before=current.state.radius_allowed,
        allowed_after=proposed.radius_allowed,
        session_action=_session_action(command.kind),
        block_reason_after=proposed.access_block_reason,
    )
    delivery_mode = (
        classify_service_change_delivery(
            subscription.offer,
            target_offer,
            service_address_changed=address_changed,
        )
        if command.kind == SubscriptionCommandKind.change_plan
        and subscription.offer is not None
        and target_offer is not None
        else None
    )
    return SubscriptionLifecyclePreview(
        command=command,
        current=current,
        proposed=proposed,
        effective_at=effective_at,
        eligible=not reasons,
        eligibility_reasons=tuple(dict.fromkeys(reasons)),
        billing_impact=billing_impact,
        access_impact=access_impact,
        delivery_mode=delivery_mode,
        field_delivery_quote=field_delivery_quote,
    )


def _target_offer(
    db: Session,
    command: SubscriptionLifecycleCommand,
) -> CatalogOffer | None:
    if command.kind != SubscriptionCommandKind.change_plan:
        return None
    return db.get(CatalogOffer, coerce_uuid(command.target_offer_id))


def _target_service_address(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
) -> Address | None:
    if not command.target_service_address_id:
        return None
    address = db.get(Address, coerce_uuid(command.target_service_address_id))
    if address is None or str(address.subscriber_id) != str(subscription.subscriber_id):
        raise SubscriptionLifecycleError(
            "Target service address does not belong to this customer"
        )
    return address


def _address_label(address: Address) -> str:
    return ", ".join(
        part
        for part in (
            address.label,
            address.address_line1,
            address.address_line2,
            address.city,
            address.region,
        )
        if part
    )


def _resolve_field_delivery_quote(
    db: Session,
    subscription: Subscription,
    target_offer: CatalogOffer,
    target_address: Address,
) -> FieldDeliveryQuote:
    from app.models.domain_settings import SettingDomain
    from app.models.qualification import QualificationStatus
    from app.schemas.qualification import ServiceQualificationRequest
    from app.services import settings_spec
    from app.services.qualification import preview_service_qualification

    access_type = _enum_value(target_offer.access_type) or "unknown"
    qualification = preview_service_qualification(
        db,
        ServiceQualificationRequest(
            address_id=target_address.id,
            requested_tech=access_type,
            metadata_={
                "purpose": "subscription_relocation_preview",
                "subscription_id": str(subscription.id),
            },
        ),
    )
    fee_offer: CatalogOffer | None = None
    fee_amount = Decimal("0.00")
    currency = "NGN"
    blocking_reason: str | None = None
    if qualification.status != QualificationStatus.eligible:
        blocking_reason = "target_address_not_serviceable"

    # A wireless/radio address move is never silently free. Operations selects
    # the one-time catalog offer in Settings; its current active one-time price
    # is the only fee authority consumed by both customer and reseller portals.
    if access_type == "fixed_wireless":
        configured = settings_spec.resolve_value(
            db, SettingDomain.projects, "wireless_relocation_offer_id"
        )
        configured_id = str(configured or "").strip()
        if configured_id:
            fee_offer = db.get(CatalogOffer, coerce_uuid(configured_id))
        if fee_offer is None or not fee_offer.is_active:
            blocking_reason = "wireless_relocation_fee_not_configured"
        else:
            price = db.execute(
                select(OfferPrice)
                .where(OfferPrice.offer_id == fee_offer.id)
                .where(OfferPrice.price_type == PriceType.one_time)
                .where(OfferPrice.is_active.is_(True))
                .order_by(OfferPrice.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if price is None or Decimal(str(price.amount)) <= Decimal("0.00"):
                blocking_reason = "wireless_relocation_fee_not_configured"
            else:
                fee_amount = round_money(Decimal(str(price.amount)))
                currency = str(price.currency or "NGN")

    payload = {
        "subscription_id": str(subscription.id),
        "current_service_address_id": (
            str(subscription.service_address_id)
            if subscription.service_address_id
            else None
        ),
        "target_service_address_id": str(target_address.id),
        "target_offer_id": str(target_offer.id),
        "access_type": access_type,
        "qualification_status": qualification.status.value,
        "qualification_reasons": list(qualification.reasons),
        "qualification_coverage_area_id": (
            str(qualification.coverage_area_id)
            if qualification.coverage_area_id
            else None
        ),
        "fee_offer_id": str(fee_offer.id) if fee_offer else None,
        "fee_amount": str(fee_amount),
        "currency": currency,
        "blocking_reason": blocking_reason,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return FieldDeliveryQuote(
        target_service_address_id=str(target_address.id),
        target_address_label=_address_label(target_address),
        access_type=access_type,
        qualification_status=qualification.status.value,
        qualification_reasons=qualification.reasons,
        qualification_coverage_area_id=(
            str(qualification.coverage_area_id)
            if qualification.coverage_area_id
            else None
        ),
        fee_offer_id=str(fee_offer.id) if fee_offer else None,
        fee_offer_name=fee_offer.name if fee_offer else None,
        fee_amount=fee_amount,
        currency=currency,
        fingerprint=fingerprint,
        eligible=blocking_reason is None,
        blocking_reason=blocking_reason,
    )


def _eligibility_reasons(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
    *,
    target_offer: CatalogOffer | None,
    address_changed: bool,
    field_delivery_quote: FieldDeliveryQuote | None,
    now: datetime,
) -> list[str]:
    status = subscription.status
    reasons: list[str] = []
    allowed_statuses = {
        SubscriptionCommandKind.activate: {SubscriptionStatus.pending},
        SubscriptionCommandKind.suspend: {
            SubscriptionStatus.active,
            SubscriptionStatus.pending,
            SubscriptionStatus.blocked,
            SubscriptionStatus.suspended,
            SubscriptionStatus.stopped,
        },
        SubscriptionCommandKind.disable: set(SubscriptionStatus)
        - set(TERMINAL_SERVICE_STATUSES)
        - {SubscriptionStatus.disabled},
        SubscriptionCommandKind.restore: set(RESTORE_CANDIDATE_STATUSES),
        SubscriptionCommandKind.renew: {
            SubscriptionStatus.active,
            SubscriptionStatus.blocked,
            SubscriptionStatus.suspended,
        },
        SubscriptionCommandKind.cancel: set(SubscriptionStatus)
        - set(TERMINAL_SERVICE_STATUSES),
        SubscriptionCommandKind.expire: set(SubscriptionStatus)
        - set(TERMINAL_SERVICE_STATUSES),
        SubscriptionCommandKind.change_plan: {
            SubscriptionStatus.pending,
            SubscriptionStatus.active,
            SubscriptionStatus.suspended,
        },
        SubscriptionCommandKind.vacation_hold: {SubscriptionStatus.active},
        SubscriptionCommandKind.vacation_resume: {SubscriptionStatus.suspended},
    }
    if status not in allowed_statuses[command.kind]:
        reasons.append(f"status_{status.value}_not_eligible_for_{command.kind.value}")
    if command.kind == SubscriptionCommandKind.restore:
        unresolved_prepaid_lock = db.scalar(
            select(EnforcementLock.id)
            .where(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.reason == EnforcementReason.prepaid,
                EnforcementLock.is_active.is_(True),
            )
            .limit(1)
        )
        if unresolved_prepaid_lock is not None:
            reasons.append("prepaid_financial_reconciliation_required")
    if (
        command.effective_timing == SubscriptionEffectiveTiming.scheduled
        and command.effective_at is not None
        and (_aware_utc(command.effective_at) or now) < now
    ):
        reasons.append("effective_at_is_in_the_past")
    if command.kind in {
        SubscriptionCommandKind.vacation_hold,
        SubscriptionCommandKind.vacation_resume,
    }:
        decision = resolve_vacation_hold_policy(
            db,
            subscription,
            command_kind=command.kind,
            requested_days=command.vacation_hold_days,
            now=now,
        )
        reasons.extend(decision.reasons)
    if command.kind != SubscriptionCommandKind.change_plan:
        return reasons
    if target_offer is None:
        reasons.append("target_offer_not_found")
        return reasons
    if str(target_offer.id) == str(subscription.offer_id) and not address_changed:
        reasons.append("already_on_target_offer")
        return reasons
    if not target_offer.is_active or target_offer.status != OfferStatus.active:
        reasons.append("target_offer_inactive")
        return reasons
    if str(target_offer.id) != str(subscription.offer_id):
        try:
            from app.services.catalog.subscriptions import _validate_plan_change

            _validate_plan_change(db, subscription, str(target_offer.id))
        except HTTPException as exc:
            detail = exc.detail
            code = (
                str(detail.get("code"))
                if isinstance(detail, dict) and detail.get("code")
                else "plan_change_policy_rejected"
            )
            reasons.append(code)
    if _active_change_request(db, subscription.id) is not None:
        reasons.append("outstanding_plan_change_exists")
    if field_delivery_quote is not None and not field_delivery_quote.eligible:
        reasons.append(
            field_delivery_quote.blocking_reason or "field_delivery_not_eligible"
        )
    return reasons


def _active_change_request(
    db: Session, subscription_id: object
) -> SubscriptionChangeRequest | None:
    return db.scalar(
        select(SubscriptionChangeRequest)
        .where(SubscriptionChangeRequest.subscription_id == subscription_id)
        .where(
            SubscriptionChangeRequest.status.in_(
                {
                    SubscriptionChangeStatus.pending,
                    SubscriptionChangeStatus.approved,
                }
            )
        )
        .where(SubscriptionChangeRequest.applied_at.is_(None))
        .where(SubscriptionChangeRequest.is_active.is_(True))
        .order_by(SubscriptionChangeRequest.effective_date.asc())
    )


def _billing_impact(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
    *,
    current: SubscriptionLifecycleSnapshot,
    proposed: SubscriptionLifecycleState,
    target_offer: CatalogOffer | None,
    effective_at: datetime,
    current_balance: Decimal | None,
) -> tuple[SubscriptionBillingImpact, str | None]:
    before = current.state.billing_collectible
    after = proposed.billing_collectible
    if command.kind == SubscriptionCommandKind.change_plan and target_offer is not None:
        if str(target_offer.id) == str(subscription.offer_id):
            effective = effective_at.isoformat()
            fingerprint = hashlib.sha256(
                "|".join(
                    (
                        str(subscription.id),
                        str(subscription.offer_id),
                        str(command.target_service_address_id or ""),
                        current.head,
                        effective,
                    )
                ).encode("utf-8")
            ).hexdigest()
            quote: dict[str, object] = {
                "current_remaining_value": Decimal("0.00"),
                "required_amount": Decimal("0.00"),
                "prepaid_funding_before": current_balance or Decimal("0.00"),
                "prepaid_funding_after": current_balance or Decimal("0.00"),
                "postpaid_receivables": Decimal("0.00"),
                "currency": "NGN",
                "preview_effective_at": effective,
                "shortfall": Decimal("0.00"),
                "collection_blocking_balance": Decimal("0.00"),
                "charge_amount": Decimal("0.00"),
                "net_amount": Decimal("0.00"),
                "days_remaining": 0,
                "days_in_cycle": 0,
                "remaining_cycle_seconds": 0,
                "total_cycle_seconds": 0,
                "can_apply_immediately": True,
                "is_upgrade": False,
                "is_downgrade": False,
                "reason": None,
                "preview_fingerprint": fingerprint,
                "ledger_entry_type": None,
                "ledger_source": None,
                "ledger_amount": Decimal("0.00"),
                "access_consequence": "field_relocation_only",
            }
            return (
                SubscriptionBillingImpact(
                    action="preserve_current_service_until_relocation_verified",
                    collectible_before=before,
                    collectible_after=after,
                    currency="NGN",
                    details={"quote": quote},
                ),
                None,
            )
        if command.effective_timing != SubscriptionEffectiveTiming.immediate:
            currency, recurring = _recurring_price(db, target_offer.id)
            return (
                SubscriptionBillingImpact(
                    action="replace_recurring_price_at_effective_date",
                    collectible_before=before,
                    collectible_after=after,
                    currency=currency,
                    required_amount=recurring,
                    details={"target_recurring_amount": recurring},
                ),
                None,
            )
        from app.services.prepaid_plan_changes import resolve_prepaid_plan_change

        decision = resolve_prepaid_plan_change(
            db,
            subscription,
            str(target_offer.id),
            # Preserve the pricing owner's clock unless the command carries an
            # explicit effective time. Tests, scheduled jobs, and billing runs
            # can freeze that owner independently of this presentation layer.
            effective_at=_aware_utc(command.effective_at),
            prepaid_funding_before=current_balance,
        )
        quote = decision.as_quote_dict()
        return (
            SubscriptionBillingImpact(
                action="apply_prorated_plan_change",
                collectible_before=before,
                collectible_after=after,
                currency=decision.currency,
                charge_amount=round_money(
                    Decimal(str(decision.proration.get("charge_amount", "0.00")))
                ),
                credit_amount=round_money(
                    Decimal(str(decision.proration.get("credit_amount", "0.00")))
                ),
                net_amount=decision.net_amount,
                available_balance=decision.prepaid_funding_before,
                required_amount=decision.required_amount,
                shortfall=decision.shortfall,
                collection_blocking_balance=decision.collection_blocking_balance,
                details={"quote": quote},
            ),
            decision.reason,
        )
    if command.kind == SubscriptionCommandKind.renew:
        currency, recurring = _recurring_price(db, subscription.offer_id)
        return (
            SubscriptionBillingImpact(
                action="billing_owned_renewal",
                collectible_before=before,
                collectible_after=after,
                currency=currency,
                required_amount=recurring,
                details={"next_billing_at": effective_at.isoformat()},
            ),
            None,
        )
    action = {
        SubscriptionCommandKind.activate: "start_or_resume_collection",
        SubscriptionCommandKind.suspend: "continue_collection_while_held",
        SubscriptionCommandKind.disable: "stop_collection",
        SubscriptionCommandKind.restore: "collection_unchanged",
        SubscriptionCommandKind.cancel: "stop_collection",
        SubscriptionCommandKind.expire: "stop_collection",
        SubscriptionCommandKind.vacation_hold: "continue_collection_while_held",
        SubscriptionCommandKind.vacation_resume: "collection_unchanged",
    }.get(command.kind, "collection_unchanged")
    return (
        SubscriptionBillingImpact(
            action=action,
            collectible_before=before,
            collectible_after=after,
            possible_prorated_credit=command.kind == SubscriptionCommandKind.cancel,
        ),
        None,
    )


def _resolve_state(
    subscription: Subscription,
    *,
    subscriber: Subscriber | None,
    status: SubscriptionStatus | None = None,
    offer: CatalogOffer | None = None,
) -> SubscriptionLifecycleState:
    projected_status = status or subscription.status
    projected_offer = offer or subscription.offer
    billing_mode = subscription.billing_mode
    if (
        projected_offer is not None
        and str(projected_offer.id) != str(subscription.offer_id)
        and projected_offer.billing_mode is not None
    ):
        billing_mode = projected_offer.billing_mode
    projected = SimpleNamespace(
        id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        subscriber=subscriber,
        status=projected_status,
        billing_mode=billing_mode,
    )
    access = resolve_customer_access(projected, subscriber=subscriber)
    return SubscriptionLifecycleState(
        status=projected_status.value,
        offer_id=str(projected_offer.id if projected_offer else subscription.offer_id),
        offer_name=projected_offer.name if projected_offer else None,
        billing_mode=_enum_value(billing_mode) or "",
        billing_collectible=(
            access.is_billable_account
            and projected_status in BILLING_COLLECTIBLE_SERVICE_STATUSES
        ),
        mrr_countable=(
            access.is_active_customer_service
            and projected_status in MRR_COUNTABLE_SERVICE_STATUSES
        ),
        radius_access_state=(
            access.radius_access_state.value if access.radius_access_state else None
        ),
        radius_allowed=access.radius_allowed,
        radius_blocked=access.radius_blocked,
        access_block_reason=access.access_block_reason,
        terminal=projected_status in TERMINAL_SERVICE_STATUSES,
    )


def _proposed_status(
    current: SubscriptionStatus, kind: SubscriptionCommandKind
) -> SubscriptionStatus:
    if (
        kind in {SubscriptionCommandKind.suspend, SubscriptionCommandKind.vacation_hold}
        and current in _SUSPENDED_EQUIVALENT_STATUSES
    ):
        return current
    return {
        SubscriptionCommandKind.activate: SubscriptionStatus.active,
        SubscriptionCommandKind.suspend: SubscriptionStatus.suspended,
        SubscriptionCommandKind.disable: SubscriptionStatus.disabled,
        SubscriptionCommandKind.restore: SubscriptionStatus.active,
        SubscriptionCommandKind.cancel: SubscriptionStatus.canceled,
        SubscriptionCommandKind.expire: SubscriptionStatus.expired,
        SubscriptionCommandKind.vacation_hold: SubscriptionStatus.suspended,
        SubscriptionCommandKind.vacation_resume: SubscriptionStatus.active,
    }.get(kind, current)


def _session_action(kind: SubscriptionCommandKind) -> SubscriptionSessionAction:
    return {
        SubscriptionCommandKind.activate: SubscriptionSessionAction.authorize,
        SubscriptionCommandKind.restore: SubscriptionSessionAction.authorize,
        SubscriptionCommandKind.suspend: SubscriptionSessionAction.disconnect,
        SubscriptionCommandKind.disable: SubscriptionSessionAction.disconnect,
        SubscriptionCommandKind.cancel: SubscriptionSessionAction.deprovision,
        SubscriptionCommandKind.expire: SubscriptionSessionAction.deprovision,
        SubscriptionCommandKind.change_plan: SubscriptionSessionAction.reauthorize,
        SubscriptionCommandKind.vacation_hold: SubscriptionSessionAction.disconnect,
        SubscriptionCommandKind.vacation_resume: SubscriptionSessionAction.authorize,
    }.get(kind, SubscriptionSessionAction.none)


def _effective_at(
    current: SubscriptionLifecycleSnapshot,
    command: SubscriptionLifecycleCommand,
    *,
    now: datetime,
) -> datetime:
    if command.effective_at is not None:
        return _aware_utc(command.effective_at) or now
    if command.effective_timing == SubscriptionEffectiveTiming.next_cycle:
        return current.next_billing_at or now
    if command.kind == SubscriptionCommandKind.renew:
        return current.next_billing_at or now
    return now


def _recurring_price(db: Session, offer_id: object) -> tuple[str, Decimal]:
    row = db.execute(
        select(OfferPrice.currency, OfferPrice.amount)
        .where(OfferPrice.offer_id == offer_id)
        .where(OfferPrice.price_type == PriceType.recurring)
        .where(OfferPrice.is_active.is_(True))
        .order_by(OfferPrice.created_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return "NGN", Decimal("0.00")
    return str(row.currency or "NGN"), round_money(Decimal(str(row.amount)))


def _subscription_head(
    db: Session,
    subscription: Subscription,
    *,
    subscriber: object | None,
    pending_change: SubscriptionChangeRequest | None,
) -> str:
    currency, recurring_amount = _recurring_price(db, subscription.offer_id)
    offer = subscription.offer
    source = "|".join(
        (
            str(subscription.id),
            subscription.status.value,
            str(subscription.offer_id),
            _enum_value(subscription.billing_mode) or "missing-billing-mode",
            _head_datetime(subscription.start_at),
            _head_datetime(subscription.end_at),
            _head_datetime(subscription.next_billing_at),
            _head_datetime(subscription.canceled_at),
            _enum_value(getattr(subscriber, "status", None))
            or "missing-account-status",
            str(bool(subscriber and getattr(subscriber, "is_active", False))),
            _enum_value(getattr(offer, "status", None)) or "missing-offer-status",
            str(bool(offer and offer.is_active)),
            _enum_value(getattr(offer, "billing_mode", None))
            or "missing-offer-billing-mode",
            _head_datetime(getattr(offer, "updated_at", None)),
            currency,
            str(recurring_amount),
            str(pending_change.id) if pending_change else "no-pending-change",
            (
                _enum_value(pending_change.status) or "missing-change-status"
                if pending_change
                else "no-pending-change-status"
            ),
            (
                str(pending_change.requested_offer_id)
                if pending_change
                else "no-pending-target"
            ),
            (
                pending_change.effective_date.isoformat()
                if pending_change
                else "no-pending-effective-date"
            ),
            _head_datetime(
                getattr(pending_change, "updated_at", None) if pending_change else None
            ),
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _head_datetime(value: object | None) -> str:
    if not isinstance(value, datetime):
        return "missing"
    normalized = _aware_utc(value)
    return normalized.isoformat() if normalized else "missing"


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _enum_value(value: object | None) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


__all__ = [
    "FieldDeliveryQuote",
    "PendingSubscriptionChange",
    "ServiceChangeDeliveryMode",
    "SubscriptionAccessImpact",
    "SubscriptionBillingImpact",
    "SubscriptionCommandKind",
    "SubscriptionCommandOutcome",
    "SubscriptionCommandOutcomeStatus",
    "SubscriptionEffectiveTiming",
    "SubscriptionLifecycleCommand",
    "SubscriptionLifecycleError",
    "SubscriptionLifecycleHeadConflict",
    "SubscriptionLifecyclePreview",
    "SubscriptionLifecycleSnapshot",
    "SubscriptionLifecycleState",
    "SubscriptionSessionAction",
    "assert_subscription_head",
    "assert_subscription_transition",
    "classify_service_change_delivery",
    "preview_subscription_command",
    "resolve_subscription_lifecycle",
]
