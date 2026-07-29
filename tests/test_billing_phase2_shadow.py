"""Durable Phase 2 billing rating/obligation shadow evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select

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
    ProrationPolicy,
    RateBasis,
)
from app.models.billing_shadow_verification import BillingCutoverVerificationRun
from app.models.catalog import (
    AddOn,
    AddOnPrice,
    AddOnType,
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing.cadence import BillingCadence, service_period
from app.services.billing.contracts import (
    BillingContracts,
    ContractLineInput,
    RecordContractVersionCommand,
)
from app.services.billing.obligations import (
    BillingObligations,
    ScheduleObligationCommand,
)
from app.services.billing.shadow_verification import (
    BillingShadowVerification,
    RecordPhase2VerificationCommand,
)
from app.services.owner_commands import CommandContext


def _context(scope: str, *, key: str | None = None) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:phase2-shadow-test",
        scope=scope,
        reason="pytest phase2 shadow evidence",
        idempotency_key=key or f"pytest:{command_id}",
    )


def _cadence(
    *,
    cycle: BillingCycle,
    mode: BillingMode,
) -> BillingCadence:
    unit, count = {
        BillingCycle.daily: (IntervalUnit.day, 1),
        BillingCycle.weekly: (IntervalUnit.week, 1),
        BillingCycle.monthly: (IntervalUnit.month, 1),
        BillingCycle.quarterly: (IntervalUnit.month, 3),
        BillingCycle.annual: (IntervalUnit.year, 1),
    }[cycle]
    return BillingCadence(
        rate_basis=RateBasis.fixed_per_service_period,
        rate_unit=unit,
        rate_quantity=Decimal("1"),
        service_interval_unit=unit,
        service_interval_count=count,
        invoice_interval_unit=unit,
        invoice_interval_count=count,
        collection_timing=(
            CollectionTiming.advance
            if mode is BillingMode.prepaid
            else CollectionTiming.arrears
        ),
        alignment=CadenceAlignment.contract_anniversary,
        timezone_name="Africa/Lagos",
        proration_policy=ProrationPolicy.none,
    )


def _prepare_subscription(
    db_session,
    *,
    subscription,
    starts_at: datetime,
    amount: Decimal,
    cycle: BillingCycle,
    mode: BillingMode,
) -> None:
    account = db_session.get(Subscriber, subscription.subscriber_id)
    assert account is not None
    account.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = mode
    subscription.billing_cycle = cycle
    subscription.start_at = starts_at
    subscription.next_billing_at = starts_at
    subscription.unit_price = amount
    db_session.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=amount,
            currency="NGN",
            billing_cycle=cycle,
            is_active=True,
        )
    )
    db_session.commit()


def _record_and_schedule(
    db_session,
    *,
    subscriber_id: UUID,
    subscription_id: UUID,
    starts_at: datetime,
    amount: Decimal,
    cycle: BillingCycle,
    mode: BillingMode,
    period_indexes: tuple[int, ...] = (0,),
    additional_lines: tuple[ContractLineInput, ...] = (),
) -> tuple[UUID, tuple[UUID, ...]]:
    result = BillingContracts.record_version(
        db_session,
        RecordContractVersionCommand(
            account_id=subscriber_id,
            subscription_id=subscription_id,
            source_kind=BillingContractSourceKind.migration_backfill,
            source_id=uuid4(),
            starts_at=starts_at,
            contracted_price=amount,
            currency="NGN",
            cadence=_cadence(cycle=cycle, mode=mode),
            lines=(
                ContractLineInput(
                    charge_component=ChargeComponent.recurring_service,
                    description="Shadow recurring service",
                    unit_price=amount,
                    currency="NGN",
                    accounting_treatment=(
                        AccountingTreatment.prepaid_consumption
                        if mode is BillingMode.prepaid
                        else AccountingTreatment.receivable
                    ),
                ),
                *additional_lines,
            ),
        ),
        context=_context("billing-contract"),
    )
    db_session.commit()
    line_keys = tuple(
        db_session.execute(
            select(BillingContractLine.contract_line_key).where(
                BillingContractLine.contract_version_id == result.version_id
            )
        )
        .scalars()
        .all()
    )
    db_session.commit()
    for period_index in period_indexes:
        for line_key in line_keys:
            scheduled = BillingObligations.schedule(
                db_session,
                ScheduleObligationCommand(
                    contract_version_id=result.version_id,
                    contract_line_key=line_key,
                    period_index=period_index,
                ),
                context=_context("billing-obligation"),
            )
            assert scheduled.authority is BillingRecordAuthority.shadow
            db_session.commit()
    return result.version_id, line_keys


def _add_recurring_addon(
    db_session,
    *,
    subscription_id: UUID,
    starts_at: datetime,
    amount: Decimal,
    quantity: int = 1,
) -> tuple[UUID, UUID]:
    add_on = AddOn(
        name="Static IP",
        addon_type=AddOnType.static_ip,
        is_active=True,
    )
    db_session.add(add_on)
    db_session.flush()
    db_session.add(
        AddOnPrice(
            add_on_id=add_on.id,
            price_type=PriceType.recurring,
            amount=amount,
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    subscription_add_on = SubscriptionAddOn(
        subscription_id=subscription_id,
        add_on_id=add_on.id,
        quantity=quantity,
        start_at=starts_at,
    )
    db_session.add(subscription_add_on)
    db_session.flush()
    subscription_add_on_id = subscription_add_on.id
    add_on_id = add_on.id
    db_session.commit()
    return subscription_add_on_id, add_on_id


def _run_command(cutoff: datetime) -> RecordPhase2VerificationCommand:
    return RecordPhase2VerificationCommand(
        cutoff_at=cutoff,
        observation_started_at=cutoff - timedelta(hours=1),
        observation_ended_at=cutoff,
        code_version="test-phase2-code",
        database_schema_version="439_billing_obligation_rating_provenance",
    )


def test_phase2_exact_postpaid_period_and_amount_parity_is_durable(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    amount = Decimal("25000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-parity"),
    )

    assert result.cohort_count == 1
    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert result.covered_count == 1, run.cohort_classification
    assert result.expected_difference_count == 0
    assert result.blocker_count == 0
    assert run.phase == "phase_2"
    assert run.currency_totals == {
        "NGN": {
            "legacy": "25000.00",
            "target": "25000.00",
            "difference": "0.00",
        }
    }
    assert run.event_outcomes["authority_moved"] is False
    assert run.event_outcomes["repair_requested"] is False


def test_phase2_postpaid_preview_proves_base_and_recurring_addon_parity(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    base_amount = Decimal("25000.00")
    addon_amount = Decimal("2000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=base_amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    subscription_add_on_id, _ = _add_recurring_addon(
        db_session,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=addon_amount,
        quantity=2,
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=base_amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
        additional_lines=(
            ContractLineInput(
                charge_component=ChargeComponent.addon,
                component_key=str(subscription_add_on_id),
                description="Static IP",
                quantity=Decimal("2"),
                unit_price=addon_amount,
                currency="NGN",
                accounting_treatment=AccountingTreatment.receivable,
            ),
        ),
    )

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-postpaid-addon-parity"),
    )

    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert result.covered_count == 1, run.cohort_classification
    assert result.blocker_count == 0
    assert run.currency_totals == {
        "NGN": {
            "legacy": "29000.00",
            "target": "29000.00",
            "difference": "0.00",
        }
    }


def test_phase2_postpaid_addon_requires_structural_target_line_identity(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    amount = Decimal("25000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    subscription_add_on_id, _ = _add_recurring_addon(
        db_session,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=Decimal("2000.00"),
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-postpaid-addon-unlinked"),
    )

    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert run.unexpected_unlinked_count == 1
    assert result.blocker_count == 1
    assert run.cohort_classification["_details"][str(subscription_id)] == [
        f"missing_target_recurring_addon:{subscription_add_on_id}"
    ]


def test_phase2_postpaid_multiple_active_addon_prices_are_not_parity(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    base_amount = Decimal("25000.00")
    addon_amount = Decimal("2000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=base_amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    subscription_add_on_id, add_on_id = _add_recurring_addon(
        db_session,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=addon_amount,
    )
    db_session.add(
        AddOnPrice(
            add_on_id=add_on_id,
            price_type=PriceType.recurring,
            amount=Decimal("3000.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db_session.commit()
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=base_amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
        additional_lines=(
            ContractLineInput(
                charge_component=ChargeComponent.addon,
                component_key=str(subscription_add_on_id),
                description="Static IP",
                unit_price=addon_amount,
                currency="NGN",
                accounting_treatment=AccountingTreatment.receivable,
            ),
        ),
    )

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-postpaid-addon-ambiguous"),
    )

    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert run.unresolved_count == 1
    assert result.blocker_count == 1
    assert run.cohort_classification["_details"][str(subscription_id)] == [
        "postpaid_recurring_addon_multiple_active_prices"
    ]


def test_phase2_exact_prepaid_monthly_parity_uses_the_prepaid_owner_preview(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    amount = Decimal("25000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.prepaid,
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.prepaid,
    )

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-prepaid-parity"),
    )

    assert result.covered_count == 1
    assert result.expected_difference_count == 0
    assert result.blocker_count == 0


def test_phase2_prepaid_recurring_addon_exclusion_remains_a_cutover_blocker(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    base_amount = Decimal("25000.00")
    addon_amount = Decimal("2000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=base_amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.prepaid,
    )
    subscription_add_on_id, _ = _add_recurring_addon(
        db_session,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=addon_amount,
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=base_amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.prepaid,
        additional_lines=(
            ContractLineInput(
                charge_component=ChargeComponent.addon,
                component_key=str(subscription_add_on_id),
                description="Static IP",
                unit_price=addon_amount,
                currency="NGN",
                accounting_treatment=AccountingTreatment.prepaid_consumption,
            ),
        ),
    )

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-prepaid-addon-blocker"),
    )

    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert result.covered_count == 0
    assert run.unresolved_count == 1
    assert result.blocker_count == 1
    assert run.cohort_classification["_details"][str(subscription_id)] == [
        "current_prepaid_owner_excludes_recurring_addon"
    ]
    assert run.event_outcomes["authority_moved"] is False
    assert run.event_outcomes["repair_requested"] is False


def test_phase2_new_prepaid_quarterly_cadence_is_explicit_expected_difference(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    amount = Decimal("72000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.quarterly,
        mode=BillingMode.prepaid,
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.quarterly,
        mode=BillingMode.prepaid,
    )

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-new-cadence"),
    )

    assert result.covered_count == 0
    assert result.expected_difference_count == 1
    assert result.blocker_count == 0
    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert run.cohort_classification["expected_difference"] == [str(subscription_id)]


def test_phase2_missing_target_obligation_blocks_approval(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    amount = Decimal("25000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
        period_indexes=(),
    )

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-unlinked"),
    )

    assert result.blocker_count == 1
    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert run.unexpected_unlinked_count == 1


def test_phase2_incomplete_legacy_rating_provenance_is_unresolved(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    amount = Decimal("25000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    obligation = db_session.execute(select(BillingObligation)).scalar_one()
    obligation.rating_provenance_complete = False
    db_session.commit()

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-incomplete-provenance"),
    )

    assert result.covered_count == 0
    assert result.blocker_count == 1
    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert run.unresolved_count == 1
    assert run.cohort_classification["_details"][str(subscription_id)] == [
        "billing.obligations.incomplete_rating_provenance"
    ]


def test_phase2_detects_a_gap_without_attempting_repair(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    # Keep the third period ahead of the fixture's creation time so it belongs
    # to the active verification cohort regardless of when the test runs.
    contract_start = datetime.now(UTC) - timedelta(days=55)
    cadence = _cadence(cycle=BillingCycle.monthly, mode=BillingMode.postpaid)
    third_period = service_period(
        cadence=cadence,
        contract_start=contract_start,
        index=2,
    )
    cutoff = third_period.starts_at
    amount = Decimal("25000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=contract_start,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
        period_indexes=(0, 2),
    )

    result = BillingShadowVerification.record_phase2_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase2-run", key="pytest:phase2-gap"),
    )

    assert result.blocker_count == 1
    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert run.gap_count == 1
    assert run.event_outcomes["repair_requested"] is False


def test_phase2_run_replay_returns_the_same_evidence(
    db_session,
    subscriber,
    subscription,
) -> None:
    subscriber_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    amount = Decimal("25000.00")
    _prepare_subscription(
        db_session,
        subscription=subscription,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    _record_and_schedule(
        db_session,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        starts_at=cutoff,
        amount=amount,
        cycle=BillingCycle.monthly,
        mode=BillingMode.postpaid,
    )
    command = _run_command(cutoff)

    first = BillingShadowVerification.record_phase2_run(
        db_session,
        command,
        context=_context("phase2-run", key="pytest:phase2-replay"),
    )
    db_session.commit()
    second = BillingShadowVerification.record_phase2_run(
        db_session,
        command,
        context=_context("phase2-run", key="pytest:phase2-replay"),
    )

    assert second.run_id == first.run_id
    assert second.replayed is True
