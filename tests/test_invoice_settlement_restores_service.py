"""Non-payment settlement (write-off / void) of an overdue invoice must lift the
overdue enforcement lock and restore service — same as the restore-on-payment
path. Without this, the debt clears but the service stays suspended on a stale
overdue lock.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import InvoiceDueDateBasis, InvoiceStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.schemas.billing import (
    CreditNoteApplicationPreviewRequest,
    CreditNoteApplyRequest,
    CreditNoteIssuePreviewRequest,
    InvoiceBulkVoidRequest,
    InvoiceCreate,
)
from app.services import billing as billing_service
from app.services.account_lifecycle import has_active_lock, suspend_subscription


def _overdue_postpaid(db, subscriber, offer):
    """A postpaid sub suspended by an overdue lock for a real past-due invoice."""
    now = datetime.now(UTC)
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        start_at=now - timedelta(days=60),
        next_billing_at=now + timedelta(days=1),
    )
    db.add(sub)
    db.commit()
    invoice = billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            issued_at=now - timedelta(days=30),
            due_at=now - timedelta(days=10),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:postpaid-contract",
            due_date_policy_version="test-v1",
        ),
    )
    suspend_subscription(
        db,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source=f"invoice:{invoice.id}",
    )
    db.commit()
    db.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended
    return sub, invoice


def test_write_off_overdue_invoice_restores_service(
    db_session, subscriber, catalog_offer
):
    sub, invoice = _overdue_postpaid(db_session, subscriber, catalog_offer)

    billing_service.invoices.write_off(db_session, str(invoice.id))

    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active
    assert not has_active_lock(
        db_session, str(sub.id), reason=EnforcementReason.overdue
    )


def test_void_overdue_invoice_restores_service(db_session, subscriber, catalog_offer):
    sub, invoice = _overdue_postpaid(db_session, subscriber, catalog_offer)

    billing_service.invoices.void(db_session, str(invoice.id))

    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active
    assert not has_active_lock(
        db_session, str(sub.id), reason=EnforcementReason.overdue
    )


def test_bulk_void_overdue_invoice_restores_service(
    db_session, subscriber, catalog_offer
):
    sub, invoice = _overdue_postpaid(db_session, subscriber, catalog_offer)

    billing_service.invoices.bulk_void(
        db_session, InvoiceBulkVoidRequest(invoice_ids=[str(invoice.id)])
    )

    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active


def test_credit_note_settlement_rechecks_access_and_restores_eligible_service(
    db_session, subscriber, catalog_offer
):
    sub, invoice = _overdue_postpaid(db_session, subscriber, catalog_offer)
    credit_note = billing_service.credit_notes.issue_system(
        db_session,
        CreditNoteIssuePreviewRequest(
            account_id=subscriber.id,
            currency=invoice.currency,
            subtotal=invoice.balance_due,
            total=invoice.balance_due,
        ),
        idempotency_key="credit-settlement-restoration-issue",
        commit=True,
    ).credit_note
    preview = billing_service.credit_notes.preview_application(
        db_session,
        str(credit_note.id),
        CreditNoteApplicationPreviewRequest(invoice_id=invoice.id),
    )

    result = billing_service.credit_notes.apply_with_evidence(
        db_session,
        str(credit_note.id),
        CreditNoteApplyRequest(
            invoice_id=invoice.id,
            amount=preview.apply_amount,
            preview_fingerprint=preview.fingerprint,
            idempotency_key="credit-settlement-restore-test",
        ),
    )

    db_session.refresh(sub)
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")
    assert result.preview is not None
    assert result.preview.access_consequence == "recheck_after_receivable_settlement"
    assert sub.status == SubscriptionStatus.active
    assert not has_active_lock(
        db_session, str(sub.id), reason=EnforcementReason.overdue
    )
