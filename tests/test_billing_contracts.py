"""Behavior coverage for the `billing.contracts` owner (ADR 0007 Phase 1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.billing_contract import (
    AccountingTreatment,
    BillingContract,
    BillingContractSourceKind,
    BillingContractVersion,
    BillingContractVersionStatus,
    BillingRecordAuthority,
    CadenceAlignment,
    ChargeComponent,
    CollectionTiming,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
)
from app.services.billing.cadence import BillingCadence
from app.services.billing.contracts import (
    BillingContractError,
    BillingContracts,
    ContractLineInput,
    RecordContractVersionCommand,
)
from app.services.owner_commands import CommandContext

LAGOS = "Africa/Lagos"


def _utc(value):
    """SQLite hands persisted instants back naive; restore UTC for asserts."""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _context(key: str | None = None) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="billing-contract:record",
        reason="pytest billing contract",
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


def _command(account_id, subscription_id, **overrides) -> RecordContractVersionCommand:
    fields = {
        "account_id": account_id,
        "subscription_id": subscription_id,
        "source_kind": BillingContractSourceKind.sales_order_line,
        "source_id": uuid4(),
        "starts_at": datetime(2026, 3, 1, tzinfo=UTC),
        "contracted_price": Decimal("25000.00"),
        "currency": "NGN",
        "cadence": _cadence(),
        "lines": (
            ContractLineInput(
                charge_component=ChargeComponent.recurring_service,
                description="Standard Internet",
                unit_price=Decimal("25000.00"),
                currency="NGN",
                accounting_treatment=AccountingTreatment.receivable,
            ),
        ),
    }
    fields.update(overrides)
    return RecordContractVersionCommand(**fields)


@pytest.fixture()
def ids(db_session, subscriber, subscription):
    """Account and subscription identity captured before the first commit.

    ``commit`` expires every ORM instance, so touching ``subscriber.id``
    afterwards issues a refresh SELECT and leaves the session inside a caller
    transaction, which ``execute_owner_command`` correctly refuses to inherit.
    """

    captured = (subscriber.id, subscription.id)
    db_session.commit()
    return captured


def test_recording_terms_creates_a_shadow_contract_version(db_session, ids):
    account_id, subscription_id = ids

    result = BillingContracts.record_version(
        db_session, _command(account_id, subscription_id), context=_context()
    )

    assert result.replayed is False
    assert result.version == 1
    # Phase 1 is expand-and-shadow: nothing may read these rows as money.
    assert result.authority is BillingRecordAuthority.shadow

    contract = db_session.get(BillingContract, result.contract_id)
    assert contract.subscription_id == subscription_id
    assert contract.account_id == account_id

    version = db_session.get(BillingContractVersion, result.version_id)
    assert version.status is BillingContractVersionStatus.effective
    assert version.ends_at is None
    assert version.currency == "NGN"
    assert len(result.line_ids) == 1


def test_replay_of_the_same_idempotency_key_writes_one_version(db_session, ids):
    account_id, subscription_id = ids
    context = _context("pytest:stable-key")

    first = BillingContracts.record_version(
        db_session, _command(account_id, subscription_id), context=context
    )
    db_session.commit()
    second = BillingContracts.record_version(
        db_session,
        _command(account_id, subscription_id),
        context=_context("pytest:stable-key"),
    )

    assert second.replayed is True
    assert second.version_id == first.version_id
    versions = (
        db_session.execute(
            select(BillingContractVersion).where(
                BillingContractVersion.contract_id == first.contract_id
            )
        )
        .scalars()
        .all()
    )
    assert len(versions) == 1


def test_supersession_closes_the_previous_version_contiguously(db_session, ids):
    account_id, subscription_id = ids
    first = BillingContracts.record_version(
        db_session, _command(account_id, subscription_id), context=_context()
    )
    db_session.commit()

    change_at = datetime(2026, 6, 1, tzinfo=UTC)
    second = BillingContracts.record_version(
        db_session,
        _command(
            account_id,
            subscription_id,
            starts_at=change_at,
            contracted_price=Decimal("30000.00"),
            source_kind=BillingContractSourceKind.plan_change,
            lines=(
                ContractLineInput(
                    charge_component=ChargeComponent.recurring_service,
                    description="Standard Internet",
                    unit_price=Decimal("30000.00"),
                    currency="NGN",
                    accounting_treatment=AccountingTreatment.receivable,
                ),
            ),
        ),
        context=_context(),
    )

    previous = db_session.get(BillingContractVersion, first.version_id)
    current = db_session.get(BillingContractVersion, second.version_id)

    assert previous.status is BillingContractVersionStatus.superseded
    # Half-open and contiguous: no gap, no overlap, no rewritten history.
    assert _utc(previous.ends_at) == change_at
    assert _utc(current.starts_at) == change_at
    assert current.supersedes_id == previous.id
    assert current.version == 2
    assert previous.contracted_price == Decimal("25000.00")


def test_line_lineage_survives_supersession(db_session, ids):
    """An obligation keeps one lineage key when terms change."""

    account_id, subscription_id = ids
    first = BillingContracts.record_version(
        db_session, _command(account_id, subscription_id), context=_context()
    )
    db_session.commit()
    second = BillingContracts.record_version(
        db_session,
        _command(
            account_id,
            subscription_id,
            starts_at=datetime(2026, 6, 1, tzinfo=UTC),
            source_kind=BillingContractSourceKind.plan_change,
        ),
        context=_context(),
    )

    from app.models.billing_contract import BillingContractLine

    keys = {
        version_id: db_session.execute(
            select(BillingContractLine.contract_line_key).where(
                BillingContractLine.contract_version_id == version_id
            )
        ).scalar_one()
        for version_id in (first.version_id, second.version_id)
    }

    assert keys[first.version_id] == keys[second.version_id]


def test_effective_version_resolves_one_row_across_the_boundary(db_session, ids):
    account_id, subscription_id = ids
    BillingContracts.record_version(
        db_session, _command(account_id, subscription_id), context=_context()
    )
    db_session.commit()
    change_at = datetime(2026, 6, 1, tzinfo=UTC)
    BillingContracts.record_version(
        db_session,
        _command(
            account_id,
            subscription_id,
            starts_at=change_at,
            contracted_price=Decimal("30000.00"),
            source_kind=BillingContractSourceKind.plan_change,
        ),
        context=_context(),
    )

    before = BillingContracts.effective_version_at(
        db_session,
        subscription_id=subscription_id,
        moment=datetime(2026, 5, 31, tzinfo=UTC),
    )
    at_boundary = BillingContracts.effective_version_at(
        db_session, subscription_id=subscription_id, moment=change_at
    )

    assert before.contracted_price == Decimal("25000.00")
    # Half-open: the boundary instant belongs to the new version.
    assert at_boundary.contracted_price == Decimal("30000.00")


def test_cadence_round_trips_through_the_stored_version(db_session, ids):
    account_id, subscription_id = ids
    cadence = _cadence(
        service_interval_unit=IntervalUnit.month,
        service_interval_count=3,
        rate_unit=IntervalUnit.day,
        proration_policy=ProrationPolicy.actual_calendar_days,
    )
    result = BillingContracts.record_version(
        db_session,
        _command(account_id, subscription_id, cadence=cadence),
        context=_context(),
    )

    version = db_session.get(BillingContractVersion, result.version_id)
    restored = BillingContracts.cadence_of(version)

    assert restored.service_interval_count == 3
    assert restored.rate_unit is IntervalUnit.day
    assert restored.proration_policy is ProrationPolicy.actual_calendar_days
    assert restored.timezone_name == LAGOS


def test_mixed_currency_between_contract_and_line_is_refused(db_session, ids):
    account_id, subscription_id = ids

    with pytest.raises(BillingContractError) as excinfo:
        BillingContracts.record_version(
            db_session,
            _command(
                account_id,
                subscription_id,
                lines=(
                    ContractLineInput(
                        charge_component=ChargeComponent.recurring_service,
                        description="Standard Internet",
                        unit_price=Decimal("25000.00"),
                        currency="USD",
                        accounting_treatment=AccountingTreatment.receivable,
                    ),
                ),
            ),
            context=_context(),
        )

    assert excinfo.value.code == "billing.contracts.mixed_currency_contract"


def test_a_version_cannot_start_before_the_current_effective_one(db_session, ids):
    account_id, subscription_id = ids
    BillingContracts.record_version(
        db_session, _command(account_id, subscription_id), context=_context()
    )
    db_session.commit()

    with pytest.raises(BillingContractError) as excinfo:
        BillingContracts.record_version(
            db_session,
            _command(
                account_id,
                subscription_id,
                starts_at=datetime(2026, 1, 1, tzinfo=UTC),
                source_kind=BillingContractSourceKind.staff_correction,
            ),
            context=_context(),
        )

    assert excinfo.value.code == "billing.contracts.out_of_order_contract_version"


def test_duplicate_charge_component_on_one_version_is_refused(db_session, ids):
    account_id, subscription_id = ids
    line = ContractLineInput(
        charge_component=ChargeComponent.addon,
        description="Static IP",
        unit_price=Decimal("2000.00"),
        currency="NGN",
        accounting_treatment=AccountingTreatment.receivable,
    )

    with pytest.raises(BillingContractError) as excinfo:
        BillingContracts.record_version(
            db_session,
            _command(account_id, subscription_id, lines=(line, line)),
            context=_context(),
        )

    assert excinfo.value.code == "billing.contracts.duplicate_contract_line"


def test_recording_terms_requires_an_idempotency_key(db_session, ids):
    account_id, subscription_id = ids
    command_id = uuid4()
    context = CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="billing-contract:record",
        reason="pytest missing key",
    )

    with pytest.raises(BillingContractError) as excinfo:
        BillingContracts.record_version(
            db_session, _command(account_id, subscription_id), context=context
        )

    assert excinfo.value.code == "billing.contracts.missing_idempotency_key"


def test_owner_command_rejects_a_caller_owned_transaction(db_session, ids):
    account_id, subscription_id = ids
    # Leave a pending read so the session is inside a caller transaction.
    db_session.execute(select(BillingContract)).all()

    with pytest.raises(Exception) as excinfo:
        BillingContracts.record_version(
            db_session, _command(account_id, subscription_id), context=_context()
        )

    assert getattr(excinfo.value, "code", "") == (
        "billing.contracts.active_caller_transaction"
    )
