"""Tests for collections and dunning service."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.audit import AuditEvent
from app.models.catalog import SubscriptionStatus
from app.models.collections import DunningAction, DunningCase, DunningCaseStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.subscriber import SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.schemas.collections import (
    DunningActionLogCreate,
    DunningCaseCreate,
    DunningCaseUpdate,
)
from app.services import collections as collections_service
from app.services.settings_cache import SettingsCache
from app.services.settings_spec import get_spec


def test_create_dunning_case(db_session, subscriber_account):
    """Test creating a dunning case."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
            current_step=1,
        ),
    )
    assert case.account_id == subscriber_account.id
    assert case.status == DunningCaseStatus.open
    assert case.current_step == 1


def test_open_dunning_case_refreshes_account_status(
    db_session, subscriber, subscription
):
    subscription.status = SubscriptionStatus.active
    subscriber.status = SubscriberStatus.active
    db_session.commit()

    collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber.id,
            status=DunningCaseStatus.open,
        ),
    )

    db_session.refresh(subscriber)
    assert subscriber.status == SubscriberStatus.delinquent


def test_dunning_case_status_transitions(db_session, subscriber_account):
    """Test dunning case status transitions."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    assert case.status == DunningCaseStatus.open

    # Pause the case
    updated = collections_service.dunning_cases.update(
        db_session,
        str(case.id),
        DunningCaseUpdate(status=DunningCaseStatus.paused),
    )
    assert updated.status == DunningCaseStatus.paused

    # Resolve the case
    resolved = collections_service.dunning_cases.update(
        db_session,
        str(case.id),
        DunningCaseUpdate(
            status=DunningCaseStatus.resolved,
            resolved_at=datetime.now(UTC),
        ),
    )
    assert resolved.status == DunningCaseStatus.resolved
    assert resolved.resolved_at is not None


def test_dunning_action_log_creation(db_session, subscriber_account):
    """Test creating dunning action logs."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    log = collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            step_day=1,
            outcome="Email sent successfully",
        ),
    )
    assert log.case_id == case.id
    assert log.action == DunningAction.notify
    assert log.outcome == "Email sent successfully"


def test_suspension_notification_uses_configured_dedupe_window(
    db_session, subscriber_account
):
    from app.services.collections._core import _create_suspension_notification

    spec = get_spec(SettingDomain.collections, "suspension_notification_dedupe_hours")
    assert spec is not None
    assert spec.default == 24
    assert spec.min_value == 1
    assert spec.max_value == 168

    db_session.add(
        DomainSetting(
            domain=SettingDomain.collections,
            key="suspension_notification_dedupe_hours",
            value_type=SettingValueType.integer,
            value_text="1",
            is_active=True,
        )
    )
    existing = Notification(
        channel=NotificationChannel.email,
        event_type="account_suspended",
        category="billing",
        recipient=subscriber_account.email,
        subject="Account Suspended",
        body="Already queued",
        status=NotificationStatus.queued,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add(existing)
    db_session.commit()
    SettingsCache.invalidate(
        SettingDomain.collections.value, "suspension_notification_dedupe_hours"
    )

    _create_suspension_notification(db_session, str(subscriber_account.id))
    db_session.commit()

    rows = (
        db_session.query(Notification)
        .filter(Notification.recipient == subscriber_account.email)
        .filter(Notification.subject == "Account Suspended")
        .order_by(Notification.created_at.asc())
        .all()
    )
    assert len(rows) == 2


def test_list_dunning_cases_by_account(db_session, subscriber_account):
    """Test listing dunning cases by account."""
    collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )

    cases = collections_service.dunning_cases.list(
        db_session,
        account_id=subscriber_account.id,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(cases) >= 1
    assert all(c.account_id == subscriber_account.id for c in cases)


def test_list_dunning_cases_by_status(db_session, subscriber_account):
    """Test listing dunning cases by status."""
    collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.closed,
        ),
    )

    open_cases = collections_service.dunning_cases.list(
        db_session,
        account_id=None,
        status=DunningCaseStatus.open,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert all(c.status == DunningCaseStatus.open for c in open_cases)


def test_list_action_logs_by_case(db_session, subscriber_account):
    """Test listing action logs by case."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            step_day=1,
        ),
    )
    collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.throttle,
            step_day=7,
        ),
    )

    logs = collections_service.dunning_action_logs.list(
        db_session,
        case_id=case.id,
        invoice_id=None,
        payment_id=None,
        order_by="executed_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(logs) >= 2
    assert all(l.case_id == case.id for l in logs)


def test_dunning_case_resolution(db_session, subscriber_account):
    """Test full dunning case resolution flow."""
    # Create case
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
            current_step=1,
            notes="Payment overdue",
        ),
    )

    # Log first action
    collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            step_day=1,
            outcome="Email sent",
        ),
    )

    # Update step
    updated = collections_service.dunning_cases.update(
        db_session,
        str(case.id),
        DunningCaseUpdate(current_step=2),
    )
    assert updated.current_step == 2

    # Log second action
    collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.suspend,
            step_day=7,
            outcome="Payment successful",
        ),
    )

    # Resolve case
    resolved = collections_service.dunning_cases.update(
        db_session,
        str(case.id),
        DunningCaseUpdate(
            status=DunningCaseStatus.resolved,
            resolved_at=datetime.now(UTC),
            notes="Payment received",
        ),
    )
    assert resolved.status == DunningCaseStatus.resolved


def test_delete_dunning_case(db_session, subscriber_account):
    """Test deleting a dunning case."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    collections_service.dunning_cases.delete(db_session, str(case.id))
    assert db_session.get(DunningCase, case.id) is None


# =============================================================================
# DunningWorkflow.run — functional tests (paused cases must be skipped)
# =============================================================================


def _setup_overdue_postpaid_account(
    db_session, subscriber, subscription, catalog_offer
):
    """Overdue postpaid account with a configured day-1 notify dunning step."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from app.models.billing import Invoice, InvoiceStatus
    from app.models.catalog import (
        BillingMode,
        PolicyDunningStep,
        PolicySet,
        SubscriptionStatus,
    )
    from app.models.catalog import (
        DunningAction as CatalogDunningAction,
    )

    subscriber.billing_mode = BillingMode.postpaid
    subscriber.grace_period_days = 0
    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active

    policy_set = PolicySet(name="Dunning Test Policy")
    db_session.add(policy_set)
    db_session.flush()
    db_session.add(
        PolicyDunningStep(
            policy_set_id=policy_set.id,
            day_offset=1,
            action=CatalogDunningAction.notify,
            note="day-1 reminder",
        )
    )
    catalog_offer.policy_set_id = policy_set.id
    # The subscription may resolve its policy via the offer version first.
    if subscription.offer_version is not None:
        subscription.offer_version.policy_set_id = policy_set.id

    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-DUN-RUN-1",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=datetime.now(UTC) - timedelta(days=5),
        metadata_={},
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def _add_settled_unallocated_credit(db_session, subscriber, amount, currency="NGN"):
    """Create exact settlement evidence for credit awaiting allocation."""
    from decimal import Decimal

    from app.models.billing import (
        LedgerEntry,
        LedgerEntryType,
        LedgerSource,
        Payment,
        PaymentSettlement,
        PaymentSettlementOrigin,
        PaymentStatus,
    )

    credit = Decimal(amount)
    payment = Payment(
        account_id=subscriber.id,
        amount=credit,
        currency=currency,
        status=PaymentStatus.succeeded,
        paid_at=datetime.now(UTC) - timedelta(hours=1),
        is_active=True,
    )
    db_session.add(payment)
    db_session.flush()
    ledger_entry = LedgerEntry(
        account_id=subscriber.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=credit,
        currency=currency,
        memo="settled unallocated payment credit",
    )
    db_session.add(ledger_entry)
    db_session.flush()
    db_session.add(
        PaymentSettlement(
            payment_id=payment.id,
            unallocated_ledger_entry_id=ledger_entry.id,
            amount=credit,
            unallocated_amount=credit,
            prepaid_amount=Decimal("0.00"),
            currency=currency,
            origin=PaymentSettlementOrigin.system,
            idempotency_key=f"test-dunning-credit-{payment.id}",
        )
    )
    db_session.commit()
    return payment


def test_dunning_run_ignores_noncollectible_invoice_with_residual_balance(
    db_session, subscriber, subscription, catalog_offer
):
    """A void invoice that still carries a positive balance_due must NOT open a
    dunning case — only issued/partially_paid/overdue statuses are collectible.
    Guards against dunning a debt that isn't actually owed."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from app.models.billing import Invoice, InvoiceStatus
    from app.models.catalog import (
        BillingMode,
        PolicyDunningStep,
        PolicySet,
        SubscriptionStatus,
    )
    from app.models.catalog import (
        DunningAction as CatalogDunningAction,
    )
    from app.schemas.collections import DunningRunRequest

    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active
    policy_set = PolicySet(name="Void Invoice Policy")
    db_session.add(policy_set)
    db_session.flush()
    db_session.add(
        PolicyDunningStep(
            policy_set_id=policy_set.id,
            day_offset=1,
            action=CatalogDunningAction.notify,
            note="d1",
        )
    )
    catalog_offer.policy_set_id = policy_set.id
    if subscription.offer_version is not None:
        subscription.offer_version.policy_set_id = policy_set.id
    # Voided invoice that (wrongly) still carries balance_due — must be ignored.
    db_session.add(
        Invoice(
            account_id=subscriber.id,
            invoice_number="INV-VOID-1",
            status=InvoiceStatus.void,
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            due_at=datetime.now(UTC) - timedelta(days=5),
            metadata_={},
        )
    )
    db_session.commit()

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    open_cases = (
        db_session.query(DunningCase)
        .filter(
            DunningCase.account_id == subscriber.id,
            DunningCase.status == DunningCaseStatus.open,
        )
        .all()
    )
    assert open_cases == []
    assert response.actions_created == 0


def test_dunning_run_skips_paused_case(
    db_session, subscriber, subscription, catalog_offer
):
    """A paused case must not execute escalation steps."""
    from datetime import UTC, datetime

    from app.models.collections import DunningActionLog
    from app.schemas.collections import DunningRunRequest

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.paused,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    db_session.refresh(case)
    assert case.status == DunningCaseStatus.paused
    assert case.current_step is None
    assert response.actions_created == 0
    assert response.skipped >= 1
    logs = (
        db_session.query(DunningActionLog)
        .filter(DunningActionLog.case_id == case.id)
        .all()
    )
    assert logs == []


def test_dunning_run_isolates_one_account_failure(db_session, subscriber, monkeypatch):
    """One broken account rolls back without suppressing the next account."""
    from decimal import Decimal

    from app.models.billing import Invoice, InvoiceStatus
    from app.models.subscriber import Subscriber
    from app.schemas.collections import DunningRunRequest
    from app.services.collections._core import DunningAccountRunResult

    second = Subscriber(
        first_name="Second",
        last_name="Dunning",
        email="second-dunning@example.test",
    )
    db_session.add(second)
    db_session.flush()
    due_at = datetime.now(UTC) - timedelta(days=5)
    db_session.add_all(
        [
            Invoice(
                account_id=account_id,
                invoice_number=f"INV-ISOLATION-{index}",
                status=InvoiceStatus.issued,
                total=Decimal("100.00"),
                balance_due=Decimal("100.00"),
                due_at=due_at,
                metadata_={},
            )
            for index, account_id in enumerate((subscriber.id, second.id), start=1)
        ]
    )
    db_session.commit()

    calls = []

    def fake_account_run(_db, command):
        calls.append(command.account_id)
        if len(calls) == 1:
            raise RuntimeError("account-local defect")
        return DunningAccountRunResult(actions_created=1)

    monkeypatch.setattr(
        "app.services.collections._core._run_dunning_account", fake_account_run
    )

    result = collections_service.dunning_workflow.run(
        db_session,
        DunningRunRequest(run_at=datetime.now(UTC)),
    )

    assert len(calls) == 2
    assert result.errors == 1
    assert result.actions_created == 1
    assert result.skipped == 1


def test_dunning_account_revalidates_stale_cohort_invoice(
    db_session, subscriber, subscription, catalog_offer
):
    """A paid invoice cannot be enforced from an earlier cohort snapshot."""
    from app.models.billing import InvoiceStatus
    from app.services.collections._core import (
        DunningAccountRunCommand,
        _run_dunning_account,
    )

    invoice = _setup_overdue_postpaid_account(
        db_session, subscriber, subscription, catalog_offer
    )
    stale_invoice_id = invoice.id
    invoice.status = InvoiceStatus.paid
    invoice.balance_due = 0
    db_session.commit()

    result = _run_dunning_account(
        db_session,
        DunningAccountRunCommand(
            account_id=subscriber.id,
            invoice_ids=(stale_invoice_id,),
            run_at=datetime.now(UTC),
            dry_run=False,
        ),
    )

    assert result.skipped == 1
    assert result.actions_created == 0
    assert result.has_effective_overdue is False


def test_payment_resolves_open_but_not_paused_cases(
    db_session, subscriber, subscription, catalog_offer
):
    """An incoming payment auto-resolves OPEN dunning cases but must leave a
    PAUSED case (operator hold) untouched (#A7 paused-policy)."""
    open_case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    paused_case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.paused,
        started_at=datetime.now(UTC),
    )
    db_session.add_all([open_case, paused_case])
    subscription.status = SubscriptionStatus.active
    subscriber.status = SubscriberStatus.delinquent
    db_session.commit()

    collections_service.dunning_workflow.resolve_cases_for_account(
        db_session, str(subscriber.id), None, commit=False
    )
    db_session.flush()

    db_session.refresh(open_case)
    db_session.refresh(paused_case)
    db_session.refresh(subscriber)
    assert open_case.status == DunningCaseStatus.resolved
    assert paused_case.status == DunningCaseStatus.paused  # operator hold kept
    assert subscriber.status == SubscriberStatus.active


def test_suspend_proceeds_when_overdue_and_unshielded(
    db_session, subscriber, subscription, catalog_offer
):
    """Control: an overdue, unshielded account is actually suspended."""
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=7, note=None
    )
    assert outcome == "suspended"
    assert get_active_locks(db_session, subscription_id=str(subscription.id))


def test_suspend_adds_overdue_lock_to_already_suspended_subscription(
    db_session, subscriber, subscription, catalog_offer
):
    """Dunning must keep owing customers walled after unrelated locks clear."""
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import get_active_locks, suspend_subscription
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.admin,
        source="test:admin_hold",
    )
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=7, note=None
    )

    locks = get_active_locks(db_session, subscription_id=str(subscription.id))
    assert outcome == "suspended"
    assert {lock.reason for lock in locks} == {
        EnforcementReason.admin,
        EnforcementReason.overdue,
    }


def test_suspend_waits_for_minimum_notice_runway(
    db_session, subscriber, subscription, catalog_offer
):
    """Service-affecting dunning must not cut before the notice runway."""
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=1, note=None
    )

    assert outcome == "notice_grace_active"
    assert not get_active_locks(db_session, subscription_id=str(subscription.id))


def test_immediate_suspend_step_waits_until_actual_overdue_runway(
    db_session, subscriber, subscription, catalog_offer
):
    """A day-0 suspend policy still waits until the real overdue age reaches
    the configured notice runway."""
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session,
        case,
        DunningAction.suspend,
        day_offset=0,
        note=None,
        overdue_days=2,
    )

    assert outcome == "notice_grace_active"
    assert not get_active_locks(db_session, subscription_id=str(subscription.id))


def test_immediate_suspend_step_uses_actual_overdue_days_for_runway(
    db_session, subscriber, subscription, catalog_offer
):
    """Regression: day-0 policies must not be permanently blocked by the
    minimum notice runway once the customer is actually old enough."""
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session,
        case,
        DunningAction.suspend,
        day_offset=0,
        note=None,
        overdue_days=3,
    )

    assert outcome == "suspended"
    assert get_active_locks(db_session, subscription_id=str(subscription.id))


def test_dunning_run_day_zero_suspend_uses_actual_overdue_days(
    db_session, subscriber, subscription, catalog_offer
):
    """The scheduled workflow must pass real overdue age to the runway gate,
    not the policy step day."""
    from app.models.catalog import (
        DunningAction as CatalogDunningAction,
    )
    from app.models.catalog import (
        PolicyDunningStep,
        PolicySet,
    )
    from app.models.collections import DunningActionLog
    from app.schemas.collections import DunningRunRequest
    from app.services.account_lifecycle import get_active_locks

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    policy_set = PolicySet(name="Immediate Suspend Test Policy")
    db_session.add(policy_set)
    db_session.flush()
    db_session.add(
        PolicyDunningStep(
            policy_set_id=policy_set.id,
            day_offset=0,
            action=CatalogDunningAction.suspend,
            note="day-0 suspend",
        )
    )
    catalog_offer.policy_set_id = policy_set.id
    if subscription.offer_version is not None:
        subscription.offer_version.policy_set_id = policy_set.id
    db_session.commit()

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    log = (
        db_session.query(DunningActionLog)
        .filter(DunningActionLog.action == DunningAction.suspend)
        .one()
    )
    assert response.actions_created == 1
    assert log.step_day == 0
    assert log.outcome == "suspended"
    assert get_active_locks(db_session, subscription_id=str(subscription.id))


def test_suspend_not_blocked_by_notification_backlog_by_default(
    db_session, subscriber, subscription, catalog_offer
):
    """Notification delivery is monitored, but not a default hard billing gate."""
    from datetime import timedelta

    from app.models.notification import (
        Notification,
        NotificationChannel,
        NotificationStatus,
    )
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    queued_at = datetime.now(UTC) - timedelta(hours=3)
    db_session.add(
        Notification(
            channel=NotificationChannel.email,
            event_type="invoice_overdue",
            category="billing",
            recipient="billing@example.test",
            subject="Invoice overdue",
            body="pay",
            status=NotificationStatus.queued,
            created_at=queued_at,
            updated_at=queued_at,
        )
    )
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=7, note=None
    )

    assert outcome == "suspended"
    assert get_active_locks(db_session, subscription_id=str(subscription.id))


def test_suspend_skipped_when_balance_cleared_mid_run(
    db_session, subscriber, subscription, catalog_offer
):
    """The dunning-after-payment race: if the balance cleared since the run's
    snapshot, the re-read under lock cancels the suspend (#A1)."""
    from app.models.billing import Invoice, InvoiceStatus
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    # Simulate the payment landing after the run snapshotted balances.
    inv = db_session.query(Invoice).filter(Invoice.account_id == subscriber.id).one()
    inv.balance_due = 0
    inv.status = InvoiceStatus.paid
    db_session.commit()

    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=7, note=None
    )
    assert outcome == "balance_cleared"
    assert not get_active_locks(db_session, subscription_id=str(subscription.id))


def test_prepaid_suspend_skipped_when_available_balance_covers_invoice(
    db_session, subscriber, subscription, catalog_offer
):
    """Prepaid monthly invoices can create dunning cases, but the service cut
    is guarded by available balance so imported/ledger credit prevents a
    wrongful suspension."""
    from decimal import Decimal

    from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
    from app.models.catalog import BillingMode
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    subscriber.billing_mode = BillingMode.prepaid
    subscriber.splynx_customer_id = 987654
    subscriber.deposit = Decimal("500.00")
    subscriber.min_balance = Decimal("0.00")
    subscription.billing_mode = BillingMode.postpaid
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("200.00"),
            currency="NGN",
            memo="prepaid test credit",
        )
    )
    db_session.commit()

    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=7, note=None
    )
    assert outcome == "billing_profile_invalid"
    assert not get_active_locks(db_session, subscription_id=str(subscription.id))


def test_suspend_shielded_by_active_arrangement(
    db_session, subscriber, subscription, catalog_offer
):
    """A customer with an active payment arrangement must not be dunned (#A2)."""
    from datetime import date
    from decimal import Decimal

    from app.models.payment_arrangement import ArrangementStatus, PaymentArrangement
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    db_session.add(
        PaymentArrangement(
            subscriber_id=subscriber.id,
            status=ArrangementStatus.active,
            is_active=True,
            total_amount=Decimal("100.00"),
            installment_amount=Decimal("50.00"),
            installments_total=2,
            start_date=date(2026, 1, 1),
        )
    )
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=7, note=None
    )
    assert outcome == "shielded"
    assert not get_active_locks(db_session, subscription_id=str(subscription.id))


def test_dunning_run_skips_account_with_active_arrangement(
    db_session, subscriber, subscription, catalog_offer
):
    """An active arrangement pauses notices and case creation, not only suspension."""
    from datetime import date
    from decimal import Decimal

    from app.models.collections import DunningActionLog
    from app.models.payment_arrangement import ArrangementStatus, PaymentArrangement
    from app.schemas.collections import DunningRunRequest

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    db_session.add(
        PaymentArrangement(
            subscriber_id=subscriber.id,
            status=ArrangementStatus.active,
            is_active=True,
            total_amount=Decimal("100.00"),
            installment_amount=Decimal("50.00"),
            installments_total=2,
            start_date=date(2026, 1, 1),
        )
    )
    db_session.commit()

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    assert response.actions_created == 0
    assert response.skipped >= 1
    assert (
        db_session.query(DunningCase)
        .filter(DunningCase.account_id == subscriber.id)
        .count()
        == 0
    )
    assert db_session.query(DunningActionLog).count() == 0


def test_prepaid_invoice_rows_do_not_create_dunning_case(
    db_session, subscriber, subscription, catalog_offer
):
    """Prepaid has its own balance-enforcement path, not dunning."""
    from datetime import timedelta
    from decimal import Decimal

    from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
    from app.models.catalog import (
        BillingCycle,
        BillingMode,
        SubscriptionStatus,
    )
    from app.schemas.collections import DunningRunRequest

    subscriber.billing_mode = BillingMode.prepaid
    subscriber.splynx_customer_id = 987655
    subscriber.deposit = Decimal("500.00")
    subscriber.min_balance = Decimal("0.00")
    subscriber.grace_period_days = 0
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    catalog_offer.billing_cycle = BillingCycle.monthly

    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-PREPAID-DUN-RUN-1",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=datetime.now(UTC) - timedelta(days=5),
        metadata_={},
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid renewal",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    db_session.refresh(invoice)
    assert response.accounts_scanned == 0
    assert response.skipped == 0
    assert response.cases_created == 0
    assert response.actions_created == 0
    assert invoice.status == InvoiceStatus.issued
    assert (
        db_session.query(DunningCase)
        .filter(DunningCase.account_id == subscriber.id)
        .count()
        == 0
    )


def test_imported_line_less_prepaid_invoice_does_not_create_dunning_case(
    db_session, subscriber, subscription
):
    """Imported prepaid AR without lines is legacy balance data, not dunning."""
    from datetime import timedelta
    from decimal import Decimal

    from app.models.billing import Invoice, InvoiceStatus
    from app.models.catalog import BillingMode, SubscriptionStatus
    from app.schemas.collections import DunningRunRequest

    subscriber.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-IMPORTED-PREPAID-DUN-RUN",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=datetime.now(UTC) - timedelta(days=5),
        metadata_={"imported_via": "system_import_wizard"},
    )
    db_session.add(invoice)
    db_session.commit()

    assert (
        collections_service.has_overdue_balance(db_session, str(subscriber.id)) is False
    )

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    db_session.refresh(invoice)
    assert response.accounts_scanned == 0
    assert response.cases_created == 0
    assert response.actions_created == 0
    assert invoice.status == InvoiceStatus.issued
    assert (
        db_session.query(DunningCase)
        .filter(DunningCase.account_id == subscriber.id)
        .count()
        == 0
    )


def test_prepaid_monthly_dunning_does_not_suspend_service(
    db_session, subscriber, subscription, catalog_offer
):
    """Prepaid service cuts are owned by prepaid_balance_sweep, not dunning."""
    from decimal import Decimal

    from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
    from app.models.catalog import (
        BillingCycle,
        BillingMode,
        SubscriptionStatus,
    )
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.schemas.collections import DunningRunRequest
    from app.services.account_lifecycle import get_active_locks

    subscriber.billing_mode = BillingMode.prepaid
    subscriber.min_balance = Decimal("0.00")
    subscriber.grace_period_days = 0
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    catalog_offer.billing_cycle = BillingCycle.monthly
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.collections,
                key="billing_enforcement_min_enforcing_day_offset",
                value_text="3",
                is_active=True,
            ),
        ]
    )
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-PREPAID-DUN-RUN-2",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=datetime.now(UTC) - timedelta(days=1),
        metadata_={},
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid renewal",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    db_session.refresh(subscription)
    db_session.refresh(invoice)
    assert response.accounts_scanned == 0
    assert response.skipped == 0
    assert response.actions_created == 0
    assert invoice.status == InvoiceStatus.issued
    assert subscription.status == SubscriptionStatus.active
    assert not get_active_locks(db_session, subscription_id=str(subscription.id))


def test_dunning_run_executes_step_for_active_case(
    db_session, subscriber, subscription, catalog_offer, monkeypatch
):
    """An open case with an overdue invoice executes the configured step."""
    from datetime import UTC, datetime

    from app.models.collections import DunningActionLog
    from app.schemas.collections import DunningRunRequest
    from app.services.events.types import EventType

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    emit_calls = []

    def _capture_emit(_db, event_type, payload, **kwargs):
        emit_calls.append((event_type, payload, kwargs))

    monkeypatch.setattr("app.services.collections._core.emit_event", _capture_emit)

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    db_session.refresh(case)
    assert case.status == DunningCaseStatus.open
    assert case.current_step == 1
    assert response.actions_created == 1
    logs = (
        db_session.query(DunningActionLog)
        .filter(DunningActionLog.case_id == case.id)
        .all()
    )
    assert len(logs) == 1
    assert logs[0].action == DunningAction.notify
    assert logs[0].outcome == "notification_sent"
    warning_calls = [
        call
        for call in emit_calls
        if call[0] == EventType.subscription_suspension_warning
    ]
    assert len(warning_calls) == 1
    assert warning_calls[0][1]["reason"] == "dunning"
    assert warning_calls[0][1]["invoice_number"] == "INV-DUN-RUN-1"


def test_dunning_run_skips_reconciliation_hold_invoice(
    db_session, subscriber, subscription, catalog_offer
):
    """Invoices on reconciliation hold must not drive enforcement actions."""
    from app.models.collections import DunningActionLog
    from app.schemas.collections import DunningRunRequest

    invoice = _setup_overdue_postpaid_account(
        db_session, subscriber, subscription, catalog_offer
    )
    invoice.metadata_ = {"reconciliation_hold": True}
    db_session.commit()

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    assert response.actions_created == 0
    assert response.accounts_scanned == 0
    assert db_session.query(DunningActionLog).count() == 0


def test_billing_enforcement_settles_payment_credit_before_dunning(
    db_session, subscriber, subscription, catalog_offer
):
    """Payment-backed credit is applied before overdue cases are scanned."""
    from decimal import Decimal

    from app.models.billing import InvoiceStatus
    from app.schemas.collections import BillingEnforcementRunRequest

    invoice = _setup_overdue_postpaid_account(
        db_session, subscriber, subscription, catalog_offer
    )
    _add_settled_unallocated_credit(db_session, subscriber, "100.00")

    response = collections_service.billing_enforcement_reconciler.run(
        db_session, BillingEnforcementRunRequest()
    )

    db_session.refresh(invoice)
    assert response.credit_accounts_settled == 1
    assert response.credit_invoices_touched == 1
    assert response.dunning_accounts_scanned == 0
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")


def test_billing_enforcement_settles_non_ngn_credit_before_dunning(
    db_session, subscriber, subscription, catalog_offer
):
    """The pre-dunning credit pass must not skip accounts with only USD credit."""
    from decimal import Decimal

    from app.models.billing import InvoiceStatus
    from app.schemas.collections import BillingEnforcementRunRequest

    invoice = _setup_overdue_postpaid_account(
        db_session, subscriber, subscription, catalog_offer
    )
    invoice.currency = "USD"
    _add_settled_unallocated_credit(db_session, subscriber, "100.00", "USD")

    response = collections_service.billing_enforcement_reconciler.run(
        db_session, BillingEnforcementRunRequest()
    )

    db_session.refresh(invoice)
    assert response.credit_accounts_settled == 1
    assert response.credit_invoices_touched == 1
    assert response.dunning_accounts_scanned == 0
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")


@pytest.mark.parametrize(
    "pre_dunning_status",
    [SubscriptionStatus.active, SubscriptionStatus.blocked],
)
def test_billing_enforcement_settlement_restores_dunned_account(
    db_session, subscriber, subscription, catalog_offer, pre_dunning_status
):
    """Scheduled pre-dunning credit settlement must also lift overdue locks."""
    from decimal import Decimal

    from app.models.billing import InvoiceStatus
    from app.models.enforcement_lock import EnforcementReason
    from app.schemas.collections import BillingEnforcementRunRequest
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    invoice = _setup_overdue_postpaid_account(
        db_session, subscriber, subscription, catalog_offer
    )
    subscription.status = pre_dunning_status
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    assert (
        _execute_dunning_action(
            db_session, case, DunningAction.suspend, day_offset=7, note=None
        )
        == "suspended"
    )
    assert get_active_locks(db_session, subscription_id=str(subscription.id))

    _add_settled_unallocated_credit(db_session, subscriber, "100.00")

    response = collections_service.billing_enforcement_reconciler.run(
        db_session, BillingEnforcementRunRequest()
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    db_session.refresh(case)
    db_session.refresh(subscriber)
    assert response.credit_accounts_settled == 1
    assert response.dunning_accounts_scanned == 0
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert subscription.status == SubscriptionStatus.active
    assert subscriber.status == SubscriberStatus.active
    assert case.status == DunningCaseStatus.resolved
    assert [
        lock
        for lock in get_active_locks(db_session, subscription_id=str(subscription.id))
        if lock.reason == EnforcementReason.overdue
    ] == []


def test_billing_enforcement_restore_failure_does_not_rollback_settlement(
    db_session, subscriber, subscription, catalog_offer, monkeypatch
):
    """A redundant enforcement restore failure must not undo settlement."""
    from decimal import Decimal

    from app.models.billing import InvoiceStatus
    from app.schemas.collections import BillingEnforcementRunRequest
    from app.services.collections import _core as collections_core
    from app.services.collections._core import _execute_dunning_action

    invoice = _setup_overdue_postpaid_account(
        db_session, subscriber, subscription, catalog_offer
    )
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    assert (
        _execute_dunning_action(
            db_session, case, DunningAction.suspend, day_offset=7, note=None
        )
        == "suspended"
    )

    _add_settled_unallocated_credit(db_session, subscriber, "100.00")

    def _raise_restore(*_args, **_kwargs):
        raise RuntimeError("restore failed")

    monkeypatch.setattr(collections_core, "restore_account_services", _raise_restore)

    response = collections_service.billing_enforcement_reconciler.run(
        db_session, BillingEnforcementRunRequest()
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    assert response.credit_accounts_settled == 1
    assert response.credit_settlement_errors == 0
    assert response.dunning_accounts_scanned == 0
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    # The payment-allocation owner already requested the access consequence.
    # The enforcement adapter's redundant restore failure must not reverse it.
    assert subscription.status == SubscriptionStatus.active
    failure = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "dunning_account_failed")
        .filter(AuditEvent.entity_id == str(subscriber.id))
        .one()
    )
    assert failure.metadata_["phase"] == "credit_settlement_restoration"


def test_billing_enforcement_health_observes_all_channels(db_session, monkeypatch):
    """Health is complete operational evidence, never a lifecycle gate."""
    from app.services import billing_enforcement_guards as guards

    monkeypatch.setattr(
        guards,
        "notification_delivery_health",
        lambda _db: guards.EnforcementHealth(
            ok=False,
            reasons=["critical_notifications_failed"],
            details={"recent_failed": 10},
        ),
    )
    monkeypatch.setattr(
        guards,
        "payment_channel_health",
        lambda _db: guards.EnforcementHealth(
            ok=False,
            reasons=["payment_inbox_failures"],
            details={"dead_letters": 1},
        ),
    )

    health = guards.billing_enforcement_health(db_session)

    assert health.ok is False
    assert health.reasons == [
        "critical_notifications_failed",
        "payment_inbox_failures",
    ]
    assert health.details["notification_recent_failed"] == 10
    assert health.details["payment_dead_letters"] == 1


def test_billing_enforcement_health_reports_notification_failure(
    db_session, monkeypatch
):
    from app.services import billing_enforcement_guards as guards

    monkeypatch.setattr(
        guards,
        "notification_delivery_health",
        lambda _db: guards.EnforcementHealth(
            ok=False,
            reasons=["critical_notifications_not_draining"],
            details={"old_queued": 1},
        ),
    )
    monkeypatch.setattr(
        guards,
        "payment_channel_health",
        lambda _db: guards.EnforcementHealth(ok=True),
    )

    health = guards.billing_enforcement_health(db_session)

    assert health.ok is False
    assert health.reasons == ["critical_notifications_not_draining"]
    assert health.details["notification_old_queued"] == 1


def test_billing_enforcement_credit_settle_failures_are_counted(
    db_session, subscriber, subscription, catalog_offer, monkeypatch
):
    """A credit-settle exception is isolated and reported, not fatal."""
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.services.billing import _common as billing_common
    from app.services.billing import reconcile_unposted
    from app.services.collections._core import BillingEnforcementReconciler

    invoice = _setup_overdue_postpaid_account(
        db_session, subscriber, subscription, catalog_offer
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.collections,
            key="billing_enforcement_settle_credit_before_dunning_enabled",
            value_text="true",
            is_active=True,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        billing_common,
        "get_account_credit_balance",
        lambda _db, _account_id: invoice.balance_due,
    )

    def _raise_settle(_db, _account_id):
        raise RuntimeError("settlement failed")

    monkeypatch.setattr(
        reconcile_unposted,
        "settle_open_invoices_from_credit",
        _raise_settle,
    )

    stats = BillingEnforcementReconciler._settle_due_credit_before_dunning(
        db_session, datetime.now(UTC)
    )

    assert stats["credit_accounts_scanned"] == 1
    assert stats["credit_accounts_settled"] == 0
    assert stats["credit_invoices_touched"] == 0
    assert stats["credit_settlement_errors"] == 1
    assert stats["credit_applied"] == "0.00"
