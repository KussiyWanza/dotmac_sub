"""Tests for the truthful /me/service-status builder.

Service expiry is status/balance-driven, not date-driven: prepaid lapses on
balance exhaustion (grace/deactivation timers), postpaid only via dunning.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import SubscriberStatus
from app.schemas.billing import InvoiceCreate
from app.schemas.service_status import ServiceStatusActionKind
from app.services import billing as billing_service
from app.services.collections.grace_policy import resolve_grace_decision
from app.services.service_status import build_service_status
from tests.prepaid_funding_helpers import ensure_test_prepaid_contract


def _n(dt):
    # SQLite (test DB) drops tzinfo on tz-aware columns; Postgres keeps it.
    # Normalise so assertions compare the instant, not the tz attribute.
    return dt.replace(tzinfo=None) if dt is not None else None


def _activate(db, subscription, mode):
    now = datetime.now(UTC)
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = mode
    subscription.start_at = subscription.start_at or now - timedelta(days=30)
    subscription.next_billing_at = subscription.next_billing_at or now
    if mode == BillingMode.prepaid:
        ensure_test_prepaid_contract(
            db,
            subscription,
            amount=subscription.unit_price or Decimal("100.00"),
        )
    db.commit()


def _add_lock(db, subscriber, subscription, reason):
    db.add(
        EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=reason,
            source=f"test:{reason.value}",
        )
    )
    db.commit()


def test_postpaid_active_no_overdue_is_ok_with_no_expiry(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.next_billing_at = datetime.now(UTC) - timedelta(days=3)
    _activate(db_session, subscription, BillingMode.postpaid)

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.billing_mode == "postpaid"
    assert resp.in_dunning is False
    assert resp.outstanding == Decimal("0.00")
    assert len(resp.services) == 1
    svc = resp.services[0]
    assert svc.usable is True
    assert svc.reason == "ok"
    assert svc.status_presentation.model_dump(mode="json") == {
        "value": "active",
        "label": "Active",
        "tone": "positive",
        "icon": "check",
    }
    # A stale next_billing_at must NOT become an expiry for postpaid.
    assert svc.expires_at is None
    assert svc.next_charge_at == subscription.next_billing_at


def test_postpaid_overdue_invoice_flags_dunning(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    _activate(db_session, subscription, BillingMode.postpaid)
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            status=InvoiceStatus.issued,
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:service-status",
            due_date_policy_version="test-v1",
        ),
    )
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.in_dunning is True
    assert resp.outstanding == Decimal("5000.00")
    assert resp.oldest_overdue_due_at is not None
    assert resp.services[0].reason == "overdue"
    assert resp.services[0].action is not None
    assert resp.services[0].action.kind == ServiceStatusActionKind.pay_invoices
    assert resp.services[0].action.amount == Decimal("5000.00")
    assert resp.services[0].action.restores_service is False
    assert resp.primary_action == resp.services[0].action


def test_prepaid_healthy_balance_is_ok_no_expiry(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = Decimal("100.00")
    _activate(db_session, subscription, BillingMode.prepaid)
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("500.00"),
            currency="NGN",
            memo="top-up",
        )
    )
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.billing_mode == "prepaid"
    assert resp.balance == Decimal("500.00")
    assert resp.low_balance is False
    assert resp.grace_until is None
    svc = resp.services[0]
    assert svc.reason == "ok"
    assert svc.expires_at is None


def test_prepaid_insufficient_wallet_without_paid_coverage_is_low_balance(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = None
    subscription.unit_price = Decimal("17500.00")
    _activate(db_session, subscription, BillingMode.prepaid)
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("543.00"),
            currency="NGN",
            memo="remaining wallet",
        )
    )
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.balance == Decimal("543.00")
    assert resp.min_balance == Decimal("17500.00")
    assert resp.low_balance is True
    assert resp.services[0].reason == "low_balance"
    assert resp.services[0].action is not None
    assert resp.services[0].action.kind == ServiceStatusActionKind.top_up
    assert resp.services[0].action.amount == Decimal("16957.00")
    assert resp.services[0].action.restores_service is False


def test_prepaid_low_wallet_with_paid_invoice_is_protected_pending_projection(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = None
    subscription.unit_price = Decimal("17500.00")
    _activate(db_session, subscription, BillingMode.prepaid)
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-STATUS-FUNDED-PREPAID",
        status=InvoiceStatus.paid,
        total=Decimal("17500.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=datetime.now(UTC) - timedelta(days=10),
        billing_period_end=datetime.now(UTC) + timedelta(days=20),
        issued_at=datetime.now(UTC) - timedelta(days=10),
        paid_at=datetime.now(UTC) - timedelta(days=10),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Funded prepaid renewal",
            quantity=Decimal("1.000"),
            unit_price=Decimal("17500.00"),
            amount=Decimal("17500.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.balance == Decimal("0.00")
    assert resp.min_balance == Decimal("0.00")
    assert resp.low_balance is False
    assert resp.services[0].reason == "ok"


def test_prepaid_low_wallet_with_active_entitlement_is_ok(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = None
    subscription.unit_price = Decimal("17500.00")
    _activate(db_session, subscription, BillingMode.prepaid)
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            starts_at=datetime.now(UTC) - timedelta(days=10),
            ends_at=datetime.now(UTC) + timedelta(days=20),
            amount_funded=Decimal("17500.00"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
        )
    )
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.balance == Decimal("0.00")
    assert resp.min_balance == Decimal("0.00")
    assert resp.low_balance is False
    assert resp.services[0].reason == "ok"


def test_future_prepaid_entitlement_does_not_cover_current_period(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = None
    subscription.unit_price = Decimal("17500.00")
    _activate(db_session, subscription, BillingMode.prepaid)
    now = datetime.now(UTC)
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            starts_at=now + timedelta(days=5),
            ends_at=now + timedelta(days=35),
            amount_funded=Decimal("17500.00"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
        )
    )
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.balance == Decimal("0.00")
    assert resp.min_balance == Decimal("17500.00")
    assert resp.low_balance is True
    assert resp.services[0].reason == "low_balance"


def test_future_paid_prepaid_invoice_does_not_cover_current_period(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = None
    subscription.unit_price = Decimal("17500.00")
    _activate(db_session, subscription, BillingMode.prepaid)
    now = datetime.now(UTC)
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-STATUS-FUTURE-FUNDED-PREPAID",
        status=InvoiceStatus.paid,
        total=Decimal("17500.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=now + timedelta(days=5),
        billing_period_end=now + timedelta(days=35),
        issued_at=now,
        paid_at=now,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Future funded prepaid renewal",
            quantity=Decimal("1.000"),
            unit_price=Decimal("17500.00"),
            amount=Decimal("17500.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.balance == Decimal("0.00")
    assert resp.min_balance == Decimal("17500.00")
    assert resp.low_balance is True
    assert resp.services[0].reason == "low_balance"


def test_prepaid_low_balance_surfaces_grace_as_expiry(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = Decimal("100.00")
    subscriber_account.grace_period_days = 3
    low_at = datetime.now(UTC) - timedelta(days=1)
    subscriber_account.prepaid_low_balance_at = low_at
    _activate(db_session, subscription, BillingMode.prepaid)  # balance 0 < 100

    resp = build_service_status(db_session, str(subscriber_account.id))
    grace = resolve_grace_decision(
        db_session,
        subscriber_account,
        starts_at=low_at,
        as_of=resp.as_of,
    )

    assert resp.low_balance is True
    assert _n(resp.grace_until) == _n(grace.ends_at)
    svc = resp.services[0]
    assert svc.reason == "low_balance"
    # The real pending lapse is when grace ends (then suspension), not a bill date.
    assert _n(svc.expires_at) == _n(grace.ends_at)


def test_contract_end_at_always_wins_as_expiry(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    end = datetime.now(UTC) + timedelta(days=20)
    subscription.end_at = end
    _activate(db_session, subscription, BillingMode.postpaid)

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert _n(resp.services[0].expires_at) == _n(end)


def test_ended_subscriptions_are_excluded(db_session, subscriber_account, subscription):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.canceled
    subscription.canceled_at = datetime.now(UTC)
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.services == []


def test_overdue_lock_offers_exact_payment_that_restores_service(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscriber_account.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.suspended
    subscription.billing_mode = BillingMode.postpaid
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            status=InvoiceStatus.issued,
            total=Decimal("6500.00"),
            balance_due=Decimal("6500.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:service-status",
            due_date_policy_version="test-v1",
        ),
    )
    _add_lock(
        db_session,
        subscriber_account,
        subscription,
        EnforcementReason.overdue,
    )

    resp = build_service_status(db_session, str(subscriber_account.id))

    action = resp.services[0].action
    assert resp.services[0].reason == "overdue"
    assert action is not None
    assert action.kind == ServiceStatusActionKind.pay_invoices
    assert action.amount == Decimal("6500.00")
    assert action.restores_service is True
    assert "NGN 6,500.00" in action.message
    assert resp.primary_action == action
    payload = resp.model_dump(mode="json")
    assert payload["services"][0]["status_presentation"] == {
        "value": "suspended",
        "label": "Suspended",
        "tone": "warning",
        "icon": "alert",
    }
    assert payload["primary_action"]["kind"] == "pay_invoices"
    assert payload["primary_action"]["amount"] == "6500.00"
    assert payload["primary_action"]["restores_service"] is True


def test_manual_suspension_never_claims_payment_will_restore_service(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscriber_account.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.suspended
    subscription.billing_mode = BillingMode.postpaid
    _add_lock(
        db_session,
        subscriber_account,
        subscription,
        EnforcementReason.admin,
    )

    resp = build_service_status(db_session, str(subscriber_account.id))

    action = resp.services[0].action
    assert resp.services[0].reason == "administrative_hold"
    assert action is not None
    assert action.kind == ServiceStatusActionKind.contact_support
    assert action.amount is None
    assert action.restores_service is False
    assert "payment cannot clear" in action.message
    assert resp.primary_action == action


def test_multiple_holds_do_not_offer_partial_payment_as_restoration(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscriber_account.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.suspended
    subscription.billing_mode = BillingMode.postpaid
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            status=InvoiceStatus.issued,
            total=Decimal("8000.00"),
            balance_due=Decimal("8000.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:service-status",
            due_date_policy_version="test-v1",
        ),
    )
    for reason in (EnforcementReason.overdue, EnforcementReason.admin):
        _add_lock(db_session, subscriber_account, subscription, reason)

    resp = build_service_status(db_session, str(subscriber_account.id))

    action = resp.services[0].action
    assert resp.services[0].reason == "multiple_holds"
    assert action is not None
    assert action.kind == ServiceStatusActionKind.contact_support
    assert action.amount is None
    assert action.restores_service is False
    assert "payment alone will not restore" in action.message
