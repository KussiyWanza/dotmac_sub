"""Owner of the prepaid enforcement threshold.

The prepaid enforcement gate asks one question: *how much balance must this
account hold before we stop suspending it?* This module owns that decision.

There is exactly one implementation of the rule — the batch one. The scalar
entry point is a thin adapter over it, so the enforcement sweep, the customer
portal projection, and the audit harness cannot drift apart. A second
implementation of a suspension threshold would let an audit tool disagree with
the enforcement it exists to check.

The access threshold is zero while every collectible prepaid service has exact
current coverage. A configured minimum is a reserve/top-up target, not a reason
to suspend service that is already paid for. When a service is due and has no
current coverage, the threshold is::

    max(configured_minimum, unfunded_renewal_requirement)

where ``configured_minimum`` is the account's ``min_balance`` override, falling
back to the canonical ``billing.prepaid_default_min_balance`` setting, and
``unfunded_renewal_requirement`` is the summed exact taxed contracted renewal
charge of every due, uncovered collectible prepaid subscription. Charge
resolution belongs to ``financial.prepaid_service_renewals`` and coverage
classification belongs to ``financial.prepaid_service_coverage``. A future
billing anchor without coverage, exact or malformed financial evidence without
a coverage projection, or a due service without complete contract terms blocks
adverse enforcement through a typed outcome.

Query cost is bounded by the number of accounts *batches*, not by the number of
accounts: resolving 5,269 accounts costs the same handful of queries as resolving
250. The per-account form issued ~6 queries each, which made a full enforcement
audit ~16k statements and unfit to run against production.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    BillingMode,
    Subscription,
)
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.services import settings_spec
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.domain_errors import DomainError
from app.services.prepaid_currency import (
    normalize_prepaid_currency,
    resolve_prepaid_enforcement_currency,
)

ZERO = Decimal("0.00")


class PrepaidThresholdError(DomainError):
    """Stable failure at the prepaid-threshold boundary."""


class PrepaidCurrencyMismatchError(PrepaidThresholdError):
    """A price would require comparing amounts in different currencies."""


@dataclass(frozen=True, slots=True)
class PrepaidThresholdDecision:
    """Currency-bound threshold with exact minimum and renewal provenance."""

    account_id: str
    configured_minimum: Decimal
    unfunded_renewal_requirement: Decimal
    currency: str
    covered_subscription_ids: tuple[UUID, ...] = ()
    non_billable_subscription_ids: tuple[UUID, ...] = ()
    actionable_uncovered_subscription_ids: tuple[UUID, ...] = ()
    unresolved_projection_subscription_ids: tuple[UUID, ...] = ()
    unresolved_renewal_subscription_ids: tuple[UUID, ...] = ()

    @property
    def threshold(self) -> Decimal:
        if not self.actionable_uncovered_subscription_ids:
            return ZERO
        return max(self.configured_minimum, self.unfunded_renewal_requirement)

    @property
    def top_up_target(self) -> Decimal:
        """Advisory reserve/renewal target, never a coverage override."""
        return max(self.configured_minimum, self.unfunded_renewal_requirement)


def _minimum(value: object, *, source: str) -> Decimal:
    try:
        minimum = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PrepaidThresholdError(
            code="financial.prepaid_threshold.invalid_minimum_balance",
            message="The prepaid minimum balance must be a non-negative amount.",
            details={"source": source},
        ) from exc
    if not minimum.is_finite() or minimum < ZERO:
        raise PrepaidThresholdError(
            code="financial.prepaid_threshold.invalid_minimum_balance",
            message="The prepaid minimum balance must be a non-negative amount.",
            details={"source": source},
        )
    return minimum


def _decisions(
    account_ids: Sequence[str],
    *,
    configured: dict[str, Decimal],
    required: dict[str, Decimal],
    currency: str,
    covered: dict[str, tuple[UUID, ...]] | None = None,
    non_billable: dict[str, tuple[UUID, ...]] | None = None,
    actionable: dict[str, tuple[UUID, ...]] | None = None,
    unresolved: dict[str, tuple[UUID, ...]] | None = None,
    unresolved_renewal: dict[str, tuple[UUID, ...]] | None = None,
) -> dict[str, PrepaidThresholdDecision]:
    covered = covered or {}
    non_billable = non_billable or {}
    actionable = actionable or {}
    unresolved = unresolved or {}
    unresolved_renewal = unresolved_renewal or {}
    return {
        account_id: PrepaidThresholdDecision(
            account_id=account_id,
            configured_minimum=configured[account_id],
            unfunded_renewal_requirement=required.get(account_id, ZERO),
            currency=currency,
            covered_subscription_ids=covered.get(account_id, ()),
            non_billable_subscription_ids=non_billable.get(account_id, ()),
            actionable_uncovered_subscription_ids=actionable.get(account_id, ()),
            unresolved_projection_subscription_ids=unresolved.get(account_id, ()),
            unresolved_renewal_subscription_ids=unresolved_renewal.get(account_id, ()),
        )
        for account_id in account_ids
    }


def resolve_prepaid_threshold_decisions(
    db: Session,
    account_ids: Sequence[UUID | str],
    *,
    now: datetime | None = None,
    currency: str | None = None,
) -> dict[str, PrepaidThresholdDecision]:
    """Resolve the prepaid enforcement threshold for many accounts at once.

    This is the owner. Returns one typed provenance decision for every id;
    an account with no prepaid service has a zero renewal requirement.
    """
    effective_now = now or datetime.now(UTC)
    enforcement_currency = (
        normalize_prepaid_currency(currency)
        if currency is not None
        else resolve_prepaid_enforcement_currency(db)
    )
    ids = [str(a) for a in account_ids]
    if not ids:
        return {}

    # 1. configured minimum: per-account override, else the domain default.
    default_raw = settings_spec.resolve_value(
        db, SettingDomain.billing, "prepaid_default_min_balance"
    )
    default_minimum = _minimum(
        default_raw if default_raw is not None else ZERO,
        source="billing.prepaid_default_min_balance",
    )
    configured: dict[str, Decimal] = {}
    for account_id, min_balance in db.execute(
        select(Subscriber.id, Subscriber.min_balance).where(Subscriber.id.in_(ids))
    ).all():
        account_key = str(account_id)
        configured[account_key] = (
            _minimum(min_balance, source=f"account:{account_key}")
            if min_balance is not None
            else default_minimum
        )
    missing_accounts = sorted(set(ids) - set(configured))
    if missing_accounts:
        raise PrepaidThresholdError(
            code="financial.prepaid_threshold.account_not_found",
            message="A prepaid threshold account was not found.",
            details={"account_ids": missing_accounts},
        )

    # 2. the collectible prepaid subscriptions those accounts hold.
    subscriptions = list(
        db.scalars(
            select(Subscription).where(
                Subscription.subscriber_id.in_(ids),
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
            )
        ).all()
    )
    if not subscriptions:
        return _decisions(
            ids,
            configured=configured,
            required={},
            currency=enforcement_currency,
        )

    # 3. Resolve explicit customer-billing treatment before payment coverage.
    from app.services.subscription_billing_treatments import (
        resolve_subscription_billing_treatments,
    )

    treatment_decisions = resolve_subscription_billing_treatments(
        db, subscriptions, as_of=effective_now
    )
    non_billable_by_account: dict[str, list[UUID]] = defaultdict(list)
    standard_subscriptions: list[Subscription] = []
    for subscription in subscriptions:
        if treatment_decisions[subscription.id].suppress_customer_billing:
            non_billable_by_account[str(subscription.subscriber_id)].append(
                subscription.id
            )
        else:
            standard_subscriptions.append(subscription)

    # 4. Classify standard service through the named coverage owner, then ask
    # the reconciliation owner whether any uncovered service has exact or
    # malformed financial source evidence. Paid invoices remain source evidence
    # rather than read-time coverage, but a missing entitlement projection is
    # never safe-to-suspend.
    from app.services.prepaid_service_coverage import (
        PrepaidCoverageStatus,
        resolve_prepaid_service_coverage,
    )

    coverage = resolve_prepaid_service_coverage(
        db,
        standard_subscriptions,
        as_of=effective_now,
    )
    uncovered_subscriptions = [
        subscription
        for subscription in standard_subscriptions
        if not coverage[subscription.id].covered
    ]
    if uncovered_subscriptions:
        from app.services.prepaid_coverage_reconciliation import (
            resolve_prepaid_coverage_enforcement_blockers,
        )

        enforcement_blocked_ids = {
            item.subscription_id
            for item in resolve_prepaid_coverage_enforcement_blockers(
                db,
                uncovered_subscriptions,
                as_of=effective_now,
            )
        }
    else:
        enforcement_blocked_ids = set()
    covered_by_account: dict[str, list[UUID]] = defaultdict(list)
    unresolved_by_account: dict[str, list[UUID]] = defaultdict(list)
    for subscription in standard_subscriptions:
        account_key = str(subscription.subscriber_id)
        status = coverage[subscription.id].status
        if subscription.id in enforcement_blocked_ids:
            unresolved_by_account[account_key].append(subscription.id)
        elif status == PrepaidCoverageStatus.covered:
            covered_by_account[account_key].append(subscription.id)
        elif status == PrepaidCoverageStatus.unresolved_projection:
            unresolved_by_account[account_key].append(subscription.id)

    # 5. Only genuinely due/uncovered standard services form a requirement.
    # Unresolved projections are quarantined from adverse action.
    unfunded = [
        subscription
        for subscription in standard_subscriptions
        if (
            coverage[subscription.id].status == PrepaidCoverageStatus.uncovered_due
            and subscription.id not in enforcement_blocked_ids
        )
    ]
    if not unfunded:
        return _decisions(
            ids,
            configured=configured,
            required={},
            currency=enforcement_currency,
            covered={
                key: tuple(sorted(values, key=str))
                for key, values in covered_by_account.items()
            },
            non_billable={
                key: tuple(sorted(values, key=str))
                for key, values in non_billable_by_account.items()
            },
            unresolved={
                key: tuple(sorted(values, key=str))
                for key, values in unresolved_by_account.items()
            },
        )

    from app.services.prepaid_service_renewals import (
        resolve_prepaid_monthly_charges,
    )

    charges = resolve_prepaid_monthly_charges(db, unfunded, effective_now)

    # 6. Sum the exact taxed contract charge of every due, uncovered service.
    required: dict[str, Decimal] = defaultdict(lambda: ZERO)
    actionable_by_account: dict[str, list[UUID]] = defaultdict(list)
    unresolved_renewal_by_account: dict[str, list[UUID]] = defaultdict(list)
    for subscription in unfunded:
        charge = charges[subscription.id]
        if charge is None:
            unresolved_renewal_by_account[str(subscription.subscriber_id)].append(
                subscription.id
            )
            continue
        amount, amount_currency, _cycle = charge
        amount_currency = str(amount_currency or "").strip().upper()
        if amount_currency != enforcement_currency:
            raise PrepaidCurrencyMismatchError(
                code="financial.prepaid_threshold.currency_mismatch",
                message=(
                    "A prepaid subscription price currency does not match the "
                    "enforcement currency."
                ),
                details={
                    "subscription_id": str(subscription.id),
                    "price_currency": amount_currency or None,
                    "enforcement_currency": enforcement_currency,
                },
            )
        actionable_by_account[str(subscription.subscriber_id)].append(subscription.id)
        if amount > ZERO:
            required[str(subscription.subscriber_id)] += amount

    return _decisions(
        ids,
        configured=configured,
        required=required,
        currency=enforcement_currency,
        covered={
            key: tuple(sorted(values, key=str))
            for key, values in covered_by_account.items()
        },
        non_billable={
            key: tuple(sorted(values, key=str))
            for key, values in non_billable_by_account.items()
        },
        actionable={
            key: tuple(sorted(values, key=str))
            for key, values in actionable_by_account.items()
        },
        unresolved={
            key: tuple(sorted(values, key=str))
            for key, values in unresolved_by_account.items()
        },
        unresolved_renewal={
            key: tuple(sorted(values, key=str))
            for key, values in unresolved_renewal_by_account.items()
        },
    )


def resolve_prepaid_thresholds(
    db: Session,
    account_ids: Sequence[UUID | str],
    *,
    now: datetime | None = None,
    currency: str | None = None,
) -> dict[str, Decimal]:
    """Return the scalar threshold projection for batch compatibility."""

    return {
        account_id: decision.threshold
        for account_id, decision in resolve_prepaid_threshold_decisions(
            db,
            account_ids,
            now=now,
            currency=currency,
        ).items()
    }


def resolve_prepaid_threshold_decision(
    db: Session,
    account: Subscriber,
    *,
    now: datetime | None = None,
    currency: str | None = None,
) -> PrepaidThresholdDecision:
    """Resolve the typed threshold outcome for one canonical account."""

    return resolve_prepaid_threshold_decisions(
        db,
        [account.id],
        now=now,
        currency=currency,
    )[str(account.id)]


def resolve_prepaid_threshold(
    db: Session,
    account: Subscriber,
    *,
    now: datetime | None = None,
    currency: str | None = None,
) -> Decimal:
    """Threshold for one account — a thin adapter over the batch owner.

    Deliberately delegates rather than reimplementing: one set of rules, so the
    enforcement sweep and any batch consumer cannot disagree.
    """
    return resolve_prepaid_threshold_decision(
        db,
        account,
        now=now,
        currency=currency,
    ).threshold
