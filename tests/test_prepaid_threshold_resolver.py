"""The prepaid enforcement threshold has one owner and one set of rules.

`app.services.prepaid_threshold` owns the decision. `service_status._prepaid_threshold`
is a thin adapter over it. These tests hold that boundary: the scalar and batch
paths must agree on every fixture, and the batch cost must be bounded by the
number of batches rather than the number of accounts.

The query budget is the point. Resolving the threshold per account cost ~6
queries each, which made a full prepaid enforcement audit ~16,361 statements —
too heavy to run against production at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    TaxRate,
)
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services.prepaid_service_renewals import resolve_prepaid_monthly_charge
from app.services.prepaid_threshold import (
    PrepaidCurrencyMismatchError,
    resolve_prepaid_threshold,
    resolve_prepaid_threshold_decision,
    resolve_prepaid_thresholds,
)
from app.services.service_status import _prepaid_threshold

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
_USE_OFFER_PRICE = object()


class QueryCounter:
    """Count SQL statements issued while resolving."""

    def __init__(self, db):
        self.bind = db.get_bind()
        self.count = 0

    def __enter__(self):
        event.listen(self.bind, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *exc):
        event.remove(self.bind, "before_cursor_execute", self._hit)

    def _hit(self, *_args, **_kwargs):
        self.count += 1


def _offer(db, price: str | None, name: str = "Prepaid Plan") -> CatalogOffer:
    offer = CatalogOffer(
        name=name,
        billing_cycle="monthly",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    if price is not None:
        db.add(
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal(price),
                currency="NGN",
                billing_cycle="monthly",
                is_active=True,
            )
        )
        db.commit()
    return offer


def _account(db, *, min_balance: str | None = None) -> Subscriber:
    sub = Subscriber(
        first_name="T",
        last_name="User",
        email=f"t{datetime.now(UTC).timestamp()}@example.com",
        status="active",
        is_active=True,
        billing_mode=BillingMode.prepaid,
        min_balance=Decimal(min_balance) if min_balance is not None else None,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _subscription(
    db,
    account: Subscriber,
    offer: CatalogOffer,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
    billing_mode: BillingMode = BillingMode.prepaid,
    unit_price: str | None | object = _USE_OFFER_PRICE,
    discount: bool = False,
    discount_value: str | None = None,
    discount_type: str | None = None,
    next_billing_at: datetime | None = None,
) -> Subscription:
    kwargs = {}
    if discount:
        kwargs.update(
            discount=True,
            discount_value=Decimal(discount_value),
            discount_type=discount_type,
        )
    contracted_price = unit_price
    if unit_price is _USE_OFFER_PRICE:
        price = (
            db.query(OfferPrice)
            .filter(
                OfferPrice.offer_id == offer.id,
                OfferPrice.price_type == PriceType.recurring,
                OfferPrice.is_active.is_(True),
            )
            .order_by(OfferPrice.created_at.desc(), OfferPrice.id.desc())
            .first()
        )
        contracted_price = price.amount if price is not None else None
    sub = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=status,
        billing_mode=billing_mode,
        unit_price=(
            Decimal(str(contracted_price)) if contracted_price is not None else None
        ),
        next_billing_at=next_billing_at,
        **kwargs,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _fund_with_entitlement(db, account, subscription, *, days: int = 30) -> None:
    db.add(
        ServiceEntitlement(
            account_id=account.id,
            subscription_id=subscription.id,
            status=ServiceEntitlementStatus.active,
            starts_at=NOW - timedelta(days=1),
            ends_at=NOW + timedelta(days=days),
            amount_funded=Decimal("1000.00"),
        )
    )
    db.commit()


def _paid_invoice_without_entitlement(db, account, subscription) -> None:
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-{subscription.id}",
        status=InvoiceStatus.paid,
        total=Decimal("1000.00"),
        balance_due=Decimal("0.00"),
        currency="NGN",
        billing_period_start=NOW - timedelta(days=1),
        billing_period_end=NOW + timedelta(days=29),
    )
    db.add(invoice)
    db.commit()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="prepaid period",
            quantity=Decimal("1"),
            unit_price=Decimal("1000.00"),
            amount=Decimal("1000.00"),
            is_active=True,
        )
    )
    db.commit()


def _both(db, account) -> tuple[Decimal, Decimal]:
    """Resolve via the scalar adapter and the batch owner."""
    scalar = _prepaid_threshold(db, account, now=NOW)
    batch = resolve_prepaid_thresholds(db, [account.id], now=NOW)[str(account.id)]
    return scalar, batch


# --- equivalence ------------------------------------------------------------


def test_account_override_beats_the_default(db_session):
    account = _account(db_session, min_balance="5000.00")
    scalar, batch = _both(db_session, account)
    assert scalar == batch == Decimal("0.00")


def test_default_setting_used_when_no_override(db_session):
    account = _account(db_session)  # min_balance = None
    scalar, batch = _both(db_session, account)
    assert scalar == batch


def test_paid_entitlement_means_no_renewal_requirement(db_session):
    account = _account(db_session, min_balance="1000.00")
    offer = _offer(db_session, "17500.00")
    subscription = _subscription(db_session, account, offer)
    _fund_with_entitlement(db_session, account, subscription)

    scalar, batch = _both(db_session, account)
    # Current service is already paid; reserve is advisory until renewal is due.
    assert scalar == batch == Decimal("0.00")


def test_paid_invoice_without_entitlement_blocks_enforcement_until_reconciled(
    db_session,
):
    account = _account(db_session, min_balance="1000.00")
    offer = _offer(db_session, "17500.00")
    subscription = _subscription(db_session, account, offer)
    _paid_invoice_without_entitlement(db_session, account, subscription)

    decision = resolve_prepaid_threshold_decision(db_session, account, now=NOW)

    assert decision.threshold == Decimal("0.00")
    assert decision.covered_subscription_ids == ()
    assert decision.actionable_uncovered_subscription_ids == ()
    assert decision.unresolved_projection_subscription_ids == (subscription.id,)


def test_future_anchor_without_coverage_is_unresolved_not_funded(db_session):
    account = _account(db_session, min_balance="1000.00")
    offer = _offer(db_session, "17500.00")
    subscription = _subscription(
        db_session,
        account,
        offer,
        next_billing_at=NOW + timedelta(days=20),
    )

    decision = resolve_prepaid_threshold_decision(db_session, account, now=NOW)

    assert decision.threshold == Decimal("0.00")
    assert decision.actionable_uncovered_subscription_ids == ()
    assert decision.unresolved_projection_subscription_ids == (subscription.id,)


def test_unfunded_single_subscription_requires_its_price(db_session):
    account = _account(db_session, min_balance="1000.00")
    offer = _offer(db_session, "17500.00")
    _subscription(db_session, account, offer)

    scalar, batch = _both(db_session, account)
    assert scalar == batch == Decimal("17500.00")


def test_threshold_decision_exposes_minimum_and_renewal_provenance(db_session):
    account = _account(db_session, min_balance="1000.00")
    offer = _offer(db_session, "17500.00")
    _subscription(db_session, account, offer)

    decision = resolve_prepaid_threshold_decision(db_session, account, now=NOW)

    assert decision.account_id == str(account.id)
    assert decision.configured_minimum == Decimal("1000.00")
    assert decision.unfunded_renewal_requirement == Decimal("17500.00")
    assert decision.threshold == Decimal("17500.00")
    assert decision.currency == "NGN"
    assert decision.actionable_uncovered_subscription_ids


def test_missing_unfunded_subscription_price_fails_closed(db_session):
    account = _account(db_session, min_balance="0.00")
    offer = _offer(db_session, None)
    subscription = _subscription(db_session, account, offer, unit_price=None)

    decision = resolve_prepaid_threshold_decision(db_session, account, now=NOW)

    assert decision.threshold == Decimal("0.00")
    assert decision.actionable_uncovered_subscription_ids == ()
    assert decision.unresolved_renewal_subscription_ids == (subscription.id,)


def test_price_currency_must_match_configured_enforcement_currency(db_session):
    account = _account(db_session, min_balance="0.00")
    offer = _offer(db_session, "100.00")
    price = db_session.query(OfferPrice).filter(OfferPrice.offer_id == offer.id).one()
    price.currency = "USD"
    _subscription(db_session, account, offer)
    db_session.commit()

    with pytest.raises(PrepaidCurrencyMismatchError, match="does not match"):
        resolve_prepaid_threshold(db_session, account, now=NOW)


def test_unfunded_multiple_subscriptions_sum(db_session):
    account = _account(db_session, min_balance="1000.00")
    _subscription(db_session, account, _offer(db_session, "17500.00", "A"))
    _subscription(db_session, account, _offer(db_session, "2500.00", "B"))

    scalar, batch = _both(db_session, account)
    assert scalar == batch == Decimal("20000.00")


def test_percentage_discount_lowers_the_effective_price(db_session):
    account = _account(db_session, min_balance="0.00")
    offer = _offer(db_session, "20000.00")
    _subscription(
        db_session,
        account,
        offer,
        discount=True,
        discount_value="10",
        discount_type="percentage",
    )

    scalar, batch = _both(db_session, account)
    assert scalar == batch == Decimal("18000.00")


def test_threshold_uses_the_same_taxed_contract_charge_as_renewal(db_session):
    account = _account(db_session, min_balance="0.00")
    tax_rate = TaxRate(
        name="VAT 7.5%",
        code="VAT75",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    account.tax_rate_id = tax_rate.id
    offer = _offer(db_session, "17500.00")
    subscription = _subscription(db_session, account, offer)
    db_session.commit()

    renewal = resolve_prepaid_monthly_charge(db_session, subscription, NOW)
    decision = resolve_prepaid_threshold_decision(db_session, account, now=NOW)

    assert renewal is not None
    assert renewal[0] == Decimal("18812.50")
    assert decision.unfunded_renewal_requirement == renewal[0]
    assert decision.threshold == Decimal("18812.50")


def test_unit_price_override_beats_the_catalog_price(db_session):
    account = _account(db_session, min_balance="0.00")
    offer = _offer(db_session, "20000.00")
    _subscription(db_session, account, offer, unit_price="12000.00")

    scalar, batch = _both(db_session, account)
    assert scalar == batch == Decimal("12000.00")


@pytest.mark.parametrize(
    "status", [SubscriptionStatus.canceled, SubscriptionStatus.expired]
)
def test_terminal_subscriptions_are_excluded(db_session, status):
    account = _account(db_session, min_balance="1000.00")
    offer = _offer(db_session, "17500.00")
    _subscription(db_session, account, offer, status=status)

    scalar, batch = _both(db_session, account)
    assert scalar == batch == Decimal("0.00")


def test_postpaid_subscriptions_are_excluded(db_session):
    account = _account(db_session, min_balance="1000.00")
    offer = _offer(db_session, "17500.00")
    _subscription(db_session, account, offer, billing_mode=BillingMode.postpaid)

    scalar, batch = _both(db_session, account)
    assert scalar == batch == Decimal("0.00")


# --- the owner boundary -----------------------------------------------------


def test_scalar_delegates_to_the_batch_owner(db_session):
    """One rule, not two. The adapter must not re-derive anything."""
    accounts = []
    for i in range(5):
        account = _account(db_session, min_balance=str(1000 * (i + 1)))
        _subscription(db_session, account, _offer(db_session, "17500.00", f"P{i}"))
        accounts.append(account)

    batch = resolve_prepaid_thresholds(db_session, [a.id for a in accounts], now=NOW)
    for account in accounts:
        assert batch[str(account.id)] == _prepaid_threshold(
            db_session, account, now=NOW
        )
        assert batch[str(account.id)] == resolve_prepaid_threshold(
            db_session, account, now=NOW
        )


# --- query budget -----------------------------------------------------------


def test_batch_cost_does_not_scale_with_account_count(db_session):
    """The whole point: query count is bounded by batches, not accounts.

    The per-account resolver cost ~6 queries each. This asserts there is NO
    per-account slope — resolving 20 accounts must not cost meaningfully more
    queries than resolving 4.
    """
    accounts = []
    offer = _offer(db_session, "17500.00")
    for _ in range(20):
        account = _account(db_session, min_balance="1000.00")
        _subscription(db_session, account, offer)
        accounts.append(account)

    small = [a.id for a in accounts[:4]]
    large = [a.id for a in accounts]

    with QueryCounter(db_session) as c_small:
        resolve_prepaid_thresholds(db_session, small, now=NOW)
    with QueryCounter(db_session) as c_large:
        resolve_prepaid_thresholds(db_session, large, now=NOW)

    # 5x the accounts must not mean ~5x the queries. Allow a small constant
    # slack, but nothing proportional to account count.
    assert c_large.count <= c_small.count + 2, (
        f"query count scales with accounts: {c_small.count} for 4 accounts, "
        f"{c_large.count} for 20 — the derivation is not batched"
    )
    # And the absolute cost stays tiny for a single batch.
    # Raised from 15 to 20 by the settings cutover, and deliberately not
    # quietly. Kernel resolution walks a SCOPE CHAIN — the operator tenant's
    # row, then the platform row — where Sub's former resolver did one flat
    # lookup, so each distinct setting read costs one statement more. That is
    # the price of tenant-scoped settings (ADR-0009: the operator IS a tenant),
    # not a regression in this code path.
    #
    # What guards the bug class is the SLOPE assertion above, and it is
    # untouched: `c_large <= c_small + 2` still holds. A budget raised whenever it
    # fails is worthless, so the constant moves only with a named cause and the
    # proportional check stays exactly as strict.
    #
    # It comes back down when settings caching is settled — Redis fronts this
    # in production but is deliberately dead in the unit suite (conftest points
    # REDIS_URL at a refusing port), so these numbers are the uncached cost.
    assert c_large.count < 20
