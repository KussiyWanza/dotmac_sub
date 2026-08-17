"""Reconcile of stale `overdue` enforcement locks (lock active, no overdue debt).

Clears the stale overdue lock; reactivates subs held by nothing else; leaves
subs held by another lock suspended; ignores accounts that owe overdue debt.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.models.splynx_transaction import SplynxBillingTransaction
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services.account_lifecycle import suspend_subscription
from app.services.stale_overdue_lock_reconcile import reconcile


def _postpaid_active(db, subscriber, offer):
    now = datetime.now(UTC)
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        start_at=now - timedelta(days=30),
        next_billing_at=now + timedelta(days=1),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_apply_restores_stale_overdue_only(db_session, subscriber, catalog_offer):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    # Overdue lock but NO overdue invoice => stale.
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:stale",
    )

    result = reconcile(db_session, apply=True)

    assert result.candidates == 1
    assert result.restored == 1
    assert result.items[0].action == "restored"
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active


def test_other_lock_keeps_suspended_after_clearing_overdue(
    db_session, subscriber, catalog_offer
):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:stale",
    )
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.prepaid,
        source="legacy_prepaid_balance",
    )

    result = reconcile(db_session, apply=True)

    assert result.candidates == 1
    assert result.restored == 0
    assert result.lock_cleared_only == 1
    assert "prepaid" in result.items[0].other_active_locks
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended


def test_account_with_real_overdue_debt_is_not_a_candidate(
    db_session, subscriber, catalog_offer
):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:real",
    )
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,  # past due_at => real overdue debt
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:stale-overdue-lock",
            due_date_policy_version="test-v1",
        ),
    )
    db_session.commit()

    result = reconcile(db_session, apply=True)

    assert result.candidates == 0
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended


def test_ledger_covered_overdue_lock_can_be_restored(
    db_session, subscriber, catalog_offer
):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:covered",
    )
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:stale-overdue-lock",
            due_date_policy_version="test-v1",
        ),
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("6000.00"),
            currency="NGN",
            memo="covered balance",
        )
    )
    db_session.commit()

    dry = reconcile(db_session, apply=False, restore_ledger_covered=True)
    assert dry.candidates == 1
    assert dry.restored == 1
    assert dry.items[0].available_balance == "1000.00"

    result = reconcile(db_session, apply=True, restore_ledger_covered=True)

    assert result.candidates == 1
    assert result.restored == 1
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active


def test_archived_splynx_credit_cannot_restore_native_overdue_debt(
    db_session, subscriber, catalog_offer
):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:not-covered-natively",
    )
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:stale-overdue-lock",
            due_date_policy_version="test-v1",
        ),
    )
    db_session.add(
        SplynxBillingTransaction(
            splynx_transaction_id=991001,
            splynx_customer_id=991001,
            subscriber_id=subscriber.id,
            entry_type="credit",
            amount=Decimal("6000.00"),
            transaction_date=date(2026, 3, 1),
            description="Archived pre-cutover receipt",
        )
    )
    db_session.commit()

    result = reconcile(db_session, apply=False, restore_ledger_covered=True)

    assert result.candidates == 0
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended


def test_dry_run_writes_nothing(db_session, subscriber, catalog_offer):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:stale",
    )

    result = reconcile(db_session, apply=False)

    assert result.candidates == 1
    assert result.restored == 1  # projected
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended
