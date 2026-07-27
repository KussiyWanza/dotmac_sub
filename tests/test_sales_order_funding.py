"""Behavior coverage for `sales.order_funding` (ADR 0007 Phase 6)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.event_store import EventStore
from app.models.sales_order_funding import FundingGateState
from app.services.owner_commands import CommandContext
from app.services.sales_order_funding import (
    OrderFundingError,
    SalesOrderFunding,
)

RESOLVED_AT = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)


def _context() -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="order-funding:test",
        reason="pytest order funding",
        idempotency_key=f"pytest:{command_id}",
    )


@pytest.fixture()
def funding_setup(db_session, subscriber, subscription):
    """A real order plus two shadow obligations to fund it with.

    The funding-obligation rows carry a foreign key to billing_obligations,
    which the harness enforces, so the obligations are created through the
    real owners rather than invented ids.
    """

    from decimal import Decimal

    from sqlalchemy import select as sa_select

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
    from app.models.sales import SalesOrder
    from app.services.billing.cadence import BillingCadence
    from app.services.billing.contracts import (
        BillingContracts,
        ContractLineInput,
        RecordContractVersionCommand,
    )
    from app.services.billing.obligations import (
        BillingObligations,
        ScheduleObligationCommand,
    )

    account_id, subscription_id = subscriber.id, subscription.id
    order = SalesOrder(subscriber_id=account_id)
    db_session.add(order)
    db_session.commit()
    order_id = order.id
    db_session.commit()

    version = BillingContracts.record_version(
        db_session,
        RecordContractVersionCommand(
            account_id=account_id,
            subscription_id=subscription_id,
            source_kind=BillingContractSourceKind.sales_order_line,
            source_id=uuid4(),
            starts_at=datetime(2026, 3, 1, tzinfo=UTC),
            contracted_price=Decimal("25000.00"),
            currency="NGN",
            cadence=BillingCadence(
                rate_basis=RateBasis.fixed_per_service_period,
                rate_unit=IntervalUnit.month,
                rate_quantity=Decimal("1"),
                service_interval_unit=IntervalUnit.month,
                service_interval_count=1,
                invoice_interval_unit=IntervalUnit.month,
                invoice_interval_count=1,
                collection_timing=CollectionTiming.advance,
                alignment=CadenceAlignment.contract_anniversary,
                timezone_name="Africa/Lagos",
                proration_policy=ProrationPolicy.none,
            ),
            lines=(
                ContractLineInput(
                    charge_component=ChargeComponent.installation,
                    description="Installation",
                    unit_price=Decimal("25000.00"),
                    currency="NGN",
                    accounting_treatment=AccountingTreatment.receivable,
                    is_finite=True,
                ),
            ),
        ),
        context=_context(),
    )
    db_session.commit()
    line_key = db_session.execute(
        sa_select(BillingContractLine.contract_line_key).where(
            BillingContractLine.contract_version_id == version.version_id
        )
    ).scalar_one()
    db_session.commit()

    obligation_ids = []
    for index in range(2):
        result = BillingObligations.schedule(
            db_session,
            ScheduleObligationCommand(
                contract_version_id=version.version_id,
                contract_line_key=line_key,
                period_index=index,
                net_amount=Decimal("12500.00"),
            ),
            context=_context(),
        )
        db_session.commit()
        obligation_ids.append(result.obligation_id)

    return order_id, tuple(obligation_ids)


def _register(db, sales_order_id, obligation_ids):
    return SalesOrderFunding.register_finite_obligations(
        db,
        sales_order_id=sales_order_id,
        obligation_ids=obligation_ids,
        context=_context(),
    )


def _resolve(db, sales_order_id, obligation_id):
    return SalesOrderFunding.record_obligation_resolution(
        db,
        sales_order_id=sales_order_id,
        obligation_id=obligation_id,
        resolution_kind="settlement",
        resolved_event_id=uuid4(),
        resolved_at=RESOLVED_AT,
        context=_context(),
    )


def test_partial_funding_never_advances_the_gate(db_session, funding_setup):
    sales_order_id, (first, second) = funding_setup
    _register(db_session, sales_order_id, (first, second))

    status = _resolve(db_session, sales_order_id, first)

    assert status.state is FundingGateState.pending
    assert status.resolved_obligations == 1
    assert status.total_obligations == 2
    assert status.funded_event_id is None


def test_full_finite_funding_advances_exactly_once(db_session, funding_setup):
    sales_order_id, (first, second) = funding_setup
    _register(db_session, sales_order_id, (first, second))
    _resolve(db_session, sales_order_id, first)

    funded = _resolve(db_session, sales_order_id, second)
    # Replaying the last resolution must not fund twice or re-emit.
    replayed = _resolve(db_session, sales_order_id, second)

    assert funded.state is FundingGateState.funded
    assert funded.funded_event_id is not None
    assert replayed.funded_event_id == funded.funded_event_id

    event = db_session.execute(
        select(EventStore).where(EventStore.event_id == funded.funded_event_id)
    ).scalar_one()
    assert event.payload["output"] == "sales.order_funding.completed"
    assert event.payload["obligation_count"] == 2


def test_an_unregistered_obligation_cannot_touch_the_gate(db_session, funding_setup):
    """A future recurring obligation cannot reopen or inflate the order."""

    sales_order_id, (registered, recurring) = funding_setup
    _register(db_session, sales_order_id, (registered,))

    with pytest.raises(OrderFundingError) as excinfo:
        _resolve(db_session, sales_order_id, recurring)

    assert excinfo.value.code == "sales.order_funding.obligation_not_in_finite_set"


def test_a_funded_gate_refuses_finite_set_changes(db_session, funding_setup):
    sales_order_id, (only, later) = funding_setup
    _register(db_session, sales_order_id, (only,))
    _resolve(db_session, sales_order_id, only)

    with pytest.raises(OrderFundingError) as excinfo:
        _register(db_session, sales_order_id, (later,))

    assert excinfo.value.code == "sales.order_funding.gate_already_funded"


def test_registration_is_idempotent_per_obligation(db_session, funding_setup):
    sales_order_id, (obligation, _) = funding_setup
    first = _register(db_session, sales_order_id, (obligation,))
    second = _register(db_session, sales_order_id, (obligation,))

    assert first.total_obligations == 1
    assert second.total_obligations == 1


def test_an_empty_finite_set_fails_closed(db_session, funding_setup):
    sales_order_id, _ = funding_setup
    with pytest.raises(OrderFundingError) as excinfo:
        _register(db_session, sales_order_id, ())

    assert excinfo.value.code == "sales.order_funding.empty_finite_obligation_set"
