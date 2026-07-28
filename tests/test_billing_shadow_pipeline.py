"""Phase 1 billing shadow delivery and durable cutover-evidence behavior."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing_contract import (
    AccountingTreatment,
    BillingContractSourceKind,
    BillingRecordAuthority,
    CadenceAlignment,
    ChargeComponent,
    CollectionTiming,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
)
from app.models.billing_shadow_verification import BillingCutoverVerificationRun
from app.models.catalog import BillingCycle, BillingMode, SubscriptionStatus
from app.models.event_store import EventStore
from app.services.billing.cadence import BillingCadence
from app.services.billing.contracts import (
    BillingContracts,
    ContractLineInput,
    RecordContractVersionCommand,
)
from app.services.billing.shadow_verification import (
    BillingShadowVerification,
    BillingShadowVerificationError,
    RecordPhase1VerificationCommand,
)
from app.services.owner_commands import CommandContext


def _context(scope: str, *, key: str | None = None) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:billing-migration-test",
        scope=scope,
        reason="pytest billing shadow verification",
        idempotency_key=key or f"pytest:{command_id}",
    )


def _record_matching_contract(
    db_session,
    *,
    account_id,
    subscription_id,
    source_id,
    starts_at,
    unit_price,
) -> None:
    result = BillingContracts.record_version(
        db_session,
        RecordContractVersionCommand(
            account_id=account_id,
            subscription_id=subscription_id,
            source_kind=BillingContractSourceKind.sales_order_line,
            source_id=source_id,
            starts_at=starts_at,
            contracted_price=unit_price,
            currency="NGN",
            cadence=BillingCadence(
                rate_basis=RateBasis.fixed_per_service_period,
                rate_unit=IntervalUnit.month,
                rate_quantity=Decimal("1"),
                service_interval_unit=IntervalUnit.month,
                service_interval_count=1,
                invoice_interval_unit=IntervalUnit.month,
                invoice_interval_count=1,
                collection_timing=CollectionTiming.arrears,
                alignment=CadenceAlignment.contract_anniversary,
                timezone_name="Africa/Lagos",
                proration_policy=ProrationPolicy.none,
            ),
            lines=(
                ContractLineInput(
                    charge_component=ChargeComponent.recurring_service,
                    description="Matched shadow service",
                    unit_price=unit_price,
                    currency="NGN",
                    accounting_treatment=AccountingTreatment.receivable,
                ),
            ),
        ),
        context=_context("billing-contract"),
    )
    assert result.authority is BillingRecordAuthority.shadow


def _run_command(now: datetime) -> RecordPhase1VerificationCommand:
    return RecordPhase1VerificationCommand(
        cutoff_at=now,
        observation_started_at=now - timedelta(hours=1),
        observation_ended_at=now,
        code_version="test-code",
        database_schema_version="436_billing_shadow_verification_evidence",
    )


def test_complete_phase1_cohort_can_receive_separate_approvals(
    db_session,
    subscriber,
    subscription,
) -> None:
    starts_at = datetime(2026, 7, 1, tzinfo=UTC)
    account_id, subscription_id = subscriber.id, subscription.id
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.postpaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.unit_price = Decimal("25000.00")
    subscription.start_at = starts_at
    db_session.commit()

    _record_matching_contract(
        db_session,
        account_id=account_id,
        subscription_id=subscription_id,
        source_id=uuid4(),
        starts_at=starts_at,
        unit_price=Decimal("25000.00"),
    )
    db_session.commit()

    now = datetime.now(UTC)
    result = BillingShadowVerification.record_phase1_run(
        db_session,
        _run_command(now),
        context=_context("phase1-run", key="pytest:phase1-complete"),
    )
    assert result.cohort_count == 1
    assert result.covered_count == 1
    assert result.blocker_count == 0
    db_session.commit()

    BillingShadowVerification.approve_operator(
        db_session,
        run_id=result.run_id,
        context=_context("phase1-operator-approval"),
        approved_at=now,
    )
    db_session.commit()
    BillingShadowVerification.approve_operator(
        db_session,
        run_id=result.run_id,
        context=_context("phase1-operator-approval-replay"),
        approved_at=now,
    )
    operator_events = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "billing.cutover_verification.approved")
        .count()
    )
    assert operator_events == 1
    db_session.commit()
    BillingShadowVerification.approve_finance(
        db_session,
        run_id=result.run_id,
        context=_context("phase1-finance-approval"),
        approved_at=now,
    )

    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert run.approved is True
    assert run.source_fingerprint != run.result_fingerprint
    approval_events = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "billing.cutover_verification.approved")
        .count()
    )
    assert approval_events == 2
    db_session.commit()

    with pytest.raises(BillingShadowVerificationError) as raised:
        BillingShadowVerification.approve_operator(
            db_session,
            run_id=result.run_id,
            context=_context("phase1-operator-approval-conflict"),
            approved_at=now + timedelta(seconds=1),
        )

    assert raised.value.code == "billing.shadow_verification.approval_already_recorded"


def test_unlinked_active_subscription_blocks_approval(
    db_session,
    subscription,
) -> None:
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.postpaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.unit_price = Decimal("25000.00")
    db_session.commit()

    now = datetime.now(UTC)
    result = BillingShadowVerification.record_phase1_run(
        db_session,
        _run_command(now),
        context=_context("phase1-run", key="pytest:phase1-blocked"),
    )
    assert result.blocker_count == 1
    db_session.commit()

    with pytest.raises(BillingShadowVerificationError) as raised:
        BillingShadowVerification.approve_operator(
            db_session,
            run_id=result.run_id,
            context=_context("phase1-operator-approval"),
            approved_at=now,
        )

    assert (
        raised.value.code == "billing.shadow_verification.verification_blockers_present"
    )


def test_phase1_run_uses_contract_version_effective_at_cutoff(
    db_session,
    subscriber,
    subscription,
) -> None:
    cutoff = datetime.now(UTC)
    current_start = cutoff - timedelta(days=180)
    future_start = cutoff + timedelta(days=35)
    account_id, subscription_id = subscriber.id, subscription.id
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.postpaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.unit_price = Decimal("25000.00")
    subscription.start_at = current_start
    db_session.commit()

    _record_matching_contract(
        db_session,
        account_id=account_id,
        subscription_id=subscription_id,
        source_id=uuid4(),
        starts_at=current_start,
        unit_price=Decimal("25000.00"),
    )
    db_session.commit()
    _record_matching_contract(
        db_session,
        account_id=account_id,
        subscription_id=subscription_id,
        source_id=uuid4(),
        starts_at=future_start,
        unit_price=Decimal("30000.00"),
    )
    db_session.commit()

    result = BillingShadowVerification.record_phase1_run(
        db_session,
        _run_command(cutoff),
        context=_context("phase1-cutoff", key="pytest:phase1-cutoff-version"),
    )

    assert result.cohort_count == 1
    assert result.covered_count == 1
    assert result.blocker_count == 0


def test_phase1_run_rejects_idempotency_key_reuse_for_another_identity(
    db_session,
    subscription,
) -> None:
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    command = _run_command(datetime.now(UTC))
    context = _context("phase1-run", key="pytest:phase1-identity")

    BillingShadowVerification.record_phase1_run(
        db_session,
        command,
        context=context,
    )
    db_session.commit()

    with pytest.raises(BillingShadowVerificationError) as raised:
        BillingShadowVerification.record_phase1_run(
            db_session,
            replace(command, code_version="different-code"),
            context=_context("phase1-run", key="pytest:phase1-identity"),
        )

    assert raised.value.code == "billing.shadow_verification.idempotency_conflict"
