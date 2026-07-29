"""Behavior coverage for the `billing.obligations` owner (ADR 0007 Phase 1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.billing import TaxRate
from app.models.billing_contract import (
    AccountingTreatment,
    BillingContractLine,
    BillingContractSourceKind,
    BillingObligation,
    BillingRecordAuthority,
    CadenceAlignment,
    ChargeComponent,
    CollectionTiming,
    IntervalUnit,
    ObligationResolutionKind,
    ObligationState,
    ProrationPolicy,
    RateBasis,
)
from app.services.billing.cadence import BillingCadence, Interval
from app.services.billing.contracts import (
    BillingContracts,
    ContractLineInput,
    RecordContractVersionCommand,
)
from app.services.billing.obligations import (
    BillingObligationError,
    BillingObligations,
    ScheduleObligationCommand,
)
from app.services.owner_commands import CommandContext

LAGOS = "Africa/Lagos"
START = datetime(2026, 3, 1, tzinfo=UTC)


def _utc(value):
    """SQLite hands persisted instants back naive; restore UTC for asserts."""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _context(key: str | None = None) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="billing-obligation:schedule",
        reason="pytest billing obligation",
        idempotency_key=key or f"pytest:{command_id}",
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


@pytest.fixture()
def contract_version(db_session, subscriber, subscription):
    """One effective shadow contract version with a single recurring line.

    Identity is captured before the commit: committing expires every ORM
    instance, and refreshing one would leave the session inside a caller
    transaction that ``execute_owner_command`` refuses to inherit.
    """

    account_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    result = BillingContracts.record_version(
        db_session,
        RecordContractVersionCommand(
            account_id=account_id,
            subscription_id=subscription_id,
            source_kind=BillingContractSourceKind.sales_order_line,
            source_id=uuid4(),
            starts_at=START,
            contracted_price=Decimal("25000.00"),
            currency="NGN",
            cadence=_cadence(),
            lines=(
                ContractLineInput(
                    charge_component=ChargeComponent.recurring_service,
                    description="Standard Internet",
                    unit_price=Decimal("25000.00"),
                    currency="NGN",
                    accounting_treatment=AccountingTreatment.receivable,
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
    return result.version_id, line_key, account_id


def _schedule(db_session, contract_version, *, index=0, key=None, covered=None):
    version_id, line_key, _ = contract_version
    return BillingObligations.schedule(
        db_session,
        ScheduleObligationCommand(
            contract_version_id=version_id,
            contract_line_key=line_key,
            period_index=index,
            covered=covered,
        ),
        context=_context(key),
    )


def test_scheduling_creates_one_shadow_obligation_for_an_exact_period(
    db_session, contract_version
):
    result = _schedule(db_session, contract_version)

    assert result.replayed is False
    assert result.state is ObligationState.scheduled
    assert result.authority is BillingRecordAuthority.shadow
    assert result.gross_amount == Decimal("25000.00")

    obligation = db_session.get(BillingObligation, result.obligation_id)
    assert _utc(obligation.period_start) == START
    assert _utc(obligation.period_end).astimezone(UTC).month == 4
    assert obligation.accounting_treatment is AccountingTreatment.receivable
    assert obligation.collection_timing is CollectionTiming.advance
    assert obligation.rating_provenance_complete is True
    assert obligation.rating_policy_version == "billing-rating-v1"
    assert _utc(obligation.rating_coverage_start) == START
    assert _utc(obligation.rating_coverage_end) == _utc(obligation.period_end)
    assert obligation.rating_unit_price == Decimal("25000.0000")
    assert obligation.rating_timezone_name == LAGOS
    assert obligation.rating_input_fingerprint is not None
    assert len(obligation.rating_input_fingerprint) == 64
    assert result.rating_input_fingerprint == obligation.rating_input_fingerprint


def test_replaying_the_same_natural_identity_returns_one_obligation(
    db_session, contract_version
):
    first = _schedule(db_session, contract_version)
    db_session.commit()
    # A different idempotency key, same natural identity: still one charge.
    second = _schedule(db_session, contract_version, key="pytest:other-key")

    assert second.replayed is True
    assert second.obligation_id == first.obligation_id
    rows = db_session.execute(select(BillingObligation)).scalars().all()
    assert len(rows) == 1


def test_replay_uses_recorded_tax_provenance_after_tax_configuration_changes(
    db_session,
    contract_version,
):
    version_id, line_key, _ = contract_version
    tax_rate = TaxRate(
        name="VAT replay",
        code="VAT-REPLAY",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    line = db_session.execute(
        select(BillingContractLine).where(
            BillingContractLine.contract_version_id == version_id,
            BillingContractLine.contract_line_key == line_key,
        )
    ).scalar_one()
    line.tax_treatment_code = "VAT-REPLAY"
    db_session.commit()

    first = _schedule(db_session, contract_version)
    assert first.gross_amount == Decimal("26875.00")
    db_session.commit()

    tax_rate.rate = Decimal("10.0000")
    line.unit_price = Decimal("30000.00")
    db_session.commit()
    replay = _schedule(
        db_session,
        contract_version,
        key="pytest:tax-policy-changed",
    )

    assert replay.replayed is True
    assert replay.obligation_id == first.obligation_id
    assert replay.gross_amount == Decimal("26875.00")
    obligation = db_session.get(BillingObligation, first.obligation_id)
    assert obligation.rating_tax_rate_id == tax_rate.id
    assert obligation.rating_tax_rate_percent == Decimal("7.5000")


def test_same_natural_identity_with_different_coverage_fails_closed(
    db_session,
    contract_version,
):
    partial = Interval(
        starts_at=START,
        ends_at=datetime(2026, 3, 16, tzinfo=UTC),
    )
    first = _schedule(db_session, contract_version, covered=partial)
    db_session.commit()

    with pytest.raises(BillingObligationError) as excinfo:
        _schedule(
            db_session,
            contract_version,
            key="pytest:different-coverage",
        )

    assert first.replayed is False
    assert excinfo.value.code == "billing.obligations.rating_provenance_conflict"


def test_corrupt_recorded_rating_fingerprint_fails_replay(
    db_session,
    contract_version,
):
    scheduled = _schedule(db_session, contract_version)
    db_session.commit()
    obligation = db_session.get(BillingObligation, scheduled.obligation_id)
    obligation.rating_input_fingerprint = "0" * 64
    db_session.commit()

    with pytest.raises(BillingObligationError) as excinfo:
        _schedule(
            db_session,
            contract_version,
            key="pytest:corrupt-provenance",
        )

    assert (
        excinfo.value.code == "billing.obligations.recorded_rating_provenance_invalid"
    )


def test_consecutive_periods_do_not_gap_or_overlap(db_session, contract_version):
    first = _schedule(db_session, contract_version, index=0)
    db_session.commit()
    second = _schedule(db_session, contract_version, index=1)

    a = db_session.get(BillingObligation, first.obligation_id)
    b = db_session.get(BillingObligation, second.obligation_id)

    assert a.period_end == b.period_start


def test_opening_is_idempotent(db_session, contract_version):
    scheduled = _schedule(db_session, contract_version)
    db_session.commit()

    first = BillingObligations.open(
        db_session, obligation_id=scheduled.obligation_id, context=_context()
    )
    db_session.commit()
    second = BillingObligations.open(
        db_session, obligation_id=scheduled.obligation_id, context=_context()
    )

    assert first is ObligationState.open
    assert second is ObligationState.open


def test_partial_then_full_settlement_resolves_the_obligation(
    db_session, contract_version
):
    scheduled = _schedule(db_session, contract_version)
    db_session.commit()
    BillingObligations.open(
        db_session, obligation_id=scheduled.obligation_id, context=_context()
    )
    db_session.commit()

    partial = BillingObligations.resolve(
        db_session,
        obligation_id=scheduled.obligation_id,
        kind=ObligationResolutionKind.settlement,
        amount=Decimal("10000.00"),
        context=_context(),
    )
    db_session.commit()
    full = BillingObligations.resolve(
        db_session,
        obligation_id=scheduled.obligation_id,
        kind=ObligationResolutionKind.settlement,
        amount=Decimal("15000.00"),
        context=_context(),
    )

    assert partial is ObligationState.partially_resolved
    assert full is ObligationState.resolved
    obligation = db_session.get(BillingObligation, scheduled.obligation_id)
    assert obligation.resolved_amount == Decimal("25000.00")
    assert obligation.resolved_at is not None


def test_applications_cannot_exceed_the_obligation(db_session, contract_version):
    scheduled = _schedule(db_session, contract_version)
    db_session.commit()
    BillingObligations.open(
        db_session, obligation_id=scheduled.obligation_id, context=_context()
    )
    db_session.commit()

    with pytest.raises(BillingObligationError) as excinfo:
        BillingObligations.resolve(
            db_session,
            obligation_id=scheduled.obligation_id,
            kind=ObligationResolutionKind.settlement,
            amount=Decimal("25000.01"),
            context=_context(),
        )

    assert excinfo.value.code == "billing.obligations.resolution_exceeds_obligation"


def test_non_cash_resolution_is_typed_not_a_faked_payment(db_session, contract_version):
    scheduled = _schedule(db_session, contract_version)
    db_session.commit()
    BillingObligations.open(
        db_session, obligation_id=scheduled.obligation_id, context=_context()
    )
    db_session.commit()

    state = BillingObligations.resolve(
        db_session,
        obligation_id=scheduled.obligation_id,
        kind=ObligationResolutionKind.write_off,
        amount=Decimal("25000.00"),
        context=_context(),
    )

    obligation = db_session.get(BillingObligation, scheduled.obligation_id)
    assert state is ObligationState.written_off
    assert obligation.resolution_kind is ObligationResolutionKind.write_off


def test_a_scheduled_obligation_cannot_be_resolved_before_it_opens(
    db_session, contract_version
):
    scheduled = _schedule(db_session, contract_version)
    db_session.commit()

    with pytest.raises(BillingObligationError) as excinfo:
        BillingObligations.resolve(
            db_session,
            obligation_id=scheduled.obligation_id,
            kind=ObligationResolutionKind.settlement,
            amount=Decimal("100.00"),
            context=_context(),
        )

    assert excinfo.value.code == "billing.obligations.invalid_obligation_transition"


def test_open_obligations_are_scoped_to_one_account_and_currency(
    db_session, contract_version
):
    account_id = contract_version[2]
    scheduled = _schedule(db_session, contract_version)
    db_session.commit()
    BillingObligations.open(
        db_session, obligation_id=scheduled.obligation_id, context=_context()
    )
    db_session.commit()

    matching = BillingObligations.open_obligations_for_account(
        db_session, account_id=account_id, currency="NGN"
    )
    other_currency = BillingObligations.open_obligations_for_account(
        db_session, account_id=account_id, currency="USD"
    )

    assert [item.id for item in matching] == [scheduled.obligation_id]
    assert other_currency == []


def test_scheduling_requires_an_existing_contract_version(db_session, contract_version):
    _, line_key, _ = contract_version
    db_session.commit()

    with pytest.raises(BillingObligationError) as excinfo:
        BillingObligations.schedule(
            db_session,
            ScheduleObligationCommand(
                contract_version_id=uuid4(),
                contract_line_key=line_key,
                period_index=0,
            ),
            context=_context(),
        )

    assert excinfo.value.code == "billing.obligations.contract_version_not_found"


def test_tax_is_carried_separately_into_the_gross_amount(db_session, contract_version):
    from app.models.billing import TaxRate

    version_id, line_key, _ = contract_version
    db_session.add(
        TaxRate(
            name="VAT 7.5%",
            code="VAT-NG",
            rate=Decimal("7.5000"),
            is_active=True,
        )
    )
    line = db_session.execute(
        select(BillingContractLine).where(
            BillingContractLine.contract_version_id == version_id,
            BillingContractLine.contract_line_key == line_key,
        )
    ).scalar_one()
    line.tax_treatment_code = "VAT-NG"
    db_session.commit()

    result = BillingObligations.schedule(
        db_session,
        ScheduleObligationCommand(
            contract_version_id=version_id,
            contract_line_key=line_key,
            period_index=0,
        ),
        context=_context(),
    )

    obligation = db_session.get(BillingObligation, result.obligation_id)
    assert obligation.net_amount == Decimal("25000.00")
    assert obligation.tax_amount == Decimal("1875.00")
    assert obligation.gross_amount == Decimal("26875.00")
