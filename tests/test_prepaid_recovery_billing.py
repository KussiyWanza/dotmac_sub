from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.services.owner_commands import CommandContext
from app.services.prepaid_recovery_billing import (
    PrepaidRecoveryBillingError,
    PrepaidRecoveryDraftConfirmation,
    PrepaidRecoveryNextAction,
    create_prepaid_recovery_draft,
    preview_prepaid_recovery_draft,
    resolve_prepaid_recovery_draft_eligibility,
)


def _suspend_for_prepaid(db, subscriber, subscription) -> None:
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.suspended
    db.add(
        EnforcementLock(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            reason=EnforcementReason.prepaid,
            source="pytest:prepaid-recovery",
            is_active=True,
        )
    )
    db.commit()


def _service_draft(db, subscriber, subscription) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-RECOVERY-GUARD-{uuid4().hex[:8]}",
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("4317.29"),
        tax_total=Decimal("0.00"),
        total=Decimal("4317.29"),
        balance_due=Decimal("4317.29"),
        billing_period_start=datetime(2026, 8, 2, tzinfo=UTC),
        billing_period_end=datetime(2026, 9, 2, tzinfo=UTC),
        is_proforma=False,
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Ordinary prepaid cycle",
            quantity=Decimal("1.000"),
            unit_price=Decimal("4317.29"),
            amount=Decimal("4317.29"),
            is_active=True,
        )
    )
    db.commit()
    return invoice


def test_bill_now_routes_to_existing_ordinary_prepaid_draft(
    db_session, subscriber, subscription
):
    _suspend_for_prepaid(db_session, subscriber, subscription)
    invoice = _service_draft(db_session, subscriber, subscription)

    eligibility = resolve_prepaid_recovery_draft_eligibility(
        db_session, subscription_id=subscription.id
    )

    assert eligibility.eligible is False
    assert eligibility.existing_invoice_id == invoice.id
    assert (
        eligibility.next_action is PrepaidRecoveryNextAction.reconcile_existing_invoice
    )
    with pytest.raises(PrepaidRecoveryBillingError) as exc_info:
        preview_prepaid_recovery_draft(db_session, subscription_id=subscription.id)
    assert exc_info.value.code.endswith(".unresolved_service_invoice")
    assert exc_info.value.details == {
        "subscription_id": str(subscription.id),
        "next_action": "reconcile_existing_invoice",
        "invoice_id": str(invoice.id),
        "invoice_ids": (str(invoice.id),),
    }


def test_bill_now_marks_existing_draft_with_ledger_activity_for_review(
    db_session, subscriber, subscription
):
    _suspend_for_prepaid(db_session, subscriber, subscription)
    invoice = _service_draft(db_session, subscriber, subscription)
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            invoice_id=invoice.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("17.43"),
            currency="NGN",
            memo="Existing reviewed financial evidence",
            is_active=True,
            effective_date=datetime(2026, 8, 2, tzinfo=UTC),
        )
    )
    db_session.commit()

    eligibility = resolve_prepaid_recovery_draft_eligibility(
        db_session, subscription_id=subscription.id
    )

    assert eligibility.existing_invoice_id == invoice.id
    assert eligibility.next_action is PrepaidRecoveryNextAction.review_existing_invoice


def test_bill_now_does_not_block_on_another_subscription_draft(
    db_session, subscriber, subscription, catalog_offer
):
    _suspend_for_prepaid(db_session, subscriber, subscription)
    other = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(other)
    db_session.commit()
    _service_draft(db_session, subscriber, other)

    eligibility = resolve_prepaid_recovery_draft_eligibility(
        db_session, subscription_id=subscription.id
    )

    assert eligibility.eligible is True
    assert eligibility.existing_invoice_id is None
    assert eligibility.next_action is PrepaidRecoveryNextAction.create_recovery_draft


def test_bill_now_confirmation_replays_matching_recovery_draft(
    db_session, subscriber, subscription, monkeypatch
):
    _suspend_for_prepaid(db_session, subscriber, subscription)
    subscription.unit_price = Decimal("4582.19")
    subscription.billing_cycle = BillingCycle.monthly
    db_session.commit()
    monkeypatch.setattr(
        "app.services.prepaid_recovery_billing.resolve_prepaid_monthly_charge",
        lambda *_args: (Decimal("4582.19"), "NGN", BillingCycle.monthly),
    )
    preview = preview_prepaid_recovery_draft(
        db_session,
        subscription_id=subscription.id,
        effective_at=datetime(2026, 8, 2, 9, 17, tzinfo=UTC),
    )
    confirmation = PrepaidRecoveryDraftConfirmation(
        subscription_id=subscription.id,
        starts_at=preview.starts_at,
        fingerprint=preview.fingerprint,
    )
    context = CommandContext.system(
        actor="pytest:billing-operator",
        scope="billing:invoice:update",
        reason="Reviewed recovery draft",
        idempotency_key=f"pytest-recovery-draft:{subscription.id}",
    )
    db_session.commit()

    first = create_prepaid_recovery_draft(
        db_session, context=context, confirmation=confirmation
    )
    subscription.unit_price = Decimal("4691.23")
    db_session.commit()
    replay = create_prepaid_recovery_draft(
        db_session, context=context, confirmation=confirmation
    )

    assert replay.replayed is True
    assert replay.invoice_id == first.invoice_id
    assert db_session.query(Invoice).filter(Invoice.id == first.invoice_id).count() == 1
