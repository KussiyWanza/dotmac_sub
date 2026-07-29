"""Real postpaid healing, unambiguous cases only.

`unwall_account` had no Celery task and no beat entry; its only caller was a
one-off script, and the scheduled detector hard-coded `apply=False`. So an
account that owed nothing could stay behind the wall until a human happened to
run a script.

The healing pass now applies for real, but only when a locked recomputation
proves zero overdue receivable. Exact arithmetic stands: a fifty-kobo residue
correctly blocks the automated restore — and becomes an operator exception with
durable evidence instead of silently staying invisible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.models.admin_alert import AdminAlert
from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import BillingMode
from app.models.durable_timer import DurableTimer
from app.models.enforcement_lock import EnforcementReason
from app.models.network_monitoring import AlertStatus
from app.models.owner_output import OwnerOutputReceipt
from app.models.subscriber import SubscriberStatus
from app.services.account_lifecycle import suspend_subscription
from app.services.billing.unwall_paid_accounts import (
    UNWALL_EXCEPTION_PREFIX,
    UNWALL_OWNER,
    UNWALL_TIMER_TRIGGER,
    UnwallDisposition,
    consume_walled_account_healing_due,
    decide_unwall,
    heal_walled_account,
    schedule_walled_account_healing,
)
from app.services.events.handlers.billing_lifecycle_projection import (
    BillingLifecycleProjectionHandler,
)
from app.services.events.types import Event, EventType
from app.services.owner_commands import CommandContext
from app.services.runtime_durable_timers import fire_due_timers

RESIDUE = Decimal("0.50")


def _overdue_invoice(db, account, *, amount: Decimal) -> Invoice:
    now = datetime.now(UTC)
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-HEAL-{uuid4().hex[:8]}",
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=amount,
        tax_total=Decimal("0.00"),
        total=amount,
        balance_due=amount,
        issued_at=now - timedelta(days=40),
        due_at=now - timedelta(days=10),
        is_proforma=False,
        is_active=True,
    )
    db.add(invoice)
    db.commit()
    return invoice


def _wall(db, subscription) -> None:
    # Postpaid: this is the cohort the scheduled detector never healed.
    subscription.billing_mode = BillingMode.postpaid
    db.flush()
    suspend_subscription(
        db,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="dunning_case:test",
        emit=False,
    )
    db.commit()


def _exception_alert(db, account_id) -> AdminAlert | None:
    return (
        db.query(AdminAlert)
        .filter(AdminAlert.fingerprint == f"{UNWALL_EXCEPTION_PREFIX}{account_id}")
        .one_or_none()
    )


def _context(*, actor: str, scope: str, reason: str) -> CommandContext:
    return CommandContext.system(
        actor=actor,
        scope=scope,
        reason=reason,
        idempotency_key=f"pytest:{actor}:{scope}:{uuid4()}",
    )


def _schedule_and_fire(db, account_id):
    now = datetime.now(UTC)
    scheduled = schedule_walled_account_healing(
        db,
        account_id=account_id,
        due_at=now,
        context=_context(
            actor="pytest:payment-event",
            scope=str(account_id),
            reason="settled payment",
        ),
    )
    fired = fire_due_timers(
        db,
        now=now + timedelta(minutes=1),
        context=_context(
            actor="pytest:timer-runtime",
            scope="runtime.durable_timers",
            reason="fire due timers",
        ),
    )
    assert len(fired) == 1
    assert fired[0].timer_id == scheduled.timer_id
    return scheduled, fired[0]


def test_decision_recomputes_the_exact_overdue_receivable(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)

    decision = decide_unwall(db_session, str(subscriber.id))

    assert decision.walled is True
    assert decision.overdue_receivable_total == RESIDUE
    assert len(decision.overdue_receivable_invoice_ids) == 1
    assert decision.unambiguous is False


def test_scheduled_healing_restores_an_account_that_owes_nothing(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)

    result = heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="pytest",
        reason="pytest healing",
    )

    assert result.disposition is UnwallDisposition.restored
    assert result.restored is True
    db_session.refresh(subscriber)
    assert subscriber.status is SubscriberStatus.active
    assert _exception_alert(db_session, subscriber.id) is None


def test_a_fifty_kobo_residue_blocks_scheduled_healing(
    db_session, subscriber, subscription
):
    """The money rule: no tolerance, epsilon, or de-minimis threshold."""
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)

    result = heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="service:walled_account_healing",
        reason="scheduled healing",
    )

    assert result.disposition is UnwallDisposition.blocked_overdue_receivable
    assert result.restored is False
    db_session.refresh(subscriber)
    assert subscriber.status is not SubscriberStatus.active


def test_a_blocked_row_becomes_a_durable_operator_exception(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)

    heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="service:walled_account_healing",
        reason="scheduled healing",
    )

    alert = _exception_alert(db_session, subscriber.id)
    assert alert is not None
    assert alert.status is AlertStatus.open
    assert alert.details["overdue_receivable_total"] == "0.50"
    assert alert.details["disposition"] == "blocked_overdue_receivable"
    assert len(alert.details["overdue_receivable_invoice_ids"]) == 1


def test_the_operator_exception_is_deduplicated_and_idempotent(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)

    for _ in range(3):
        heal_walled_account(
            db_session,
            str(subscriber.id),
            require_zero_overdue_receivable=True,
            actor="service:walled_account_healing",
            reason="scheduled healing",
        )

    assert (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint.like(f"{UNWALL_EXCEPTION_PREFIX}%"))
        .count()
        == 1
    )


def test_healing_is_idempotent_once_the_account_is_active(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    first = heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="pytest",
        reason="pytest healing",
    )
    second = heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="pytest",
        reason="pytest healing",
    )

    assert first.disposition is UnwallDisposition.restored
    assert second.disposition is UnwallDisposition.not_walled
    assert second.restored is False


def test_payment_schedules_one_exact_account_timer_idempotently(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    _wall(db_session, subscription)

    now = datetime.now(UTC)
    context = _context(
        actor="pytest:payment-event",
        scope=str(account_id),
        reason="settled payment",
    )
    first = schedule_walled_account_healing(
        db_session,
        account_id=account_id,
        due_at=now,
        context=context,
    )
    replay = schedule_walled_account_healing(
        db_session,
        account_id=account_id,
        due_at=now,
        context=context,
    )

    assert replay.replayed is True
    assert replay.timer_id == first.timer_id
    assert db_session.query(DurableTimer).count() == 1


def test_payment_event_adapter_schedules_the_named_account_only(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    event = Event(
        event_type=EventType.payment_received,
        payload={"payment_id": str(uuid4())},
        account_id=subscriber.id,
    )

    BillingLifecycleProjectionHandler().handle(db_session, event)

    timers = db_session.query(DurableTimer).all()
    assert len(timers) == 1
    assert timers[0].owner == UNWALL_OWNER
    assert timers[0].entity_kind == "subscriber"
    assert timers[0].entity_id == subscriber.id


def test_fired_account_timer_heals_only_the_named_zero_debt_account(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    _wall(db_session, subscription)
    scheduled, fired = _schedule_and_fire(db_session, account_id)

    result = consume_walled_account_healing_due(
        db_session,
        account_id=account_id,
        timer_id=scheduled.timer_id,
        generation=scheduled.generation,
        event_id=fired.event_id,
        context=CommandContext.system(
            actor="pytest:billing-timer-consumer",
            scope=str(account_id),
            reason=UNWALL_TIMER_TRIGGER,
            command_id=fired.event_id,
            correlation_id=fired.event_id,
            causation_id=fired.event_id,
            idempotency_key=f"event:{fired.event_id}",
        ),
    )

    # The durable dispatcher delivered the fired event after commit; an
    # explicit redelivery is therefore an exact receipt-backed replay.
    assert result == "replayed"
    db_session.refresh(subscriber)
    assert subscriber.status is SubscriberStatus.active


def test_fired_timer_keeps_fifty_kobo_and_records_one_exception(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)
    scheduled, fired = _schedule_and_fire(db_session, account_id)
    context = CommandContext.system(
        actor="pytest:billing-timer-consumer",
        scope=str(account_id),
        reason=UNWALL_TIMER_TRIGGER,
        command_id=fired.event_id,
        correlation_id=fired.event_id,
        causation_id=fired.event_id,
        idempotency_key=f"event:{fired.event_id}",
    )

    first = consume_walled_account_healing_due(
        db_session,
        account_id=account_id,
        timer_id=scheduled.timer_id,
        generation=scheduled.generation,
        event_id=fired.event_id,
        context=context,
    )
    replay = consume_walled_account_healing_due(
        db_session,
        account_id=account_id,
        timer_id=scheduled.timer_id,
        generation=scheduled.generation,
        event_id=fired.event_id,
        context=context,
    )

    assert first == "replayed"
    assert replay == "replayed"
    db_session.refresh(subscriber)
    assert subscriber.status is not SubscriberStatus.active
    alert = _exception_alert(db_session, subscriber.id)
    assert alert is not None
    assert alert.details["overdue_receivable_total"] == "0.50"
    assert (
        db_session.query(OwnerOutputReceipt)
        .filter(OwnerOutputReceipt.consumer == UNWALL_OWNER)
        .count()
        == 1
    )
