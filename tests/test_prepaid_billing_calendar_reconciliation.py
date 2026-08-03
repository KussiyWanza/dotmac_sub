from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.event_store import EventStore
from app.models.idempotency import IdempotencyKey
from app.models.usage import QuotaBucket
from app.services.owner_commands import CommandContext
from app.services.prepaid_billing_calendar_reconciliation import (
    PrepaidBillingCalendarDisposition,
    ReconcilePrepaidBillingCalendarCommand,
    preview_prepaid_billing_calendar_cohort,
    preview_prepaid_billing_calendar_reconciliation,
    reconcile_prepaid_billing_calendar,
)

PAID_AT = datetime(2026, 7, 6, 12, 30, tzinfo=UTC)
LEGACY_START = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
LEGACY_END = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)
WAT_START = datetime(2026, 7, 5, 23, 0, tzinfo=UTC)
WAT_END = datetime(2026, 8, 5, 23, 0, tzinfo=UTC)


def _chain(db, subscriber):
    offer = CatalogOffer(
        name=f"Calendar repair {uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        status=OfferStatus.active,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        unit_price=Decimal("1000.00"),
        next_billing_at=LEGACY_END,
    )
    db.add(subscription)
    db.flush()
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-{uuid4().hex[:8]}",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("1000.00"),
        total=Decimal("1000.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=LEGACY_START,
        billing_period_end=LEGACY_END,
        issued_at=LEGACY_START,
        paid_at=PAID_AT,
        is_proforma=False,
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description="Monthly prepaid service",
        quantity=Decimal("1.000"),
        unit_price=Decimal("1000.00"),
        amount=Decimal("1000.00"),
        metadata_={
            "kind": "base_subscription",
            "billing_period_start": LEGACY_START.isoformat(),
            "billing_period_end": LEGACY_END.isoformat(),
        },
        is_active=True,
    )
    db.add(line)
    db.flush()
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("1000.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=PAID_AT,
        created_at=PAID_AT,
        is_active=True,
    )
    db.add(payment)
    db.flush()
    db.add_all(
        [
            PaymentSettlement(
                payment_id=payment.id,
                amount=Decimal("1000.00"),
                unallocated_amount=Decimal("0.00"),
                prepaid_amount=Decimal("1000.00"),
                currency="NGN",
                origin=PaymentSettlementOrigin.system,
                idempotency_key=f"calendar-test-settlement-{payment.id}",
                created_at=PAID_AT,
            ),
            PaymentAllocation(
                payment_id=payment.id,
                invoice_id=invoice.id,
                amount=Decimal("1000.00"),
                idempotency_key=f"calendar-test-allocation-{payment.id}",
                is_active=True,
            ),
        ]
    )
    entitlement = ServiceEntitlement(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        source_invoice_id=invoice.id,
        source_invoice_line_id=line.id,
        starts_at=LEGACY_START,
        ends_at=LEGACY_END,
        amount_funded=Decimal("1000.00"),
        currency="NGN",
        status=ServiceEntitlementStatus.active,
    )
    db.add(entitlement)
    db.commit()
    return invoice, line, subscription, entitlement, payment


def _command(invoice_id, fingerprint, *, key="calendar-repair-test"):
    command_id = uuid4()
    return ReconcilePrepaidBillingCalendarCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="user:test-finance-manager",
            scope="billing:reconciliation:write",
            reason="Reviewed UTC-to-WAT historical calendar correction.",
            idempotency_key=key,
        ),
        invoice_id=invoice_id,
        preview_fingerprint=fingerprint,
    )


def test_exact_legacy_chain_previews_and_reconciles_without_economic_change(
    db_session, subscriber
):
    invoice, line, subscription, entitlement, payment = _chain(db_session, subscriber)
    preview = preview_prepaid_billing_calendar_reconciliation(db_session, invoice.id)

    assert preview.disposition is PrepaidBillingCalendarDisposition.eligible
    assert preview.current_starts_at == LEGACY_START
    assert preview.current_ends_at == LEGACY_END
    assert preview.proposed_starts_at == WAT_START
    assert preview.proposed_ends_at == WAT_END
    assert preview.proposed_starts_on == "2026-07-06"
    assert preview.proposed_ends_on == "2026-08-06"

    invoice_id = invoice.id
    db_session.commit()
    result = reconcile_prepaid_billing_calendar(
        db_session, _command(invoice_id, preview.fingerprint)
    )

    assert result.replayed is False
    assert result.corrected_ends_at == WAT_END
    db_session.refresh(invoice)
    db_session.refresh(line)
    db_session.refresh(subscription)
    db_session.refresh(entitlement)
    db_session.refresh(payment)
    assert invoice.billing_period_start.replace(tzinfo=UTC) == WAT_START
    assert invoice.billing_period_end.replace(tzinfo=UTC) == WAT_END
    assert subscription.next_billing_at.replace(tzinfo=UTC) == WAT_END
    assert entitlement.starts_at.replace(tzinfo=UTC) == WAT_START
    assert entitlement.ends_at.replace(tzinfo=UTC) == WAT_END
    assert line.metadata_["billing_period_end"] == WAT_END.isoformat()
    assert invoice.total == Decimal("1000.00")
    assert invoice.status is InvoiceStatus.paid
    assert payment.amount == Decimal("1000.00")
    evidence = invoice.metadata_["prepaid_billing_calendar_reconciliation"]
    assert evidence["economic_delta"] == "0.00"
    assert evidence["payment_id"] == str(payment.id)
    assert (
        db_session.query(AuditEvent)
        .filter_by(
            action="reconcile_prepaid_billing_calendar", entity_id=str(invoice.id)
        )
        .one()
    )
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="prepaid_billing_calendar.reconciled")
        .one()
    )
    assert (
        db_session.query(IdempotencyKey)
        .filter_by(
            scope="prepaid_billing_calendar_reconciliation",
            key="calendar-repair-test",
        )
        .one()
    )


def test_identical_command_replays_from_durable_invoice_evidence(
    db_session, subscriber
):
    invoice, *_ = _chain(db_session, subscriber)
    preview = preview_prepaid_billing_calendar_reconciliation(db_session, invoice.id)
    invoice_id = invoice.id
    db_session.commit()
    command = _command(invoice_id, preview.fingerprint, key="calendar-repair-replay")

    first = reconcile_prepaid_billing_calendar(db_session, command)
    second = reconcile_prepaid_billing_calendar(db_session, command)

    assert first.replayed is False
    assert second.replayed is True
    assert second.corrected_ends_at == first.corrected_ends_at
    assert (
        db_session.query(AuditEvent)
        .filter_by(
            action="reconcile_prepaid_billing_calendar", entity_id=str(invoice.id)
        )
        .count()
        == 1
    )


def test_moved_anchor_is_quarantined(db_session, subscriber):
    invoice, _line, subscription, *_ = _chain(db_session, subscriber)
    subscription.next_billing_at = datetime(2026, 9, 6, tzinfo=UTC)
    db_session.commit()

    preview = preview_prepaid_billing_calendar_reconciliation(db_session, invoice.id)

    assert preview.disposition is PrepaidBillingCalendarDisposition.anchor_changed
    assert preview.actionable is False


def test_overlapping_entitlement_is_quarantined(db_session, subscriber):
    invoice, _line, subscription, *_ = _chain(db_session, subscriber)
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber.id,
            subscription_id=subscription.id,
            starts_at=datetime(2026, 8, 5, 22, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 5, 22, 0, tzinfo=UTC),
            amount_funded=Decimal("1000.00"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
        )
    )
    db_session.commit()

    preview = preview_prepaid_billing_calendar_reconciliation(db_session, invoice.id)

    assert (
        preview.disposition is PrepaidBillingCalendarDisposition.overlapping_entitlement
    )
    assert preview.actionable is False


def test_partial_payment_allocation_is_quarantined(db_session, subscriber):
    invoice, _line, _subscription, _entitlement, payment = _chain(
        db_session, subscriber
    )
    allocation = (
        db_session.query(PaymentAllocation)
        .filter_by(invoice_id=invoice.id, payment_id=payment.id)
        .one()
    )
    allocation.amount = Decimal("999.00")
    db_session.commit()

    preview = preview_prepaid_billing_calendar_reconciliation(db_session, invoice.id)

    assert preview.disposition is PrepaidBillingCalendarDisposition.ambiguous_payment
    assert preview.actionable is False


def test_overlapping_usage_period_is_quarantined(db_session, subscriber):
    invoice, _line, subscription, *_ = _chain(db_session, subscriber)
    db_session.add(
        QuotaBucket(
            subscription_id=subscription.id,
            period_start=LEGACY_START,
            period_end=LEGACY_END,
            included_gb=Decimal("100.00"),
            used_gb=Decimal("10.00"),
            rollover_gb=Decimal("0.00"),
            topup_gb=Decimal("0.00"),
            overage_gb=Decimal("0.00"),
        )
    )
    db_session.commit()

    preview = preview_prepaid_billing_calendar_reconciliation(db_session, invoice.id)

    assert preview.disposition is PrepaidBillingCalendarDisposition.usage_period_present
    assert preview.actionable is False


def test_cohort_paginates_without_stranding_later_candidates(db_session, subscriber):
    first_invoice, *_ = _chain(db_session, subscriber)
    second_invoice, *_ = _chain(db_session, subscriber)

    first_page = preview_prepaid_billing_calendar_cohort(db_session, limit=1, offset=0)
    second_page = preview_prepaid_billing_calendar_cohort(db_session, limit=1, offset=1)

    assert first_page.has_previous is False
    assert first_page.has_more is True
    assert second_page.has_previous is True
    assert {item.invoice_id for item in first_page.previews + second_page.previews} == {
        first_invoice.id,
        second_invoice.id,
    }


def test_non_legacy_invoice_period_is_not_a_candidate(db_session, subscriber):
    invoice, *_ = _chain(db_session, subscriber)
    invoice.billing_period_start = WAT_START
    invoice.billing_period_end = WAT_END
    db_session.commit()

    preview = preview_prepaid_billing_calendar_reconciliation(db_session, invoice.id)

    assert (
        preview.disposition
        is PrepaidBillingCalendarDisposition.period_signature_mismatch
    )
    assert preview.actionable is False
    cohort = preview_prepaid_billing_calendar_cohort(db_session)
    assert all(item.invoice_id != invoice.id for item in cohort.previews)
