"""Payment allocation must reach the prepaid consequence owner.

`financial.payments` ends after confirmed cash, invoice allocation, and
unallocated-credit evidence are committed. The durable `payment.received` event
invokes `financial.prepaid_service_renewals`, which is the sole owner of prepaid
period funding, entitlements, and billing-anchor advancement.

Before this, `PaymentAllocations` emitted no event at all, so the standalone
credit-allocation path created entitlements but never advanced
`Subscription.next_billing_at` — and the account was suspended again for service
it had already paid for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.schemas.billing import (
    PaymentAllocationConfirm,
    PaymentAllocationPreviewRequest,
)
from app.services.billing.payments import PaymentAllocations
from app.services.events.handlers.prepaid_renewal import PrepaidRenewalHandler
from app.services.events.types import Event, EventType
from app.services.prepaid_service_renewals import (
    BillingAnchorAuthority,
    _utc,
    apply_stale_prepaid_billing_anchor_repair,
    preview_stale_prepaid_billing_anchor_repair,
    project_prepaid_billing_anchor_for_invoice,
    retract_prepaid_billing_anchors_after_funding_reversal,
)
from app.services.service_entitlements import (
    revoke_prepaid_entitlements_for_unpaid_invoice,
)


def at(value: datetime | None) -> datetime | None:
    """Normalize one timestamp before comparing it.

    `Subscription.next_billing_at` and `ServiceEntitlement.ends_at` are
    ``DateTime(timezone=True)`` columns, so Postgres returns them aware while
    the SQLite test backend returns them naive. Comparing a freshly written
    aware value against a naive round-tripped one raises TypeError and hides
    real assertions behind a harness artefact.

    This reuses the owner's own boundary normalizer rather than sprinkling
    ``replace(tzinfo=UTC)`` at each assertion, so the tests agree with exactly
    the coercion the product applies.
    """
    return None if value is None else _utc(value)


PERIOD_START = datetime(2026, 7, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 8, 1, tzinfo=UTC)
# Settled on the first day of the funded period, so the invoice-level
# "lapsed prepaid re-anchor" repair deliberately does not fire: this test must
# prove the event-driven owner advanced the anchor, not that repair did.
PAID_AT = PERIOD_START
AMOUNT = Decimal("18000.00")


def _prepaid_invoice(db, account, subscription, *, amount: Decimal = AMOUNT) -> Invoice:
    """One issued prepaid invoice whose subscription anchor is already due."""
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = PERIOD_START
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-ALLOC-{uuid4().hex[:8]}",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=amount,
        tax_total=Decimal("0.00"),
        total=amount,
        balance_due=amount,
        billing_period_start=PERIOD_START,
        billing_period_end=PERIOD_END,
        issued_at=PERIOD_START,
        due_at=PERIOD_START,
        is_proforma=False,
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid service",
            quantity=Decimal("1.000"),
            unit_price=amount,
            amount=amount,
            metadata_={
                "kind": "base_subscription",
                "billing_period_start": PERIOD_START.isoformat(),
                "billing_period_end": PERIOD_END.isoformat(),
            },
            is_active=True,
        )
    )
    db.commit()
    return invoice


def _settled_payment(db, account, *, amount: Decimal = AMOUNT) -> Payment:
    """A succeeded payment whose cash is sitting as unallocated account credit."""
    payment = Payment(
        account_id=account.id,
        amount=amount,
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=PAID_AT,
        is_active=True,
    )
    db.add(payment)
    db.flush()
    entry = LedgerEntry(
        account_id=account.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=amount,
        currency="NGN",
        memo="Settled test payment",
        is_active=True,
    )
    db.add(entry)
    db.flush()
    db.add(
        PaymentSettlement(
            payment_id=payment.id,
            unallocated_ledger_entry_id=entry.id,
            amount=amount,
            unallocated_amount=amount,
            prepaid_amount=Decimal("0.00"),
            currency="NGN",
            origin=PaymentSettlementOrigin.system,
            idempotency_key=f"pytest-allocation-settlement-{payment.id}",
        )
    )
    db.commit()
    return payment


def _allocation_key(payment: Payment, invoice: Invoice) -> str:
    return f"pytestalloc{payment.id.hex}{invoice.id.hex}"[:64]


def _allocate(db, payment: Payment, invoice: Invoice, *, amount: Decimal = AMOUNT):
    request = PaymentAllocationPreviewRequest(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=amount,
    )
    preview = PaymentAllocations.preview(db, request)
    return PaymentAllocations.confirm(
        db,
        PaymentAllocationConfirm(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=_allocation_key(payment, invoice),
        ),
    )


def _allocation_funding_events(db) -> list[EventStore]:
    return [
        record
        for record in db.query(EventStore)
        .filter(EventStore.event_type == EventType.payment_received.value)
        .all()
        if (record.payload or {}).get("source") == "payment_allocation"
    ]


def _funding_event(db, account, invoice) -> Event:
    """Rebuild the durable event the allocation staged, for handler dispatch."""
    records = _allocation_funding_events(db)
    assert len(records) == 1, "expected exactly one allocation funding-change event"
    record = records[0]
    return Event(
        event_type=EventType.payment_received,
        payload=record.payload,
        event_id=record.event_id,
        account_id=account.id,
        invoice_id=invoice.id,
    )


def _entitlement(db, invoice) -> ServiceEntitlement:
    return (
        db.query(ServiceEntitlement)
        .filter(ServiceEntitlement.source_invoice_id == invoice.id)
        .one()
    )


def test_payment_allocation_emits_the_durable_funding_change_event(
    db_session, subscriber, subscription
):
    """The allocation path must reach the prepaid consequence owner at all."""
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)

    result = _allocate(db_session, payment, invoice)

    db_session.refresh(invoice)
    assert result.allocation.amount == AMOUNT
    assert invoice.status is InvoiceStatus.paid

    records = _allocation_funding_events(db_session)
    assert len(records) == 1, "the allocation path emitted no funding-change event"
    payload = records[0].payload
    assert payload["payment_id"] == str(payment.id)
    assert payload["invoice_id"] == str(invoice.id)
    assert payload["allocation_id"] == str(result.allocation.id)
    assert payload["settlement_id"]
    # `emit_event(..., account_id=...)` populates `EventStore.account_id`; it is
    # the field `PrepaidRenewalHandler` routes on, so assert the routing key the
    # consequence owner actually reads.
    assert records[0].account_id == subscriber.id
    assert records[0].invoice_id == invoice.id


def test_paid_allocation_produces_entitlement_and_advanced_anchor(
    db_session, subscriber, subscription
):
    """The full behaviour: exact entitlement AND an exact billing anchor."""
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)

    entitlement = _entitlement(db_session, invoice)
    assert entitlement.status is ServiceEntitlementStatus.active
    assert entitlement.amount_funded == AMOUNT

    PrepaidRenewalHandler().handle(
        db_session, _funding_event(db_session, subscriber, invoice)
    )
    db_session.commit()

    db_session.refresh(subscription)
    assert at(subscription.next_billing_at) == at(entitlement.ends_at), (
        "the prepaid renewal owner did not advance the billing anchor to the "
        "exact funded coverage end"
    )
    assert at(subscription.next_billing_at) > PERIOD_START


def test_settled_service_survives_a_later_prepaid_sweep(
    db_session, subscriber, subscription
):
    """After settlement the account must stay covered, not be re-suspended."""
    from app.services.prepaid_service_coverage import (
        PrepaidCoverageStatus,
        resolve_prepaid_service_coverage,
    )

    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    PrepaidRenewalHandler().handle(
        db_session, _funding_event(db_session, subscriber, invoice)
    )
    db_session.commit()
    db_session.refresh(subscription)

    inside_period = PERIOD_START + timedelta(days=10)
    coverage = resolve_prepaid_service_coverage(
        db_session, [subscription], as_of=inside_period
    )

    decision = coverage[subscription.id]
    assert decision.status is PrepaidCoverageStatus.covered
    assert decision.covered
    assert at(subscription.next_billing_at) > inside_period


def test_enforcement_guard_blocks_adverse_action_on_a_stale_anchor(
    db_session, subscriber, subscription
):
    """A current exact entitlement must veto adverse action, anchor or not.

    This is the release guard: even if the anchor projection is stale or has
    not caught up, an entitlement covering *now* is positive coverage evidence
    and no adverse enforcement may follow from the anchor alone.
    """
    from app.services.prepaid_service_coverage import (
        PrepaidCoverageStatus,
        resolve_prepaid_service_coverage,
    )

    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)

    # Force the exact defect: exact funded coverage, stale anchor.
    entitlement = _entitlement(db_session, invoice)
    subscription.next_billing_at = PERIOD_START
    db_session.commit()

    inside_period = PERIOD_START + timedelta(days=10)
    decision = resolve_prepaid_service_coverage(
        db_session, [subscription], as_of=inside_period
    )[subscription.id]

    assert decision.status is PrepaidCoverageStatus.covered
    assert decision.evidence is not None
    assert decision.evidence.source_id == entitlement.id
    assert at(subscription.next_billing_at) <= inside_period, (
        "test precondition: the anchor must still be stale for this guard to "
        "be meaningful"
    )


def test_replaying_the_same_funding_change_event_is_idempotent(
    db_session, subscriber, subscription
):
    """The durable event is retried on failure; replay must change nothing."""
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    event = _funding_event(db_session, subscriber, invoice)

    PrepaidRenewalHandler().handle(db_session, event)
    db_session.commit()
    db_session.refresh(subscription)
    first_anchor = at(subscription.next_billing_at)
    first_entitlements = db_session.query(ServiceEntitlement).count()

    PrepaidRenewalHandler().handle(db_session, event)
    db_session.commit()
    db_session.refresh(subscription)

    assert at(subscription.next_billing_at) == first_anchor
    assert db_session.query(ServiceEntitlement).count() == first_entitlements


def test_reversal_does_not_leave_a_stale_advanced_anchor(
    db_session, subscriber, subscription
):
    """A refund/reversal revokes coverage, so the anchor must retract too."""
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    PrepaidRenewalHandler().handle(
        db_session, _funding_event(db_session, subscriber, invoice)
    )
    db_session.commit()
    entitlement = _entitlement(db_session, invoice)
    db_session.refresh(subscription)
    assert at(subscription.next_billing_at) == at(entitlement.ends_at)

    # The payment owner reopens the invoice and revokes what its money funded.
    period_start = at(entitlement.starts_at)
    invoice.status = InvoiceStatus.issued
    invoice.balance_due = AMOUNT
    db_session.flush()
    revoke_prepaid_entitlements_for_unpaid_invoice(db_session, invoice)

    retract_prepaid_billing_anchors_after_funding_reversal(
        db_session,
        account_id=subscriber.id,
        payment_id=payment.id,
        invoice_ids=(invoice.id,),
        evidence_ref="pytest:reversal",
    )
    db_session.commit()
    db_session.refresh(subscription)

    assert at(subscription.next_billing_at) == period_start, (
        "the reversed period kept an advanced anchor, so the customer would "
        "have kept unfunded service"
    )


def test_reversal_retraction_is_idempotent(db_session, subscriber, subscription):
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    PrepaidRenewalHandler().handle(
        db_session, _funding_event(db_session, subscriber, invoice)
    )
    db_session.commit()
    period_start = at(_entitlement(db_session, invoice).starts_at)

    invoice.status = InvoiceStatus.issued
    invoice.balance_due = AMOUNT
    db_session.flush()
    revoke_prepaid_entitlements_for_unpaid_invoice(db_session, invoice)
    for _ in range(3):
        retract_prepaid_billing_anchors_after_funding_reversal(
            db_session,
            account_id=subscriber.id,
            payment_id=payment.id,
            invoice_ids=(invoice.id,),
            evidence_ref="pytest:reversal-replay",
        )
    db_session.commit()
    db_session.refresh(subscription)

    assert at(subscription.next_billing_at) == period_start


def test_reversal_retraction_finds_invoices_without_a_payload_hint(
    db_session, subscriber, subscription
):
    """Reversal deactivates allocations; the owner must still find the invoice."""
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    result = _allocate(db_session, payment, invoice)
    PrepaidRenewalHandler().handle(
        db_session, _funding_event(db_session, subscriber, invoice)
    )
    db_session.commit()
    period_start = at(_entitlement(db_session, invoice).starts_at)

    invoice.status = InvoiceStatus.issued
    invoice.balance_due = AMOUNT
    result.allocation.is_active = False
    db_session.flush()
    revoke_prepaid_entitlements_for_unpaid_invoice(db_session, invoice)

    projections = retract_prepaid_billing_anchors_after_funding_reversal(
        db_session,
        account_id=subscriber.id,
        payment_id=payment.id,
        evidence_ref="pytest:reversal-derived",
    )
    db_session.commit()
    db_session.refresh(subscription)

    assert projections
    assert at(subscription.next_billing_at) == period_start


def test_projection_never_claws_back_an_anchor_lead_it_cannot_see(
    db_session, subscriber, subscription
):
    """Advancement is forward-only while this invoice's coverage survives.

    `financial.service_extensions` owns its own billing-anchor projection, and
    `financial.payments` preserves an extension delta when it re-anchors a
    lapsed prepaid renewal. Both leave the anchor AHEAD of what this invoice
    funded. Overwriting it with the entitlement end would silently claw back
    service another owner granted — worse than the stale anchor being fixed.
    """
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    entitlement = _entitlement(db_session, invoice)

    granted_through = at(entitlement.ends_at) + timedelta(days=5)
    subscription.next_billing_at = granted_through
    db_session.flush()

    projections = project_prepaid_billing_anchor_for_invoice(
        db_session, invoice, evidence_ref="pytest:extension-delta"
    )
    db_session.commit()
    db_session.refresh(subscription)

    assert at(subscription.next_billing_at) == granted_through
    assert [item.changed for item in projections] == [False]
    assert [item.retracted for item in projections] == [False]


def test_reviewed_reconciliation_may_correct_a_stale_lead_downward(
    db_session, subscriber, subscription
):
    """A reviewed correction resolves an evidence-free anchor lead.

    The payment path must preserve a lead it cannot explain, but a stale anchor
    left behind by a long-lapsed period carries no grant — it is exactly the
    "unresolved projection" `financial.prepaid_service_coverage` refuses to
    treat as authority. A named owner acting on an operator-confirmed preview
    may pull it back onto exact coverage.
    """
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    entitlement = _entitlement(db_session, invoice)

    stale_lead = at(entitlement.ends_at) + timedelta(days=46)
    subscription.next_billing_at = stale_lead
    db_session.flush()

    # Observational authority leaves it alone...
    project_prepaid_billing_anchor_for_invoice(
        db_session,
        invoice,
        evidence_ref="pytest:observation",
        authority=BillingAnchorAuthority.funding_observation,
    )
    db_session.flush()
    assert at(subscription.next_billing_at) == stale_lead

    # ...a reviewed correction resolves it down to exact coverage.
    projections = project_prepaid_billing_anchor_for_invoice(
        db_session,
        invoice,
        evidence_ref="pytest:reviewed",
        authority=BillingAnchorAuthority.reviewed_reconciliation,
    )
    db_session.commit()
    db_session.refresh(subscription)

    assert at(subscription.next_billing_at) == at(entitlement.ends_at)
    assert [item.retracted for item in projections] == [True]


def test_reviewed_reconciliation_still_cannot_cut_below_granted_service(
    db_session, subscriber, subscription
):
    """The floor holds for reviewed authority too.

    A reviewed correction can delete an evidence-free lead, but never coverage
    another owner granted — otherwise it would be the round-3 defect wearing a
    different hat.
    """
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    entitlement = _entitlement(db_session, invoice)
    granted_through = at(entitlement.ends_at) + timedelta(days=9)

    extension = ServiceExtension(
        reason="Goodwill after a regional outage",
        window_start=PERIOD_START,
        window_end=PERIOD_END,
        days=9,
        scope_type=ServiceExtensionScope.subscribers,
        status=ServiceExtensionStatus.applied,
        applied_at=PERIOD_START,
    )
    db_session.add(extension)
    db_session.flush()
    db_session.add(
        ServiceExtensionEntry(
            extension_id=extension.id,
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            grant_starts_at=at(entitlement.ends_at),
            grant_ends_at=granted_through,
        )
    )
    subscription.next_billing_at = granted_through + timedelta(days=30)
    db_session.flush()

    project_prepaid_billing_anchor_for_invoice(
        db_session,
        invoice,
        evidence_ref="pytest:reviewed-with-extension",
        authority=BillingAnchorAuthority.reviewed_reconciliation,
    )
    db_session.commit()
    db_session.refresh(subscription)

    assert at(subscription.next_billing_at) == granted_through, (
        "a reviewed correction cut below coverage the extension owner granted"
    )


def test_a_reversal_retracts_the_period_but_keeps_an_applied_extension(
    db_session, subscriber, subscription
):
    """A refund removes the refunded period, not a goodwill extension."""
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    entitlement = _entitlement(db_session, invoice)
    granted_through = at(entitlement.ends_at) + timedelta(days=7)

    extension = ServiceExtension(
        reason="Regional outage goodwill",
        window_start=PERIOD_START,
        window_end=PERIOD_END,
        days=7,
        scope_type=ServiceExtensionScope.subscribers,
        status=ServiceExtensionStatus.applied,
        applied_at=PERIOD_START,
    )
    db_session.add(extension)
    db_session.flush()
    db_session.add(
        ServiceExtensionEntry(
            extension_id=extension.id,
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            grant_starts_at=at(entitlement.ends_at),
            grant_ends_at=granted_through,
        )
    )
    db_session.flush()

    # The payment owner reopens the invoice and revokes what its money funded.
    invoice.status = InvoiceStatus.issued
    invoice.balance_due = AMOUNT
    db_session.flush()
    revoke_prepaid_entitlements_for_unpaid_invoice(db_session, invoice)

    retract_prepaid_billing_anchors_after_funding_reversal(
        db_session,
        account_id=subscriber.id,
        payment_id=payment.id,
        invoice_ids=(invoice.id,),
        evidence_ref="pytest:reversal-with-extension",
    )
    db_session.commit()
    db_session.refresh(subscription)

    assert at(subscription.next_billing_at) == granted_through, (
        "the reversal cancelled an applied service-extension grant that the "
        "extension owner, not this payment, had granted"
    )


def test_anchor_projection_is_a_pure_recomputation(
    db_session, subscriber, subscription
):
    """Running the owner projection repeatedly must reach a fixed point."""
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    entitlement = _entitlement(db_session, invoice)

    project_prepaid_billing_anchor_for_invoice(
        db_session, invoice, evidence_ref="pytest:first"
    )
    second = project_prepaid_billing_anchor_for_invoice(
        db_session, invoice, evidence_ref="pytest:second"
    )
    assert [item.changed for item in second] == [False]
    db_session.commit()
    db_session.refresh(subscription)
    assert at(subscription.next_billing_at) == at(entitlement.ends_at)


def test_stale_anchor_repair_preview_then_apply_drives_the_cohort_to_zero(
    db_session, subscriber, subscription
):
    """The existing drift cohort must be repairable, idempotently."""
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    entitlement = _entitlement(db_session, invoice)
    # Reproduce the historical defect: entitlement committed, anchor untouched.
    subscription.next_billing_at = PERIOD_START
    db_session.commit()

    preview = preview_stale_prepaid_billing_anchor_repair(db_session, limit=50)
    assert preview.cohort_size == 1
    assert not preview.truncated
    candidate = preview.candidates[0]
    assert candidate.subscription_id == subscription.id
    assert candidate.current_next_billing_at == PERIOD_START
    assert candidate.coverage_end == at(entitlement.ends_at)

    result = apply_stale_prepaid_billing_anchor_repair(
        db_session,
        preview,
        actor="pytest:operator",
        reason="pytest repair of the anchor-drift cohort",
    )
    assert result.repaired == 1
    assert result.skipped_changed == 0

    db_session.refresh(subscription)
    assert at(subscription.next_billing_at) == at(entitlement.ends_at)

    after = preview_stale_prepaid_billing_anchor_repair(db_session, limit=50)
    assert after.cohort_size == 0, "the drift cohort did not reach zero"

    # Replaying the original preview must not double-write or regress.
    replay = apply_stale_prepaid_billing_anchor_repair(
        db_session,
        preview,
        actor="pytest:operator",
        reason="pytest replay",
    )
    assert replay.repaired == 0
    assert replay.already_correct == 1
    db_session.refresh(subscription)
    assert at(subscription.next_billing_at) == at(entitlement.ends_at)


def test_missing_anchor_repair_requires_exact_active_entitlement(
    db_session, subscriber, subscription
):
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    entitlement = _entitlement(db_session, invoice)
    subscription.next_billing_at = None
    db_session.commit()

    preview = preview_stale_prepaid_billing_anchor_repair(db_session, limit=50)

    assert preview.cohort_size == 1
    candidate = preview.candidates[0]
    assert candidate.current_next_billing_at is None
    assert candidate.coverage_end == at(entitlement.ends_at)
    assert candidate.drift is None

    result = apply_stale_prepaid_billing_anchor_repair(
        db_session,
        preview,
        actor="pytest:operator",
        reason="restore a missing anchor from exact entitlement evidence",
    )

    assert result.repaired == 1
    db_session.refresh(subscription)
    assert at(subscription.next_billing_at) == at(entitlement.ends_at)

    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "repair_stale_prepaid_billing_anchor")
        .one()
    )
    assert audit.metadata_["previous_next_billing_at"] is None
    assert audit.metadata_["drift_seconds"] is None

    # A later regression is new reviewed work, not an idempotent replay of the
    # first repair. A fresh preview must be able to restore the same exact
    # evidence-backed target again.
    subscription.next_billing_at = None
    db_session.commit()
    second_preview = preview_stale_prepaid_billing_anchor_repair(db_session, limit=50)
    second = apply_stale_prepaid_billing_anchor_repair(
        db_session,
        second_preview,
        actor="pytest:operator",
        reason="repair a later anchor regression",
    )
    assert second.repaired == 1
    assert second.replayed == 0
    db_session.refresh(subscription)
    assert at(subscription.next_billing_at) == at(entitlement.ends_at)


def test_missing_anchor_without_entitlement_remains_review_stock(
    db_session, subscription
):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = None
    db_session.commit()

    preview = preview_stale_prepaid_billing_anchor_repair(db_session, limit=50)

    assert preview.cohort_size == 0


def test_stale_anchor_repair_writes_durable_audit_evidence(
    db_session, subscriber, subscription
):
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    entitlement = _entitlement(db_session, invoice)
    subscription.next_billing_at = PERIOD_START
    db_session.commit()

    preview = preview_stale_prepaid_billing_anchor_repair(db_session, limit=50)
    apply_stale_prepaid_billing_anchor_repair(
        db_session,
        preview,
        actor="pytest:operator",
        reason="pytest evidence",
    )

    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "repair_stale_prepaid_billing_anchor")
        .one()
    )
    assert audit.entity_id == str(subscription.id)
    assert audit.metadata_["owner"] == "financial.prepaid_service_renewals"
    assert audit.metadata_["previous_next_billing_at"] == PERIOD_START.isoformat()
    assert (
        audit.metadata_["repaired_next_billing_at"]
        == at(entitlement.ends_at).isoformat()
    )
    assert audit.metadata_["preview_fingerprint"] == preview.fingerprint


def test_repair_skips_a_candidate_whose_coverage_changed_after_preview(
    db_session, subscriber, subscription
):
    """Exactness over convenience: changed evidence is skipped, never guessed."""
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    subscription.next_billing_at = PERIOD_START
    db_session.commit()

    preview = preview_stale_prepaid_billing_anchor_repair(db_session, limit=50)
    assert preview.cohort_size == 1

    entitlement = _entitlement(db_session, invoice)
    entitlement.ends_at = entitlement.ends_at + timedelta(days=3)
    db_session.commit()

    result = apply_stale_prepaid_billing_anchor_repair(
        db_session,
        preview,
        actor="pytest:operator",
        reason="pytest stale evidence",
    )

    assert result.repaired == 0
    assert result.skipped_changed == 1
    db_session.refresh(subscription)
    assert at(subscription.next_billing_at) == PERIOD_START
