"""Canonical owner of customer billing, funding, and RADIUS access decisions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.catalog import (
    AccessState,
    BillingMode,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import AccessRestrictionMode
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.billing_statuses import BILLABLE_SUBSCRIBER_STATUSES
from app.services.prepaid_currency import (
    normalize_prepaid_currency,
    resolve_prepaid_enforcement_currency,
)
from app.services.radius_access_state import derive_access_state
from app.services.subscriber_access_policy import RADIUS_BLOCKING_SUBSCRIBER_STATUSES

ACTIVE_CUSTOMER_SUBSCRIBER_STATUSES = frozenset({SubscriberStatus.active})
RADIUS_PERMISSIVE_SUBSCRIBER_STATUSES = frozenset(
    {
        SubscriberStatus.active,
        SubscriberStatus.delinquent,
    }
)


class SubscriberAccessInput(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def status(self) -> SubscriberStatus | None: ...

    @property
    def billing_mode(self) -> BillingMode | None: ...

    @property
    def is_active(self) -> bool: ...

    @property
    def billing_enabled(self) -> bool: ...


class SubscriptionAccessInput(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def subscriber_id(self) -> UUID: ...

    @property
    def subscriber(self) -> SubscriberAccessInput | None: ...

    @property
    def status(self) -> SubscriptionStatus | None: ...

    @property
    def billing_mode(self) -> BillingMode | None: ...


@dataclass(frozen=True, slots=True)
class CustomerBillingAccessState:
    """Resolved billing, customer-impact, and RADIUS state for one service."""

    subscription_id: UUID | None
    subscriber_id: UUID | None
    subscriber_status: str | None
    subscription_status: str | None
    account_billing_mode: str | None
    subscription_billing_mode: str | None
    account_enabled: bool
    account_billing_enabled: bool
    active_customer_service: bool
    billable_account: bool
    postpaid_invoice_eligible: bool
    prepaid_enforcement_eligible: bool
    counts_for_customer_impact: bool
    radius_access_state: AccessState | None
    radius_allowed: bool
    radius_blocked: bool
    radius_mode: str
    access_block_reason: str | None
    billing_block_reason: str | None


@dataclass(frozen=True, slots=True)
class CustomerAccessDecision:
    """Named access decision returned by the shared resolver."""

    state: CustomerBillingAccessState

    @property
    def subscription_id(self) -> UUID | None:
        return self.state.subscription_id

    @property
    def subscriber_id(self) -> UUID | None:
        return self.state.subscriber_id

    @property
    def is_active_customer_service(self) -> bool:
        return self.state.active_customer_service

    @property
    def is_billable_account(self) -> bool:
        return self.state.billable_account

    @property
    def is_postpaid_invoice_eligible(self) -> bool:
        return self.state.postpaid_invoice_eligible

    @property
    def is_prepaid_enforcement_eligible(self) -> bool:
        return self.state.prepaid_enforcement_eligible

    @property
    def counts_for_customer_impact(self) -> bool:
        return self.state.counts_for_customer_impact

    @property
    def radius_access_state(self) -> AccessState | None:
        return self.state.radius_access_state

    @property
    def radius_allowed(self) -> bool:
        return self.state.radius_allowed

    @property
    def radius_blocked(self) -> bool:
        return self.state.radius_blocked

    @property
    def radius_mode(self) -> str:
        return self.state.radius_mode

    @property
    def access_block_reason(self) -> str | None:
        return self.state.access_block_reason

    @property
    def billing_block_reason(self) -> str | None:
        return self.state.billing_block_reason


@dataclass(frozen=True, slots=True)
class PrepaidFundingDecision:
    """The one currency-bound funding decision used for prepaid access."""

    account_id: str
    available_balance: Decimal
    required_balance: Decimal
    currency: str
    configured_reserve_target: Decimal = Decimal("0.00")
    covered_subscription_ids: tuple[UUID, ...] = ()
    non_billable_subscription_ids: tuple[UUID, ...] = ()
    actionable_uncovered_subscription_ids: tuple[UUID, ...] = ()
    unresolved_projection_subscription_ids: tuple[UUID, ...] = ()
    unresolved_renewal_subscription_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "currency",
            normalize_prepaid_currency(self.currency),
        )

    @property
    def funded(self) -> bool:
        if (
            self.unresolved_projection_subscription_ids
            or self.unresolved_renewal_subscription_ids
        ):
            return False
        if (
            self.covered_subscription_ids or self.non_billable_subscription_ids
        ) and not self.actionable_uncovered_subscription_ids:
            return True
        return self.available_balance >= self.required_balance

    @property
    def adverse_action_allowed(self) -> bool:
        return (
            not (
                self.unresolved_projection_subscription_ids
                or self.unresolved_renewal_subscription_ids
            )
            and not self.funded
        )


def resolve_prepaid_available_balance(
    db: Session,
    account_id: UUID | str,
    *,
    currency: str | None = None,
) -> Decimal:
    """Resolve the currency-bound customer position used by prepaid policy."""
    from app.services.customer_financial_position import prepaid_available_balance

    resolved_currency = (
        resolve_prepaid_enforcement_currency(db)
        if currency is None
        else normalize_prepaid_currency(currency)
    )
    return prepaid_available_balance(
        db,
        account_id,
        currency=resolved_currency,
    )


def resolve_prepaid_funding(
    db: Session,
    account: Subscriber,
    *,
    now: datetime | None = None,
) -> PrepaidFundingDecision:
    """Compare canonical available balance with the canonical prepaid threshold."""
    from app.services.prepaid_threshold import resolve_prepaid_threshold_decision

    currency = resolve_prepaid_enforcement_currency(db)
    threshold = resolve_prepaid_threshold_decision(
        db,
        account,
        now=now,
        currency=currency,
    )
    return PrepaidFundingDecision(
        account_id=str(account.id),
        available_balance=resolve_prepaid_available_balance(
            db,
            account.id,
            currency=currency,
        ),
        required_balance=threshold.threshold,
        currency=currency,
        configured_reserve_target=threshold.configured_minimum,
        covered_subscription_ids=threshold.covered_subscription_ids,
        non_billable_subscription_ids=threshold.non_billable_subscription_ids,
        actionable_uncovered_subscription_ids=(
            threshold.actionable_uncovered_subscription_ids
        ),
        unresolved_projection_subscription_ids=(
            threshold.unresolved_projection_subscription_ids
        ),
        unresolved_renewal_subscription_ids=(
            threshold.unresolved_renewal_subscription_ids
        ),
    )


def resolve_prepaid_fundings(
    db: Session,
    account_ids: Sequence[UUID | str],
    *,
    now: datetime | None = None,
) -> dict[UUID, PrepaidFundingDecision]:
    """Cohort variant of :func:`resolve_prepaid_funding`.

    Identical decisions from the same two owners (threshold and available
    balance), resolved once for the whole cohort instead of ~24 statements
    per account. Keyed by account UUID.
    """
    from app.services.common import coerce_uuid
    from app.services.customer_financial_position import prepaid_available_balances
    from app.services.prepaid_threshold import resolve_prepaid_threshold_decisions

    ids = sorted({coerce_uuid(str(value)) for value in account_ids}, key=str)
    if not ids:
        return {}
    currency = resolve_prepaid_enforcement_currency(db)
    thresholds = resolve_prepaid_threshold_decisions(
        db, ids, now=now, currency=currency
    )
    balances = prepaid_available_balances(db, ids, currency=currency)
    decisions: dict[UUID, PrepaidFundingDecision] = {}
    for account_id in ids:
        threshold = thresholds[str(account_id)]
        decisions[account_id] = PrepaidFundingDecision(
            account_id=str(account_id),
            available_balance=balances.get(account_id, Decimal("0.00")),
            required_balance=threshold.threshold,
            currency=currency,
            configured_reserve_target=threshold.configured_minimum,
            covered_subscription_ids=threshold.covered_subscription_ids,
            non_billable_subscription_ids=threshold.non_billable_subscription_ids,
            actionable_uncovered_subscription_ids=(
                threshold.actionable_uncovered_subscription_ids
            ),
            unresolved_projection_subscription_ids=(
                threshold.unresolved_projection_subscription_ids
            ),
            unresolved_renewal_subscription_ids=(
                threshold.unresolved_renewal_subscription_ids
            ),
        )
    return decisions


def resolve_customer_access(
    subscription: SubscriptionAccessInput,
    *,
    subscriber: SubscriberAccessInput | None = None,
    access_restriction_mode: AccessRestrictionMode | None = None,
) -> CustomerAccessDecision:
    """Resolve one subscription's customer-facing billing and access state."""
    account = subscriber if subscriber is not None else subscription.subscriber
    subscription_status = subscription.status
    subscriber_status = account.status if account is not None else None
    subscription_mode = subscription.billing_mode
    account_mode = account.billing_mode if account is not None else None

    account_enabled = bool(account is not None and account.is_active)
    account_billing_enabled = bool(account is not None and account.billing_enabled)
    billable_account = (
        account_enabled
        and account_billing_enabled
        and subscriber_status in BILLABLE_SUBSCRIBER_STATUSES
    )
    active_customer_service = (
        account_enabled
        and subscriber_status in ACTIVE_CUSTOMER_SUBSCRIBER_STATUSES
        and subscription_status == SubscriptionStatus.active
    )
    postpaid_invoice_eligible = (
        billable_account
        and subscription_status == SubscriptionStatus.active
        and subscription_mode != BillingMode.prepaid
    )
    prepaid_enforcement_eligible = (
        billable_account
        and subscription_status in COLLECTIBLE_SERVICE_STATUSES
        and subscription_mode == BillingMode.prepaid
    )

    account_hard_reject = _account_radius_hard_reject(
        subscriber_status=subscriber_status,
        account_enabled=account_enabled,
        subscriber_missing=account is None,
    )
    radius_state = (
        derive_access_state(
            subscription_status,
            restriction_mode=(
                AccessRestrictionMode.hard_reject
                if account_hard_reject
                and not (
                    access_restriction_mode == AccessRestrictionMode.captive
                    and account_enabled
                    and subscriber_status
                    in {SubscriberStatus.blocked, SubscriberStatus.suspended}
                )
                else access_restriction_mode
            ),
        )
        if isinstance(subscription_status, SubscriptionStatus)
        else None
    )
    if account_hard_reject and radius_state == AccessState.active:
        radius_state = AccessState.suspended
    radius_blocked = radius_state in {AccessState.suspended, AccessState.captive}
    radius_allowed = radius_state in {AccessState.active, AccessState.captive}

    state = CustomerBillingAccessState(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id
        or (account.id if account is not None else None),
        subscriber_status=_enum_value(subscriber_status),
        subscription_status=_enum_value(subscription_status),
        account_billing_mode=_enum_value(account_mode),
        subscription_billing_mode=_enum_value(subscription_mode),
        account_enabled=account_enabled,
        account_billing_enabled=account_billing_enabled,
        active_customer_service=active_customer_service,
        billable_account=billable_account,
        postpaid_invoice_eligible=postpaid_invoice_eligible,
        prepaid_enforcement_eligible=prepaid_enforcement_eligible,
        counts_for_customer_impact=active_customer_service,
        radius_access_state=radius_state,
        radius_allowed=radius_allowed,
        radius_blocked=radius_blocked,
        radius_mode=_radius_mode(radius_state),
        access_block_reason=_access_block_reason(
            subscription_status=subscription_status,
            subscriber_status=subscriber_status,
            account_enabled=account_enabled,
            subscriber_missing=account is None,
            radius_state=radius_state,
        ),
        billing_block_reason=_billing_block_reason(
            subscription_status=subscription_status,
            subscriber_status=subscriber_status,
            account_enabled=account_enabled,
            account_billing_enabled=account_billing_enabled,
            subscription_mode=subscription_mode,
        ),
    )
    return CustomerAccessDecision(state=state)


def active_customer_service_filters(
    subscription_model: type[Subscription],
    subscriber_model: type[Subscriber],
) -> tuple[ColumnElement[bool], ...]:
    """SQL predicates for subscriptions counted as active customer service."""
    return (
        subscription_model.status == SubscriptionStatus.active,
        subscriber_model.status.in_(ACTIVE_CUSTOMER_SUBSCRIBER_STATUSES),
        subscriber_model.is_active.is_(True),
    )


def postpaid_billing_filters(
    subscription_model: type[Subscription],
    subscriber_model: type[Subscriber],
) -> tuple[ColumnElement[bool], ...]:
    """SQL predicates for the postpaid invoice-cycle cohort."""
    return (
        subscription_model.status == SubscriptionStatus.active,
        subscriber_model.status.in_(BILLABLE_SUBSCRIBER_STATUSES),
        subscription_model.billing_mode != BillingMode.prepaid,
    )


def prepaid_enforcement_filters(
    subscription_model: type[Subscription],
    subscriber_model: type[Subscriber],
) -> tuple[ColumnElement[bool], ...]:
    """SQL predicates for prepaid balance enforcement and exposure cohorts."""
    return (
        subscription_model.status.in_(COLLECTIBLE_SERVICE_STATUSES),
        subscriber_model.status.in_(BILLABLE_SUBSCRIBER_STATUSES),
        subscriber_model.is_active.is_(True),
        subscriber_model.billing_enabled.is_(True),
        subscription_model.billing_mode == BillingMode.prepaid,
    )


def _account_radius_hard_reject(
    *,
    subscriber_status: SubscriberStatus | None,
    account_enabled: bool,
    subscriber_missing: bool,
) -> bool:
    if subscriber_missing or not account_enabled:
        return True
    if subscriber_status in RADIUS_BLOCKING_SUBSCRIBER_STATUSES:
        return True
    if subscriber_status in RADIUS_PERMISSIVE_SUBSCRIBER_STATUSES:
        return False
    return True


def _access_block_reason(
    *,
    subscription_status: SubscriptionStatus | None,
    subscriber_status: SubscriberStatus | None,
    account_enabled: bool,
    subscriber_missing: bool,
    radius_state: AccessState | None,
) -> str | None:
    if subscriber_missing:
        return "subscriber_missing"
    if not account_enabled:
        return "subscriber_inactive"
    if subscriber_status in RADIUS_BLOCKING_SUBSCRIBER_STATUSES:
        return f"subscriber_status_{_enum_value(subscriber_status)}"
    if radius_state in {
        AccessState.suspended,
        AccessState.captive,
        AccessState.terminated,
    }:
        return f"subscription_status_{_enum_value(subscription_status)}"
    if radius_state is None:
        return f"subscription_unprovisioned_{_enum_value(subscription_status)}"
    return None


def _billing_block_reason(
    *,
    subscription_status: SubscriptionStatus | None,
    subscriber_status: SubscriberStatus | None,
    account_enabled: bool,
    account_billing_enabled: bool,
    subscription_mode: BillingMode | None,
) -> str | None:
    if not account_enabled:
        return "subscriber_inactive"
    if not account_billing_enabled:
        return "account_billing_disabled"
    if subscriber_status not in BILLABLE_SUBSCRIBER_STATUSES:
        return f"subscriber_status_{_enum_value(subscriber_status)}"
    if subscription_status not in COLLECTIBLE_SERVICE_STATUSES:
        return f"subscription_status_{_enum_value(subscription_status)}"
    if subscription_mode == BillingMode.prepaid:
        return "prepaid_not_postpaid_invoice_eligible"
    return None


def _enum_value(value: object | None) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _radius_mode(state: AccessState | None) -> str:
    if state is None:
        return "none"
    if state == AccessState.suspended:
        return "reject"
    return state.value


__all__ = [
    "CustomerAccessDecision",
    "CustomerBillingAccessState",
    "PrepaidFundingDecision",
    "active_customer_service_filters",
    "postpaid_billing_filters",
    "prepaid_enforcement_filters",
    "resolve_customer_access",
    "resolve_prepaid_available_balance",
    "resolve_prepaid_funding",
]
