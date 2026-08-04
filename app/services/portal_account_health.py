"""Shared Customer and Reseller portal Account/Service Health projection.

This read owner composes existing lifecycle, financial, access, live-session,
connection-diagnosis, and outage owners. It does not poll equipment, decide a
transition, or mutate state. Customer and reseller templates render the same
facts while adapting navigation to their audience.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.schemas.service_status import ServiceStatusAction, ServiceStatusItem
from app.schemas.status_presentation import StatusPresentation
from app.services import display_format
from app.services.billing_profile import resolve_billing_profile
from app.services.common import coerce_uuid
from app.services.customer_financial_position import (
    get_customer_receivable_summaries,
    prepaid_available_balance,
)
from app.services.domain_errors import DomainError
from app.services.network.radius_sessions import (
    SubscriptionSessionSnapshot,
    subscription_session_snapshots,
)
from app.services.prepaid_currency import resolve_prepaid_enforcement_currency
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)
from app.services.service_status import (
    build_service_status,
    list_current_service_subscriptions,
)
from app.services.status_presentation import (
    access_session_status_presentation,
    account_status_presentation,
    service_access_status_presentation,
    subscription_status_presentation,
)
from app.services.subscription_lifecycle import classify_service_change_delivery
from app.services.topology.connection_status import assess
from app.services.ui_contracts import StateValue

logger = logging.getLogger(__name__)


class PortalServiceAccessState(StrEnum):
    available = "available"
    restricted = "restricted"
    unavailable = "unavailable"


@dataclass(frozen=True, slots=True)
class MoneyAmount:
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class ReceivableLane:
    currency: str
    outstanding: Decimal
    overdue: Decimal
    overdue_count: int


@dataclass(frozen=True, slots=True)
class PortalFinancialHealth:
    billing_mode: StateValue
    billing_mode_reason: str
    receivables: StateValue
    prepaid_funding: StateValue
    prepaid_funding_reason: str


@dataclass(frozen=True, slots=True)
class PortalConnectionDiagnosis:
    state: str
    status_presentation: StatusPresentation
    headline: str
    message: str
    advice: str | None
    medium: str | None
    area_outage: bool
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class PortalRemoteProvisionPriceReview:
    fingerprint: str
    effective_at: datetime
    previous_amount: Decimal
    current_amount: Decimal
    currency: str
    available_balance: Decimal
    shortfall: Decimal
    allowed: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class PortalPendingServiceChange:
    request_id: UUID
    status: str
    target_offer_name: str
    effective_date: date
    delivery_mode: str
    delivery_state: str
    target_service_address: str | None
    field_fee_amount: Decimal | None
    field_fee_currency: str | None
    price_review: PortalRemoteProvisionPriceReview | None


@dataclass(frozen=True, slots=True)
class PortalServiceHealth:
    subscription_id: UUID
    offer_name: str
    lifecycle: StatusPresentation
    billing_mode: str | None
    access_state: PortalServiceAccessState
    access: StatusPresentation
    access_reason: str
    session: SubscriptionSessionSnapshot
    session_presentation: StatusPresentation
    connection: StateValue
    next_charge_at: datetime | None
    expires_at: datetime | None
    next_action: ServiceStatusAction | None
    customer_action_url: str | None
    pending_change: PortalPendingServiceChange | None


@dataclass(frozen=True, slots=True)
class PortalAccountHealth:
    account_id: UUID
    account_number: str | None
    subscriber_number: str | None
    display_name: str
    lifecycle: StatusPresentation
    financial: PortalFinancialHealth
    services: tuple[PortalServiceHealth, ...]
    primary_action: ServiceStatusAction | None
    customer_primary_action_url: str | None
    as_of: datetime

    @property
    def has_partial_data(self) -> bool:
        if not self.financial.billing_mode.is_present:
            return True
        if not self.financial.receivables.is_present:
            return True
        if self.financial.prepaid_funding.kind.value in {"unknown", "unavailable"}:
            return True
        return any(
            service.access_state == PortalServiceAccessState.unavailable
            or service.connection.kind.value == "unavailable"
            for service in self.services
        )

    def for_subscription(self, subscription_id: UUID) -> PortalAccountHealth:
        service = next(
            (item for item in self.services if item.subscription_id == subscription_id),
            None,
        )
        if service is None:
            raise ValueError(
                f"Subscription {subscription_id} is not in this account projection"
            )
        return replace(
            self,
            services=(service,),
            primary_action=service.next_action,
            customer_primary_action_url=service.customer_action_url,
        )

    def for_active_services(self) -> PortalAccountHealth:
        """Narrow the projection to services in the active lifecycle state."""
        active_services = tuple(
            service
            for service in self.services
            if service.lifecycle.value == SubscriptionStatus.active.value
        )
        return replace(
            self,
            services=active_services,
            primary_action=None,
            customer_primary_action_url=None,
        )


_CUSTOMER_ACTION_URLS = {
    "top_up": "/portal/billing/topup",
    "pay_invoices": "/portal/billing",
    "view_usage": "/portal/usage",
    "contact_support": "/portal/support/new",
}


def _display_name(account: Subscriber) -> str:
    return (
        account.company_name
        or account.display_name
        or " ".join(
            part for part in (account.first_name, account.last_name) if part
        ).strip()
        or account.email
        or account.account_number
        or "Customer"
    )


def _customer_action_url(action: ServiceStatusAction | None) -> str | None:
    if action is None:
        return None
    return _CUSTOMER_ACTION_URLS.get(action.kind.value)


def _financial_health(db: Session, account: Subscriber) -> PortalFinancialHealth:
    profile = resolve_billing_profile(db, account)
    if profile.effective_mode is None:
        billing_mode = StateValue.unknown()
    else:
        billing_mode = StateValue.present(
            profile.effective_mode.value.replace("_", " ").title()
        )
    billing_mode_reason = (
        profile.invalid_reason.value.replace("_", " ").capitalize()
        if profile.invalid_reason
        else f"Resolved from {profile.source.value.replace('_', ' ')}."
    )

    try:
        summaries = get_customer_receivable_summaries(
            db,
            account.id,
            default_currency=display_format.default_currency(db),
        )
        receivables = StateValue.present(
            tuple(
                ReceivableLane(
                    currency=summary.currency,
                    outstanding=summary.outstanding,
                    overdue=summary.overdue,
                    overdue_count=summary.overdue_count,
                )
                for summary in summaries
            )
        )
    except Exception as exc:
        logger.warning(
            "portal_receivables_unavailable",
            extra={"account_id": str(account.id), "error_type": type(exc).__name__},
            exc_info=True,
        )
        receivables = StateValue.unavailable()

    funding_reason = "Not applicable to a postpaid account."
    if profile.effective_mode == BillingMode.prepaid:
        try:
            currency = resolve_prepaid_enforcement_currency(db)
            funding = StateValue.present(
                MoneyAmount(
                    amount=prepaid_available_balance(
                        db,
                        account.id,
                        currency=currency,
                    ),
                    currency=currency,
                )
            )
            funding_reason = (
                "Reviewed opening position plus canonical native events in the "
                "enforcement currency."
            )
        except (DomainError, PrepaidFundingBaselineMissingError, ValueError) as exc:
            logger.warning(
                "portal_prepaid_funding_unavailable",
                extra={"account_id": str(account.id), "error_type": type(exc).__name__},
            )
            funding = StateValue.unavailable()
            funding_reason = "Authoritative prepaid funding evidence is unavailable."
    elif profile.effective_mode == BillingMode.postpaid:
        funding = StateValue.not_applicable()
    else:
        funding = StateValue.unknown()
        funding_reason = "Billing mode is unresolved; funding cannot be classified."

    return PortalFinancialHealth(
        billing_mode=billing_mode,
        billing_mode_reason=billing_mode_reason,
        receivables=receivables,
        prepaid_funding=funding,
        prepaid_funding_reason=funding_reason,
    )


def _status_items(
    db: Session, account_id: UUID
) -> tuple[dict[UUID, ServiceStatusItem], ServiceStatusAction | None, datetime]:
    try:
        response = build_service_status(db, str(account_id))
    except (DomainError, PrepaidFundingBaselineMissingError, ValueError) as exc:
        logger.warning(
            "portal_service_status_unavailable",
            extra={"account_id": str(account_id), "error_type": type(exc).__name__},
        )
        return {}, None, datetime.now(UTC)
    return (
        {item.subscription_id: item for item in response.services},
        response.primary_action,
        response.as_of,
    )


def _connection_diagnosis(db: Session, subscription) -> StateValue:
    if subscription.status != SubscriptionStatus.active:
        return StateValue.not_applicable()
    try:
        result = assess(db, subscription)
    except Exception:
        logger.warning(
            "portal_connection_diagnosis_unavailable",
            extra={"subscription_id": str(subscription.id)},
            exc_info=True,
        )
        return StateValue.unavailable()
    return StateValue.present(
        PortalConnectionDiagnosis(
            state=result.state,
            status_presentation=result.status_presentation,
            headline=result.headline,
            message=result.message,
            advice=result.advice,
            medium=result.medium,
            area_outage=result.is_area_outage,
            checked_at=result.checked_at,
        ),
        as_of=result.checked_at,
    )


def _service_health(
    db: Session,
    subscription,
    status_item: ServiceStatusItem | None,
    session: SubscriptionSessionSnapshot,
    pending_change: PortalPendingServiceChange | None,
) -> PortalServiceHealth:
    if status_item is None:
        access_state = PortalServiceAccessState.unavailable
        access_reason = "Service access could not be resolved."
        billing_mode = None
        action = None
        next_charge_at = subscription.next_billing_at
        expires_at = None
    else:
        access_state = (
            PortalServiceAccessState.available
            if status_item.usable
            else PortalServiceAccessState.restricted
        )
        access_reason = (
            status_item.action.message
            if status_item.action is not None
            else "No access hold is active."
        )
        billing_mode = status_item.billing_mode
        action = status_item.action
        next_charge_at = status_item.next_charge_at
        expires_at = status_item.expires_at

    return PortalServiceHealth(
        subscription_id=subscription.id,
        offer_name=(subscription.offer.name if subscription.offer else "Service"),
        lifecycle=subscription_status_presentation(subscription.status),
        billing_mode=billing_mode,
        access_state=access_state,
        access=service_access_status_presentation(access_state.value),
        access_reason=access_reason,
        session=session,
        session_presentation=access_session_status_presentation(session.state.value),
        connection=_connection_diagnosis(db, subscription),
        next_charge_at=next_charge_at,
        expires_at=expires_at,
        next_action=action,
        customer_action_url=_customer_action_url(action),
        pending_change=pending_change,
    )


def _review_decimal(raw: dict[object, object], key: str) -> Decimal | None:
    try:
        return Decimal(str(raw[key])).quantize(Decimal("0.01"))
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return None


def _remote_price_review(
    confirmation: dict[object, object],
) -> PortalRemoteProvisionPriceReview | None:
    raw = confirmation.get("provisioning_price_review")
    if not isinstance(raw, dict):
        return None
    fingerprint = str(raw.get("fingerprint") or "").strip()
    effective_raw = str(raw.get("effective_at") or "").strip()
    previous_amount = _review_decimal(raw, "previous_amount")
    current_amount = _review_decimal(raw, "current_amount")
    available_balance = _review_decimal(raw, "available_balance")
    shortfall = _review_decimal(raw, "shortfall")
    if (
        not fingerprint
        or not effective_raw
        or previous_amount is None
        or current_amount is None
        or available_balance is None
        or shortfall is None
    ):
        return None
    try:
        effective_at = datetime.fromisoformat(effective_raw)
    except ValueError:
        return None
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=UTC)
    else:
        effective_at = effective_at.astimezone(UTC)
    return PortalRemoteProvisionPriceReview(
        fingerprint=fingerprint,
        effective_at=effective_at,
        previous_amount=previous_amount,
        current_amount=current_amount,
        currency=str(raw.get("currency") or "NGN").upper(),
        available_balance=available_balance,
        shortfall=shortfall,
        allowed=bool(raw.get("allowed", False)),
        reason=str(raw.get("reason") or "").strip() or None,
    )


def _pending_service_changes(
    db: Session,
    subscriptions: list[Subscription],
) -> dict[UUID, PortalPendingServiceChange]:
    subscription_ids = [subscription.id for subscription in subscriptions]
    if not subscription_ids:
        return {}
    rows = db.scalars(
        select(SubscriptionChangeRequest)
        .where(SubscriptionChangeRequest.subscription_id.in_(subscription_ids))
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
        .options(
            selectinload(SubscriptionChangeRequest.current_offer),
            selectinload(SubscriptionChangeRequest.requested_offer),
            selectinload(SubscriptionChangeRequest.target_service_address),
        )
        .order_by(
            SubscriptionChangeRequest.effective_date.asc(),
            SubscriptionChangeRequest.created_at.asc(),
        )
    ).all()
    changes: dict[UUID, PortalPendingServiceChange] = {}
    for row in rows:
        if row.subscription_id in changes:
            continue
        delivery_mode = (
            classify_service_change_delivery(
                row.current_offer,
                row.requested_offer,
                service_address_changed=row.target_service_address_id is not None,
            ).value
            if row.current_offer is not None and row.requested_offer is not None
            else "unknown"
        )
        confirmation = row.confirmation_snapshot or {}
        target_address = row.target_service_address
        changes[row.subscription_id] = PortalPendingServiceChange(
            request_id=row.id,
            status=row.status.value,
            target_offer_name=(
                row.requested_offer.name if row.requested_offer else "Requested plan"
            ),
            effective_date=row.effective_date,
            delivery_mode=delivery_mode,
            delivery_state=str(
                confirmation.get("delivery_state") or "awaiting_verification"
            ),
            target_service_address=(
                ", ".join(
                    part
                    for part in (
                        target_address.label,
                        target_address.address_line1,
                        target_address.city,
                        target_address.region,
                    )
                    if part
                )
                if target_address is not None
                else None
            ),
            field_fee_amount=row.field_fee_amount,
            field_fee_currency=row.field_fee_currency,
            price_review=_remote_price_review(confirmation),
        )
    return changes


def build_portal_account_health(
    db: Session,
    account_id: UUID,
) -> PortalAccountHealth:
    """Build the shared first-viewport projection for one exact account."""
    resolved_account_id = coerce_uuid(account_id)
    account = db.get(Subscriber, resolved_account_id)
    if account is None:
        raise ValueError(f"Subscriber {resolved_account_id} was not found")

    subscriptions = list_current_service_subscriptions(db, str(resolved_account_id))
    sessions = subscription_session_snapshots(db, subscriptions)
    pending_changes = _pending_service_changes(db, subscriptions)
    status_items, primary_action, as_of = _status_items(db, resolved_account_id)
    service_rows = tuple(
        _service_health(
            db,
            subscription,
            status_items.get(subscription.id),
            sessions[subscription.id],
            pending_changes.get(subscription.id),
        )
        for subscription in subscriptions
    )
    return PortalAccountHealth(
        account_id=resolved_account_id,
        account_number=account.account_number,
        subscriber_number=account.subscriber_number,
        display_name=_display_name(account),
        lifecycle=account_status_presentation(
            account.status,
            is_active=account.is_active,
        ),
        financial=_financial_health(db, account),
        services=service_rows,
        primary_action=primary_action,
        customer_primary_action_url=_customer_action_url(primary_action),
        as_of=as_of,
    )
