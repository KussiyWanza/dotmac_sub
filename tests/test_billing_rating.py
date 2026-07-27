"""Behavior coverage for the `billing.rating` resolver (ADR 0007 Phase 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.billing import TaxRate
from app.models.billing_contract import (
    AccountingTreatment,
    BillingContractLine,
    BillingContractSourceKind,
    CadenceAlignment,
    ChargeComponent,
    CollectionTiming,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
)
from app.services.billing.cadence import BillingCadence, Interval, service_period
from app.services.billing.contracts import (
    BillingContracts,
    ContractLineInput,
    RecordContractVersionCommand,
)
from app.services.billing.rating import (
    BillingRatingError,
    rate_line_period,
)
from app.services.owner_commands import CommandContext

LAGOS = "Africa/Lagos"
START = datetime(2026, 3, 1, tzinfo=UTC)


def _context() -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="billing-rating:test",
        reason="pytest billing rating",
        idempotency_key=f"pytest:{command_id}",
    )


def _cadence(**overrides) -> BillingCadence:
    base = {
        "rate_basis": RateBasis.fixed_per_service_period,
        "rate_unit": IntervalUnit.month,
        "rate_quantity": Decimal("1"),
        "service_interval_unit": IntervalUnit.month,
        "service_interval_count": 1,
        "invoice_interval_unit": IntervalUnit.month,
        "invoice_interval_count": 1,
        "collection_timing": CollectionTiming.advance,
        "alignment": CadenceAlignment.contract_anniversary,
        "timezone_name": LAGOS,
        "proration_policy": ProrationPolicy.none,
    }
    base.update(overrides)
    return BillingCadence(**base)


def _record(
    db_session,
    account_id,
    subscription_id,
    *,
    cadence=None,
    unit_price=Decimal("25000.00"),
    tax_treatment_code=None,
    tax_inclusive=False,
):
    result = BillingContracts.record_version(
        db_session,
        RecordContractVersionCommand(
            account_id=account_id,
            subscription_id=subscription_id,
            source_kind=BillingContractSourceKind.sales_order_line,
            source_id=uuid4(),
            starts_at=START,
            contracted_price=unit_price,
            currency="NGN",
            cadence=cadence or _cadence(),
            tax_treatment_code=tax_treatment_code,
            tax_inclusive=tax_inclusive,
            lines=(
                ContractLineInput(
                    charge_component=ChargeComponent.recurring_service,
                    description="Standard Internet",
                    unit_price=unit_price,
                    currency="NGN",
                    accounting_treatment=AccountingTreatment.receivable,
                    tax_treatment_code=tax_treatment_code,
                ),
            ),
        ),
        context=_context(),
    )
    db_session.commit()
    line_key = db_session.execute(
        select(BillingContractLine.contract_line_key).where(
            BillingContractLine.contract_version_id == result.version_id
        )
    ).scalar_one()
    db_session.commit()
    return result.version_id, line_key


@pytest.fixture()
def ids(db_session, subscriber, subscription):
    captured = (subscriber.id, subscription.id)
    db_session.commit()
    return captured


def _period(version_id, db_session, index=0):
    from app.models.billing_contract import BillingContractVersion

    version = db_session.get(BillingContractVersion, version_id)
    cadence = BillingContracts.cadence_of(version)
    # SQLite hands persisted instants back naive; restore UTC.
    starts_at = version.starts_at
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    return service_period(cadence=cadence, contract_start=starts_at, index=index)


def test_fixed_period_rating_is_the_contracted_line_amount(db_session, ids):
    account_id, subscription_id = ids
    version_id, line_key = _record(db_session, account_id, subscription_id)
    period = _period(version_id, db_session)

    rated = rate_line_period(
        db_session,
        contract_version_id=version_id,
        contract_line_key=line_key,
        period=period,
    )

    assert rated.net_amount == Decimal("25000.00")
    assert rated.tax_amount == Decimal("0.00")
    assert rated.gross_amount == Decimal("25000.00")
    assert rated.currency == "NGN"
    assert rated.proration == Decimal("1")


def test_rating_is_deterministic(db_session, ids):
    account_id, subscription_id = ids
    version_id, line_key = _record(db_session, account_id, subscription_id)
    period = _period(version_id, db_session)

    first = rate_line_period(
        db_session,
        contract_version_id=version_id,
        contract_line_key=line_key,
        period=period,
    )
    second = rate_line_period(
        db_session,
        contract_version_id=version_id,
        contract_line_key=line_key,
        period=period,
    )

    assert first == second


def test_per_day_rate_aggregates_into_a_monthly_period(db_session, ids):
    """Rate unit independent of the invoice interval (ADR 0007 section 4)."""

    account_id, subscription_id = ids
    version_id, line_key = _record(
        db_session,
        account_id,
        subscription_id,
        cadence=_cadence(
            rate_basis=RateBasis.per_rate_unit, rate_unit=IntervalUnit.day
        ),
        unit_price=Decimal("500.00"),
    )
    period = _period(version_id, db_session)  # March: 31 days

    rated = rate_line_period(
        db_session,
        contract_version_id=version_id,
        contract_line_key=line_key,
        period=period,
    )

    assert rated.rate_units == Decimal(31)
    assert rated.net_amount == Decimal("15500.00")


def test_tax_is_added_from_the_named_active_rate(db_session, ids):
    account_id, subscription_id = ids
    db_session.add(
        TaxRate(name="VAT", code="VAT-NG", rate=Decimal("0.0750"), is_active=True)
    )
    db_session.commit()
    version_id, line_key = _record(
        db_session, account_id, subscription_id, tax_treatment_code="VAT-NG"
    )
    period = _period(version_id, db_session)

    rated = rate_line_period(
        db_session,
        contract_version_id=version_id,
        contract_line_key=line_key,
        period=period,
    )

    assert rated.tax_rate == Decimal("0.0750")
    assert rated.net_amount == Decimal("25000.00")
    assert rated.tax_amount == Decimal("1875.00")
    assert rated.gross_amount == Decimal("26875.00")


def test_tax_inclusive_price_backs_the_net_out(db_session, ids):
    account_id, subscription_id = ids
    db_session.add(
        TaxRate(name="VAT", code="VAT-NG", rate=Decimal("0.0750"), is_active=True)
    )
    db_session.commit()
    version_id, line_key = _record(
        db_session,
        account_id,
        subscription_id,
        tax_treatment_code="VAT-NG",
        tax_inclusive=True,
        unit_price=Decimal("26875.00"),
    )
    period = _period(version_id, db_session)

    rated = rate_line_period(
        db_session,
        contract_version_id=version_id,
        contract_line_key=line_key,
        period=period,
    )

    assert rated.gross_amount == Decimal("26875.00")
    assert rated.net_amount == Decimal("25000.00")
    assert rated.tax_amount == Decimal("1875.00")


def test_a_named_tax_code_with_no_active_rate_fails_closed(db_session, ids):
    account_id, subscription_id = ids
    version_id, line_key = _record(
        db_session, account_id, subscription_id, tax_treatment_code="VAT-MISSING"
    )
    period = _period(version_id, db_session)

    with pytest.raises(BillingRatingError) as excinfo:
        rate_line_period(
            db_session,
            contract_version_id=version_id,
            contract_line_key=line_key,
            period=period,
        )

    assert excinfo.value.code == "billing.rating.unknown_tax_treatment"


def test_declared_calendar_day_proration_narrows_the_charge(db_session, ids):
    account_id, subscription_id = ids
    version_id, line_key = _record(
        db_session,
        account_id,
        subscription_id,
        cadence=_cadence(proration_policy=ProrationPolicy.actual_calendar_days),
        unit_price=Decimal("31000.00"),
    )
    period = _period(version_id, db_session)  # March: 31 days
    # Cover only the last 10 local calendar days.
    covered = Interval(
        starts_at=period.ends_at - timedelta(days=10), ends_at=period.ends_at
    )

    rated = rate_line_period(
        db_session,
        contract_version_id=version_id,
        contract_line_key=line_key,
        period=period,
        covered=covered,
    )

    assert rated.proration == Decimal(10) / Decimal(31)
    assert rated.net_amount == Decimal("10000.00")


def test_usage_metered_rating_without_observation_fails_closed(db_session, ids):
    account_id, subscription_id = ids
    version_id, line_key = _record(
        db_session,
        account_id,
        subscription_id,
        cadence=_cadence(rate_basis=RateBasis.usage_metered),
    )
    period = _period(version_id, db_session)

    with pytest.raises(BillingRatingError) as excinfo:
        rate_line_period(
            db_session,
            contract_version_id=version_id,
            contract_line_key=line_key,
            period=period,
        )

    assert excinfo.value.code == "billing.rating.usage_rating_requires_observation"


def test_rating_an_unknown_line_fails_closed(db_session, ids):
    account_id, subscription_id = ids
    version_id, _ = _record(db_session, account_id, subscription_id)
    period = _period(version_id, db_session)

    with pytest.raises(BillingRatingError) as excinfo:
        rate_line_period(
            db_session,
            contract_version_id=version_id,
            contract_line_key=uuid4(),
            period=period,
        )

    assert excinfo.value.code == "billing.rating.contract_line_not_found"
