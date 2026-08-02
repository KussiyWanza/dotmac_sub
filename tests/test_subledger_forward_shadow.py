"""ADR 0007 Phase 3 forward-shadow: one posting group per money transition.

Slice-1 exit gates: every owner-clean prepaid money transition stages exactly
one idempotent shadow posting group inside the owner's transaction; replays
duplicate nothing; reversals link the original group. No read authority moves.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.customer_subledger import (
    CustomerPositionEffect,
    CustomerPostingGroup,
    PositionEffectKind,
    PostingCommandKind,
)
from tests.test_account_credit_deposits import (
    _intent,
    _provider,
    _settle,
    _transaction,
)


def _groups(db, producer: str) -> list[CustomerPostingGroup]:
    return (
        db.query(CustomerPostingGroup)
        .filter(CustomerPostingGroup.producer_owner == producer)
        .order_by(CustomerPostingGroup.recorded_at)
        .all()
    )


def test_deposit_settlement_stages_one_shadow_group(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="fwd-shadow-dep-1"),
    )

    groups = _groups(db_session, "financial.account_credit_deposits")
    assert len(groups) == 1
    group = groups[0]
    assert group.command_kind is PostingCommandKind.customer_credit_deposit
    assert group.authority.value == "shadow"
    assert group.source_kind == "payment"
    assert group.source_id == result.payment.id
    effects = (
        db_session.query(CustomerPositionEffect)
        .filter(CustomerPositionEffect.group_id == group.id)
        .all()
    )
    assert len(effects) == 1
    assert effects[0].effect is PositionEffectKind.customer_credit_created
    assert Decimal(str(effects[0].amount)) == Decimal("10000.00")
    assert effects[0].payment_id == result.payment.id


def test_deposit_replay_duplicates_no_group(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    tx = _transaction(intent, external_id="fwd-shadow-dep-2")
    first = _settle(db_session, intent_id=intent.id, transaction=tx)
    db_session.commit()
    replay = _settle(db_session, intent_id=intent.id, transaction=tx)

    assert replay.payment.id == first.payment.id
    groups = _groups(db_session, "financial.account_credit_deposits")
    assert len(groups) == 1


def test_posting_failure_rolls_back_the_whole_deposit(
    db_session, subscriber, monkeypatch
):
    # Atomicity gate: if staging the posting group fails, the deposit's
    # owner command aborts and NO payment or group survives.
    import app.services.account_credit_deposits as deposits_module
    from app.models.billing import Payment

    def _boom(*args, **kwargs):
        raise RuntimeError("posting unavailable")

    monkeypatch.setattr(
        "app.services.billing.customer_subledger.stage_posting_group", _boom
    )
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    before_payments = db_session.query(Payment).count()

    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="posting unavailable"):
        _settle(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, external_id="fwd-shadow-atomic-1"),
        )
    db_session.rollback()
    assert db_session.query(Payment).count() == before_payments
    assert _groups(db_session, "financial.account_credit_deposits") == []
    db_session.refresh(intent)
    # The intent itself rolled back: no settlement linkage survived.
    assert "settlement_payment_id" not in (intent.metadata_ or {})
    assert deposits_module is not None


def test_downstream_failure_leaves_no_orphan_posting(
    db_session, subscriber, monkeypatch
):
    # Atomicity gate: a failure AFTER staging (credit application) aborts
    # the same transaction; the staged group must not survive alone.
    def _boom(db, account_id, *args, **kwargs):
        raise RuntimeError("application unavailable")

    monkeypatch.setattr(
        "app.services.account_credit_deposits.AccountCreditApplications.apply",
        _boom,
    )
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")

    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="application unavailable"):
        _settle(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, external_id="fwd-shadow-atomic-2"),
        )
    db_session.rollback()
    assert _groups(db_session, "financial.account_credit_deposits") == []
    db_session.refresh(intent)
    assert "settlement_payment_id" not in (intent.metadata_ or {})


def _host_command(db, operation, key):
    from uuid import uuid4

    from app.services.owner_commands import (
        CommandContext,
        OwnerCommandDefinition,
        execute_owner_command,
    )

    command_id = uuid4()
    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(
            owner="billing.contracts",
            concern="versioned billing contract terms",
            name="record_billing_contract_version",
        ),
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="user:pytest",
            scope="forward-shadow:test",
            reason="pytest forward shadow webhook seam",
            idempotency_key=key,
        ),
        operation=operation,
    )


def _provider_payment(db, subscriber, amount="100.00"):
    from uuid import uuid4

    from app.models.billing import PaymentProvider, PaymentProviderType
    from tests.test_payment_refund_evidence import _payment

    provider = PaymentProvider(
        name=f"Provider {uuid4().hex}", provider_type=PaymentProviderType.custom
    )
    db.add(provider)
    db.commit()
    return provider, _payment(db, subscriber, provider=provider)


def test_provider_refund_stages_one_group_at_the_deciding_owner_root(
    db_session, subscriber
):
    from decimal import Decimal as D
    from uuid import uuid4

    from app.models.billing import PaymentRefund
    from app.schemas.billing import PaymentProviderEventIngest
    from app.services import billing as billing_service
    from app.services.owner_commands import CommandContext as _Ctx
    from app.services.payment_provider_events import WEBHOOK_PARTICIPANT_SCOPE
    from tests.payment_provider_event_helpers import provider_event_command

    def stage_verified_provider_event(db, payload):
        return billing_service.payment_provider_events.stage_verified_webhook_event(
            db,
            provider_event_command(payload),
            context=_Ctx.system(
                actor="pytest:payment-provider-event",
                scope=WEBHOOK_PARTICIPANT_SCOPE,
                reason="forward-shadow webhook seam test",
                idempotency_key=payload.idempotency_key or payload.external_id,
            ),
        )

    provider, payment = _provider_payment(db_session, subscriber)

    db_session.commit()
    _host_command(
        db_session,
        lambda: stage_verified_provider_event(
            db_session,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                payment_id=payment.id,
                event_type="charge.refunded",
                amount=D("40.00"),
                currency="NGN",
                idempotency_key=f"provider-refund-{uuid4().hex}",
            ),
        ),
        key=f"fwd-refund-{uuid4().hex}",
    )

    refund = db_session.query(PaymentRefund).one()
    groups = _groups(db_session, "financial.payment_provider_events")
    assert len(groups) == 1
    group = groups[0]
    assert group.command_kind is PostingCommandKind.refund
    assert group.source_kind == "payment_refund"
    assert group.source_id == refund.id
    assert group.reverses_group_id is None
    effects = (
        db_session.query(CustomerPositionEffect)
        .filter(CustomerPositionEffect.group_id == group.id)
        .all()
    )
    assert {e.effect for e in effects} >= {PositionEffectKind.credit_refunded}


def test_provider_reversal_links_the_original_settlement_group(db_session, subscriber):
    from decimal import Decimal as D
    from uuid import uuid4

    from app.schemas.billing import PaymentProviderEventIngest
    from app.services import billing as billing_service
    from app.services.owner_commands import CommandContext as _Ctx
    from app.services.payment_provider_events import WEBHOOK_PARTICIPANT_SCOPE
    from tests.payment_provider_event_helpers import provider_event_command

    def stage_verified_provider_event(db, payload):
        return billing_service.payment_provider_events.stage_verified_webhook_event(
            db,
            provider_event_command(payload),
            context=_Ctx.system(
                actor="pytest:payment-provider-event",
                scope=WEBHOOK_PARTICIPANT_SCOPE,
                reason="forward-shadow webhook seam test",
                idempotency_key=payload.idempotency_key or payload.external_id,
            ),
        )

    # Original money transition staged by F1 (deposit settlement group).
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="fwd-shadow-rev-orig"),
    )
    db_session.commit()
    original = _groups(db_session, "financial.account_credit_deposits")[0]
    payment_id = original.source_id

    db_session.commit()
    _host_command(
        db_session,
        lambda: stage_verified_provider_event(
            db_session,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                payment_id=payment_id,
                event_type="charge.reversed",
                amount=D("10000.00"),
                currency="NGN",
                idempotency_key=f"provider-reversal-{uuid4().hex}",
            ),
        ),
        key=f"fwd-reversal-{uuid4().hex}",
    )

    reversal_groups = [
        g
        for g in _groups(db_session, "financial.payment_provider_events")
        if g.command_kind is PostingCommandKind.reversal
    ]
    assert len(reversal_groups) == 1
    assert reversal_groups[0].reverses_group_id == original.id
    assert reversal_groups[0].source_kind == "payment_reversal"


def test_unwrapped_refund_path_stages_nothing(db_session, subscriber):
    from decimal import Decimal as D
    from uuid import uuid4

    from app.schemas.billing import PaymentProviderEventIngest
    from tests.payment_provider_event_helpers import stage_verified_provider_event

    provider, payment = _provider_payment(db_session, subscriber)

    # Legacy root: no owner command. The money transition proceeds; the
    # posting gap belongs to the verifier, never to invented history.
    stage_verified_provider_event(
        db_session,
        PaymentProviderEventIngest(
            provider_id=provider.id,
            payment_id=payment.id,
            event_type="charge.refunded",
            amount=D("40.00"),
            currency="NGN",
            idempotency_key=f"provider-refund-{uuid4().hex}",
        ),
    )

    assert _groups(db_session, "financial.payment_provider_events") == []


def test_deposit_that_pays_an_invoice_stages_deposit_and_application_groups(
    db_session, subscriber
):
    from decimal import Decimal as D

    from app.models.billing import Invoice, InvoiceStatus
    from tests.test_account_credit_deposits import _intent as _dep_intent
    from tests.test_account_credit_deposits import _provider as _dep_provider
    from tests.test_account_credit_deposits import _settle as _dep_settle
    from tests.test_account_credit_deposits import _transaction as _dep_tx

    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=D("4000.00"),
        total=D("4000.00"),
        balance_due=D("4000.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    provider = _dep_provider(db_session)
    intent = _dep_intent(db_session, subscriber, provider, amount="10000.00")
    _dep_settle(
        db_session,
        intent_id=intent.id,
        transaction=_dep_tx(intent, external_id="fwd-shadow-app-1"),
    )

    deposit_groups = _groups(db_session, "financial.account_credit_deposits")
    application_groups = _groups(db_session, "financial.account_credit_applications")
    assert len(deposit_groups) == 1
    assert len(application_groups) == 1
    group = application_groups[0]
    assert group.command_kind is PostingCommandKind.customer_credit_application
    assert group.source_kind == "payment_allocation"
    effects = {e.effect for e in group.effects}
    assert effects == {
        PositionEffectKind.customer_credit_consumed,
        PositionEffectKind.receivable_settled,
    }
    amounts = {str(e.amount) for e in group.effects}
    assert amounts == {"4000.0000"} or amounts == {"4000.00"}


def test_phase3_forward_verifier_is_durable_and_owns_every_debt_row(
    db_session, subscriber_account, subscription
):
    from datetime import UTC, datetime, timedelta

    # One prepaid funding candidate; with no authority-cutover batch every
    # candidate is quarantined by predicate — the opening-position debt.
    from decimal import Decimal as D

    from app.models.admin_alert import AdminAlert
    from app.models.billing_shadow_verification import (
        BillingCutoverVerificationRun,
    )
    from app.models.catalog import BillingMode, SubscriptionStatus
    from app.models.subscriber import SubscriberStatus
    from app.services.billing.shadow_verification import (
        RecordPhase3ForwardVerificationCommand,
        record_phase3_forward_run,
    )
    from app.services.owner_commands import CommandContext
    from tests.prepaid_funding_helpers import ensure_test_prepaid_contract

    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.min_balance = D("100.00")
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.status = SubscriberStatus.active
    subscriber_account.is_active = True
    subscriber_account.billing_enabled = True
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    ensure_test_prepaid_contract(db_session, subscription)
    # Deliberately NO baseline for this account. The test database carries
    # a seeded authority-cutover batch, so a LEGACY account (created before
    # the cutover position) without an active baseline is the quarantined
    # opening-position debt.
    from app.services.prepaid_funding_reconstruction import (
        authority_cutover_batch,
    )

    cutover = authority_cutover_batch(db_session)
    assert cutover is not None
    subscriber_account.created_at = cutover.position_at - timedelta(days=30)
    db_session.commit()

    now = datetime.now(UTC)
    command = RecordPhase3ForwardVerificationCommand(
        cutoff_at=now,
        observation_started_at=now - timedelta(days=1),
        observation_ended_at=now,
        code_version="pytest",
        database_schema_version="pytest",
    )

    def _ctx(key: str) -> CommandContext:
        return CommandContext.system(
            actor="pytest:phase3-forward",
            scope="billing-shadow-verification:test",
            reason="forward-shadow verifier test",
            idempotency_key=key,
        )

    db_session.commit()
    result = record_phase3_forward_run(
        db_session, command, context=_ctx("phase3-fwd-1")
    )
    db_session.commit()

    assert result.replayed is False
    assert result.cohort_count >= 1
    assert result.opening_position_debt_count == result.cohort_count
    run = db_session.get(BillingCutoverVerificationRun, result.run_id)
    assert run.phase == "phase_3_forward"
    assert run.event_outcomes["postings_manufactured"] is False
    assert len(result.source_fingerprint) == 64
    assert len(result.result_fingerprint) == 64

    # blocked \\ owned = ∅: every debt account carries an open owned item.
    debt_accounts = set(run.cohort_classification["_details"]["opening_position_debt"])
    owned = {
        alert.details.get("account_id")
        for alert in db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint.like("prepaid-funding:opening-debt:%"))
        .all()
        if alert.status.value == "open"
    }
    assert debt_accounts - owned == set()

    # Exact replay returns the recorded run.
    db_session.commit()
    replay = record_phase3_forward_run(
        db_session, command, context=_ctx("phase3-fwd-1")
    )
    assert replay.replayed is True
    assert replay.run_id == result.run_id
