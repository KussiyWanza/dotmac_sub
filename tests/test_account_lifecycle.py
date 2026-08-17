"""Scenario tests for subscription/account lifecycle state machine."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.catalog import (
    BillingMode,
    CatalogOffer,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_lifecycle import (
    ALLOWED_RESTORERS,
    activate_subscription,
    cancel_subscription,
    compute_account_status,
    expire_subscription,
    has_active_lock,
    restore_subscription,
    suspend_subscription,
    transition_account_status,
)
from app.services.events import emit_event
from app.services.events.dispatcher import EventDispatcher
from app.services.events.types import EventType


def _make_subscriber(db: Session, **kwargs) -> Subscriber:
    defaults = {
        "first_name": "Test",
        "last_name": "User",
        "email": f"test-{uuid.uuid4().hex[:8]}@example.com",
        "status": SubscriberStatus.active,
    }
    defaults.update(kwargs)
    sub = Subscriber(**defaults)
    db.add(sub)
    db.flush()
    return sub


def _make_offer(db: Session) -> CatalogOffer:
    from app.models.catalog import AccessType, OfferStatus, PriceBasis, ServiceType

    offer = CatalogOffer(
        name=f"Test Offer {uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    return offer


def _make_subscription(
    db: Session, subscriber: Subscriber, offer: CatalogOffer, **kwargs
) -> Subscription:
    defaults = {
        "subscriber_id": subscriber.id,
        "offer_id": offer.id,
        "status": SubscriptionStatus.active,
        "billing_mode": BillingMode.prepaid,
        "unit_price": Decimal("100.00"),
    }
    defaults.update(kwargs)
    sub = Subscription(**defaults)
    db.add(sub)
    db.flush()
    return sub


@pytest.mark.parametrize(
    "target_status",
    (SubscriberStatus.disabled, SubscriberStatus.canceled),
)
def test_terminal_account_transition_clears_prepaid_timers(
    db_session: Session,
    target_status: SubscriberStatus,
) -> None:
    subscriber = _make_subscriber(
        db_session,
        prepaid_low_balance_at=datetime.now(UTC),
        prepaid_deactivation_at=datetime.now(UTC),
    )
    offer = _make_offer(db_session)
    _make_subscription(db_session, subscriber, offer)

    result = transition_account_status(
        db_session,
        str(subscriber.id),
        target_status,
        reason="operator terminal transition",
        source="test:terminal-prepaid-timers",
        emit=False,
    )

    assert result == target_status
    assert subscriber.prepaid_low_balance_at is None
    assert subscriber.prepaid_deactivation_at is None


def test_last_subscription_expiry_clears_prepaid_timers(db_session: Session) -> None:
    subscriber = _make_subscriber(
        db_session,
        prepaid_low_balance_at=datetime.now(UTC),
        prepaid_deactivation_at=datetime.now(UTC),
    )
    offer = _make_offer(db_session)
    subscription = _make_subscription(db_session, subscriber, offer)

    expire_subscription(db_session, str(subscription.id), emit=False)

    assert subscriber.status == SubscriberStatus.canceled
    assert subscriber.prepaid_low_balance_at is None
    assert subscriber.prepaid_deactivation_at is None


class TestSuspendSubscription:
    """Tests for suspend_subscription."""

    def test_creates_lock_and_suspends(self, db_session):
        """Single lock: subscription moves to suspended."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:test-123",
            emit=False,
        )

        assert lock.is_active is True
        assert lock.reason == EnforcementReason.overdue
        assert subscription.status == SubscriptionStatus.suspended
        assert subscriber.status == SubscriberStatus.suspended

    def test_idempotent_suspend_creates_second_lock(self, db_session):
        """Suspending twice creates two locks, subscription stays suspended."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        lock1 = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        lock2 = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="fup_rule:42",
            emit=False,
        )

        assert lock1.id != lock2.id
        assert lock1.is_active is True
        assert lock2.is_active is True
        assert subscription.status == SubscriptionStatus.suspended

    def test_cannot_suspend_canceled_subscription(self, db_session):
        """Suspension of a terminal subscription raises ValueError."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(
            db_session, subscriber, offer, status=SubscriptionStatus.canceled
        )

        with pytest.raises(ValueError, match="Cannot suspend"):
            suspend_subscription(
                db_session,
                str(subscription.id),
                reason=EnforcementReason.admin,
                source="admin:test",
                emit=False,
            )


class TestRestoreSubscription:
    """Tests for restore_subscription."""

    def test_single_lock_restore(self, db_session):
        """Payment clears overdue lock, subscription restores."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        assert subscription.status == SubscriptionStatus.suspended

        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-123",
            emit=False,
        )

        assert restored is True
        assert subscription.status == SubscriptionStatus.active
        assert subscription.start_at is not None
        assert subscription.next_billing_at is not None
        assert subscriber.status == SubscriberStatus.active

    def test_restore_skips_duplicate_login_when_active_sibling_exists(self, db_session):
        """Payment clears the lock but does not violate active login uniqueness."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        _make_subscription(
            db_session,
            subscriber,
            offer,
            login="100008817",
            status=SubscriptionStatus.active,
        )
        duplicate = _make_subscription(
            db_session,
            subscriber,
            offer,
            login="100008817",
            status=SubscriptionStatus.pending,
        )
        suspend_subscription(
            db_session,
            str(duplicate.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )

        restored = restore_subscription(
            db_session,
            str(duplicate.id),
            trigger="payment",
            resolved_by="payment:pay-123",
            emit=False,
        )

        assert restored is False
        assert duplicate.status == SubscriptionStatus.suspended
        assert not has_active_lock(
            db_session, str(duplicate.id), EnforcementReason.overdue
        )
        assert subscriber.status == SubscriberStatus.active

    def test_dual_lock_partial_restore(self, db_session):
        """Overdue + FUP locks. Payment clears overdue but FUP remains."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="fup_rule:42",
            emit=False,
        )

        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-456",
            emit=False,
        )

        assert restored is False  # FUP lock still active
        assert subscription.status == SubscriptionStatus.suspended
        assert has_active_lock(db_session, str(subscription.id), EnforcementReason.fup)

    def test_dual_lock_full_restore(self, db_session):
        """Overdue + FUP both cleared → subscription restores."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="fup_rule:42",
            emit=False,
        )

        restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-1",
            emit=False,
        )
        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="cap_reset",
            resolved_by="fup_reset",
            emit=False,
        )

        assert restored is True
        assert subscription.status == SubscriptionStatus.active

    def test_unauthorized_restorer_rejected(self, db_session):
        """Payment cannot clear FUP lock."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="fup_rule:1",
            emit=False,
        )

        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-1",
            emit=False,
        )

        assert restored is False
        assert subscription.status == SubscriptionStatus.suspended

    def test_admin_lock_only_admin_can_clear(self, db_session):
        """Admin lock is not cleared by payment."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.admin,
            source="admin:user-1",
            emit=False,
        )

        # Payment fails to restore
        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-1",
            emit=False,
        )
        assert restored is False

        # Admin succeeds
        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="admin",
            resolved_by="admin:user-2",
            emit=False,
        )
        assert restored is True
        assert subscription.status == SubscriptionStatus.active

    def test_fraud_lock_only_admin_can_clear(self, db_session):
        """Fraud lock requires admin trigger."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fraud,
            source="fraud:investigation-1",
            emit=False,
        )

        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="admin",
            resolved_by="admin:user-1",
            emit=False,
        )
        assert restored is True


class TestTerminalStates:
    """Tests for expire and cancel operations."""

    def test_expire_resolves_locks(self, db_session):
        """Expiring a subscription with active locks resolves them all."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )

        expire_subscription(db_session, str(subscription.id), emit=False)

        assert subscription.status == SubscriptionStatus.expired
        assert not has_active_lock(db_session, str(subscription.id))

    def test_cancel_resolves_locks(self, db_session):
        """Canceling a subscription resolves all locks."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.admin,
            source="admin:user-1",
            emit=False,
        )

        cancel_subscription(
            db_session,
            str(subscription.id),
            cancel_reason="prepaid_deactivation",
            source="legacy_prepaid_balance",
            emit=False,
        )

        assert subscription.status == SubscriptionStatus.canceled
        assert subscription.cancel_reason == "prepaid_deactivation"
        assert not has_active_lock(db_session, str(subscription.id))

    def test_cancel_ends_active_addons(self, db_session):
        """Canceling a subscription ends its add-ons so they stop billing."""
        from app.models.catalog import AddOn, AddOnType, SubscriptionAddOn

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)
        add_on = AddOn(name="/29 IP", addon_type=AddOnType.extra_ip, is_active=True)
        db_session.add(add_on)
        db_session.flush()
        sub_addon = SubscriptionAddOn(
            subscription_id=subscription.id, add_on_id=add_on.id, quantity=1
        )
        db_session.add(sub_addon)
        db_session.commit()

        cancel_subscription(
            db_session,
            str(subscription.id),
            cancel_reason="prepaid_deactivation",
            source="test",
            emit=False,
        )

        db_session.refresh(sub_addon)
        assert sub_addon.end_at is not None


class TestActivateSubscription:
    """Tests for activate_subscription."""

    def test_activate_pending(self, db_session):
        """Pending subscription activates."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(
            db_session, subscriber, offer, status=SubscriptionStatus.pending
        )

        activate_subscription(db_session, str(subscription.id), emit=False)

        assert subscription.status == SubscriptionStatus.active
        assert subscription.start_at is not None
        assert subscriber.status == SubscriberStatus.active

    def test_cannot_activate_active(self, db_session):
        """Already active subscription raises ValueError."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        with pytest.raises(ValueError, match="Cannot activate"):
            activate_subscription(db_session, str(subscription.id), emit=False)


class TestComputeAccountStatus:
    """Tests for derived account status."""

    def test_mixed_active_and_suspended(self, db_session):
        """Sub A active, Sub B suspended → account active."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        sub_a = _make_subscription(db_session, subscriber, offer)
        sub_b = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(sub_b.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.active

    def test_all_suspended(self, db_session):
        """Both subs suspended → account suspended."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        sub_a = _make_subscription(db_session, subscriber, offer)
        sub_b = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(sub_a.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(sub_b.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.suspended

    def test_restore_one_of_two_suspended(self, db_session):
        """Restore one of two suspended subs → account active."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        sub_a = _make_subscription(db_session, subscriber, offer)
        sub_b = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(sub_a.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(sub_b.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )

        restore_subscription(
            db_session,
            str(sub_a.id),
            trigger="payment",
            resolved_by="payment:1",
            emit=False,
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.active

    def test_active_with_open_dunning_is_delinquent(self, db_session):
        """Active subscription + open dunning case → delinquent (durable,
        survives re-derivation). Resolving the case → back to active."""
        from app.models.collections import DunningCase, DunningCaseStatus

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        _make_subscription(db_session, subscriber, offer)

        case = DunningCase(account_id=subscriber.id, status=DunningCaseStatus.open)
        db_session.add(case)
        db_session.flush()

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.delinquent
        # delinquent keeps portal access to pay
        db_session.refresh(subscriber)
        assert subscriber.is_active is True

        # Re-deriving does not erase it (the previous bug).
        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.delinquent

        case.status = DunningCaseStatus.resolved
        db_session.flush()
        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.active

    def _add_open_ar_and_credit(self, db_session, subscriber, ar, credit):
        from decimal import Decimal

        from app.models.billing import (
            Invoice,
            InvoiceStatus,
            LedgerEntry,
            LedgerEntryType,
            LedgerSource,
        )

        db_session.add(
            Invoice(
                account_id=subscriber.id,
                currency="NGN",
                subtotal=Decimal(str(ar)),
                tax_total=Decimal("0"),
                total=Decimal(str(ar)),
                balance_due=Decimal(str(ar)),
                status=InvoiceStatus.overdue,
                is_active=True,
            )
        )
        db_session.add(
            LedgerEntry(
                account_id=subscriber.id,
                invoice_id=None,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal(str(credit)),
                currency="NGN",
                is_active=True,
            )
        )
        db_session.flush()

    def test_open_dunning_but_credit_covers_ar_is_active(self, db_session):
        """Open dunning case but available credit covers open AR → active, not
        delinquent. Status must not diverge from enforcement (which skips these
        as prepaid_balance_available)."""
        from app.models.collections import DunningCase, DunningCaseStatus

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)
        from tests.prepaid_funding_helpers import ensure_test_prepaid_contract

        ensure_test_prepaid_contract(db_session, subscription)
        self._add_open_ar_and_credit(db_session, subscriber, ar=5000, credit=8000)
        db_session.add(
            DunningCase(account_id=subscriber.id, status=DunningCaseStatus.open)
        )
        db_session.flush()

        assert (
            compute_account_status(db_session, str(subscriber.id))
            == SubscriberStatus.active
        )

    def test_open_dunning_with_insufficient_credit_is_delinquent(self, db_session):
        """Credit present but does NOT cover open AR → still delinquent."""
        from app.models.collections import DunningCase, DunningCaseStatus

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)
        from tests.prepaid_funding_helpers import ensure_test_prepaid_contract

        ensure_test_prepaid_contract(db_session, subscription)
        self._add_open_ar_and_credit(db_session, subscriber, ar=5000, credit=2000)
        db_session.add(
            DunningCase(account_id=subscriber.id, status=DunningCaseStatus.open)
        )
        db_session.flush()

        assert (
            compute_account_status(db_session, str(subscriber.id))
            == SubscriberStatus.delinquent
        )

    def test_prepaid_status_uses_threshold_not_nonnegative_net(self, db_session):
        """A positive net below the access threshold is still underfunded."""
        from decimal import Decimal

        from app.models.collections import DunningCase, DunningCaseStatus

        subscriber = _make_subscriber(
            db_session,
            billing_mode=BillingMode.prepaid,
            min_balance=Decimal("5000.00"),
        )
        offer = _make_offer(db_session)
        _make_subscription(db_session, subscriber, offer)
        # Net financial position is +1000: the old >= 0 criterion passed, but
        # the canonical prepaid threshold is 5000.
        self._add_open_ar_and_credit(db_session, subscriber, ar=5000, credit=6000)
        db_session.add(
            DunningCase(account_id=subscriber.id, status=DunningCaseStatus.open)
        )
        db_session.flush()

        assert (
            compute_account_status(db_session, str(subscriber.id))
            == SubscriberStatus.delinquent
        )

    def test_suspended_takes_precedence_over_dunning(self, db_session):
        """A suspended subscription outranks delinquent even with open dunning."""
        from app.models.collections import DunningCase, DunningCaseStatus

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        sub = _make_subscription(db_session, subscriber, offer)
        db_session.add(
            DunningCase(account_id=subscriber.id, status=DunningCaseStatus.open)
        )
        db_session.flush()
        suspend_subscription(
            db_session,
            str(sub.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )
        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.suspended

    def test_no_subscriptions(self, db_session):
        """Subscriber with no subscriptions → new."""
        subscriber = _make_subscriber(db_session)
        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.new

    def test_all_expired(self, db_session):
        """All subscriptions expired → canceled."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        sub = _make_subscription(db_session, subscriber, offer)

        expire_subscription(db_session, str(sub.id), emit=False)

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.canceled

    def test_pending_only(self, db_session):
        """Only pending subscriptions → new."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        _make_subscription(
            db_session, subscriber, offer, status=SubscriptionStatus.pending
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.new

    def test_pending_and_suspended_prefers_suspended(self, db_session):
        """A suspended service must not be masked by an unrelated pending one."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        active_sub = _make_subscription(db_session, subscriber, offer)
        _make_subscription(
            db_session, subscriber, offer, status=SubscriptionStatus.pending
        )

        suspend_subscription(
            db_session,
            str(active_sub.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.suspended


class TestDuplicateLockPrevention:
    """Tests for idempotent lock creation."""

    def test_duplicate_reason_returns_existing(self, db_session):
        """Suspending twice with same reason returns existing lock."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        lock1 = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        lock2 = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:2",
            emit=False,
        )

        assert lock1.id == lock2.id  # Same lock returned

    def test_existing_lock_repairs_active_subscription_and_parent(self, db_session):
        """Lock idempotency must not preserve active service-state drift."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)
        existing = EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.prepaid,
            access_mode=AccessRestrictionMode.captive,
            source="legacy-drift",
            is_active=True,
        )
        db_session.add(existing)
        db_session.flush()

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.prepaid,
            source="prepaid_balance_sweep",
            access_mode=AccessRestrictionMode.hard_reject,
            emit=False,
        )

        assert lock.id == existing.id
        assert lock.access_mode == AccessRestrictionMode.hard_reject
        assert subscription.status == SubscriptionStatus.suspended
        assert subscriber.status == SubscriberStatus.suspended
        assert (
            db_session.query(EnforcementLock)
            .filter(EnforcementLock.subscription_id == subscription.id)
            .count()
            == 1
        )

        repeated = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.prepaid,
            source="prepaid_balance_sweep",
            access_mode=AccessRestrictionMode.hard_reject,
            emit=False,
        )

        assert repeated.id == existing.id
        assert subscription.status == SubscriptionStatus.suspended
        assert subscriber.status == SubscriberStatus.suspended
        assert (
            db_session.query(EnforcementLock)
            .filter(EnforcementLock.subscription_id == subscription.id)
            .count()
            == 1
        )

    def test_existing_lock_status_repair_emits_suspension_once(
        self, db_session, monkeypatch
    ):
        """A repaired status emits its consequence, but never a duplicate lock."""
        emitted: list[EventType] = []

        def record_event(_db, event_type, _payload, **_kwargs):  # noqa: ANN001
            emitted.append(event_type)
            return None

        monkeypatch.setattr("app.services.account_lifecycle.emit_event", record_event)
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)
        existing = EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.prepaid,
            access_mode=AccessRestrictionMode.hard_reject,
            source="legacy-drift",
            is_active=True,
        )
        db_session.add(existing)
        db_session.flush()

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.prepaid,
            source="prepaid_balance_sweep",
            emit=True,
        )

        assert emitted == [EventType.subscription_suspended]

        emitted.clear()
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.prepaid,
            source="prepaid_balance_sweep",
            emit=True,
        )

        assert emitted == []


class TestRestoreWithReasonFilter:
    """Tests for restore_subscription with reason parameter."""

    def test_restore_specific_reason(self, db_session):
        """Only resolve locks matching the specified reason."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="test",
            emit=False,
        )

        # Resolve only FUP lock via admin
        restore_subscription(
            db_session,
            str(subscription.id),
            trigger="admin",
            resolved_by="admin:1",
            reason=EnforcementReason.fup,
            emit=False,
        )

        # Overdue lock still active
        assert has_active_lock(
            db_session, str(subscription.id), EnforcementReason.overdue
        )
        assert not has_active_lock(
            db_session, str(subscription.id), EnforcementReason.fup
        )
        assert subscription.status == SubscriptionStatus.suspended

    def test_restore_non_suspended_returns_false(self, db_session):
        """Restoring an active subscription returns False."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        result = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="test",
            emit=False,
        )
        assert result is False


class TestComputeAccountStatusEdgeCases:
    """Edge cases for derived account status."""

    def test_missing_subscriber_raises(self, db_session):
        """Non-existent subscriber raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            compute_account_status(db_session, str(uuid.uuid4()))


class TestEventEmission:
    """Tests that verify event emission (using emit=True)."""

    def test_suspend_emits_events(self, db_session, monkeypatch):
        """Suspend emits subscription_suspended + enforcement_lock_created."""
        emitted: list[tuple] = []
        original_emit = emit_event

        def mock_emit(db, event_type, payload, **kwargs):
            emitted.append((event_type, payload))
            return original_emit(db, event_type, payload, **kwargs)

        monkeypatch.setattr("app.services.account_lifecycle.emit_event", mock_emit)

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:1",
            emit=True,
        )

        event_types = [e[0] for e in emitted]
        assert EventType.subscription_suspended in event_types
        assert EventType.enforcement_lock_created in event_types

    def test_second_lock_emits_lock_created_only(self, db_session, monkeypatch):
        """Adding a second lock emits lock_created but NOT subscription_suspended."""
        emitted: list[tuple] = []
        original_emit = emit_event

        def mock_emit(db, event_type, payload, **kwargs):
            emitted.append((event_type, payload))
            return original_emit(db, event_type, payload, **kwargs)

        monkeypatch.setattr("app.services.account_lifecycle.emit_event", mock_emit)

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:1",
            emit=True,
        )
        emitted.clear()

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="test:2",
            emit=True,
        )

        event_types = [e[0] for e in emitted]
        assert EventType.enforcement_lock_created in event_types
        assert EventType.subscription_suspended not in event_types

    def test_restore_emits_resumed(self, db_session, monkeypatch):
        """Full restore emits subscription_resumed + enforcement_lock_resolved."""
        emitted: list[tuple] = []
        original_emit = emit_event

        def mock_emit(db, event_type, payload, **kwargs):
            emitted.append((event_type, payload))
            return original_emit(db, event_type, payload, **kwargs)

        monkeypatch.setattr("app.services.account_lifecycle.emit_event", mock_emit)

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:1",
            emit=False,
        )
        restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:1",
            emit=True,
        )

        event_types = [e[0] for e in emitted]
        assert EventType.enforcement_lock_resolved in event_types
        assert EventType.subscription_resumed in event_types

    def test_suspend_delivery_waits_for_commit(self, db_session, monkeypatch):
        """Lifecycle consequences cannot escape before the lock transaction."""
        dispatched: list[uuid.UUID] = []

        def record_dispatch(_self, _db, event_store_id):  # noqa: ANN001
            dispatched.append(event_store_id)
            return True

        monkeypatch.setattr(
            EventDispatcher,
            "dispatch_pending_event",
            record_dispatch,
        )
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:post-commit-suspend",
            emit=True,
        )

        assert dispatched == []
        db_session.commit()
        assert len(dispatched) == 2

    def test_rolled_back_suspend_has_no_delivery(self, db_session, monkeypatch):
        dispatched: list[uuid.UUID] = []

        def record_dispatch(_self, _db, event_store_id):  # noqa: ANN001
            dispatched.append(event_store_id)
            return True

        monkeypatch.setattr(
            EventDispatcher,
            "dispatch_pending_event",
            record_dispatch,
        )
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:rollback-suspend",
            emit=True,
        )
        db_session.rollback()

        assert dispatched == []

    def test_rolled_back_restore_has_no_delivery(self, db_session, monkeypatch):
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:restore-setup",
            emit=False,
        )
        db_session.commit()
        dispatched: list[uuid.UUID] = []

        def record_dispatch(_self, _db, event_store_id):  # noqa: ANN001
            dispatched.append(event_store_id)
            return True

        monkeypatch.setattr(
            EventDispatcher,
            "dispatch_pending_event",
            record_dispatch,
        )

        assert restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="test:rollback-restore",
            emit=True,
        )
        db_session.rollback()

        assert dispatched == []


class TestAllowedRestorers:
    """Verify the restorer map is complete and sensible."""

    def test_all_reasons_have_restorers(self):
        """Every enforcement reason has an entry in ALLOWED_RESTORERS."""
        for reason in EnforcementReason:
            assert reason in ALLOWED_RESTORERS, f"Missing restorer for {reason}"

    def test_admin_can_restore_all(self):
        """Admin trigger can resolve every enforcement reason."""
        for reason, triggers in ALLOWED_RESTORERS.items():
            assert "admin" in triggers, f"Admin cannot restore {reason}"

    def test_payment_cannot_clear_prepaid_lock(self):
        """Prepaid restoration requires the funding-gated top-up owner."""
        triggers = ALLOWED_RESTORERS[EnforcementReason.prepaid]

        assert "payment" not in triggers
        assert "top_up" in triggers
