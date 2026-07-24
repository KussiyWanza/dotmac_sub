"""Bank-transfer proof flow: submit -> verify/reject -> credit + notify."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    TopupIntent,
)
from app.models.event_store import EventStore
from app.models.notification import Notification
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.models.subscriber import SubscriberStatus
from app.services import payment_proofs as svc
from app.services.account_credit_deposits import AccountCreditDeposits
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.topup_intents import (
    DIRECT_TRANSFER_PROVIDER,
    DirectTransferBankAccountEvidence,
    TopupIntentChannel,
    TopupIntentError,
    TopupIntentStatus,
)


def _context(action: str) -> CommandContext:
    return CommandContext.system(
        actor="test:payment-proof-reviewer",
        scope=(svc.SUBMISSION_SCOPE if action == "submit" else svc.REVIEW_SCOPE),
        reason=f"Payment-proof {action} behavior test",
    )


def _submit_command(db_session, *args, **kwargs) -> dict[str, object | None]:
    db_session_adapter.release_read_transaction(db_session)
    return svc.submit_proof(
        db_session,
        *args,
        context=_context("submit"),
        **kwargs,
    ).to_dict()


def _verify(db_session, proof_id, **kwargs) -> dict[str, object | None]:
    db_session_adapter.release_read_transaction(db_session)
    return svc.verify_proof(
        db_session,
        proof_id,
        context=_context("verify"),
        **kwargs,
    ).to_dict()


def _reject(db_session, proof_id, **kwargs) -> dict[str, object | None]:
    db_session_adapter.release_read_transaction(db_session)
    return svc.reject_proof(
        db_session,
        proof_id,
        context=_context("reject"),
        **kwargs,
    ).to_dict()


def _account(db_session):
    from app.models.subscriber import Subscriber

    sub = Subscriber(
        first_name="Pay",
        last_name="Transfer",
        email="pay.transfer@example.com",
    )
    db_session.add(sub)
    db_session.commit()
    return sub


def _open_invoice(db_session, sub, amount="3000.00"):
    invoice = Invoice(
        account_id=sub.id,
        status=InvoiceStatus.issued,
        subtotal=Decimal(amount),
        tax_total=Decimal("0.00"),
        total=Decimal(amount),
        balance_due=Decimal(amount),
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def _submit(db_session, sub, amount="5000", reference=None, file_path="x.png"):
    return _submit_command(
        db_session,
        str(sub.id),
        submitted_by=str(sub.id),
        amount=amount,
        reference=reference,
        file_path=f"uploads/payment_proofs/{file_path}",
    )


def _direct_transfer_intent(db_session, sub, *, status="pending") -> TopupIntent:
    intent = TopupIntent(
        account_id=sub.id,
        reference=f"TRF-{uuid4().hex[:12].upper()}",
        provider_type=DIRECT_TRANSFER_PROVIDER,
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status=status,
        metadata_={"payment_method": "bank_transfer"},
    )
    db_session.add(intent)
    db_session.commit()
    db_session.refresh(intent)
    return intent


def _submit_direct_transfer(db_session, sub, intent) -> svc.PaymentProofResult:
    command = svc.DirectTransferProofSubmissionCommand(
        intent_id=intent.id,
        account_id=sub.id,
        submitted_by=sub.id,
        selected_bank_account=DirectTransferBankAccountEvidence(
            id="bank-primary",
            bank_name="Dotmac Test Bank",
            account_name="Dotmac Payments",
            account_number="0123456789",
        ),
        paid_at=datetime(2026, 7, 20, tzinfo=UTC),
        file_path="uploads/payment_proofs/direct-transfer.png",
    )
    db_session_adapter.release_read_transaction(db_session)
    return svc.submit_direct_transfer_proof(
        db_session,
        command,
        context=_context("submit"),
    )


def test_direct_transfer_submission_commits_proof_intent_link_and_events_atomically(
    db_session,
):
    sub = _account(db_session)
    intent = _direct_transfer_intent(db_session, sub)

    result = _submit_direct_transfer(db_session, sub, intent)

    proof = db_session.get(PaymentProof, result.id)
    persisted_intent = db_session.get(TopupIntent, intent.id)
    assert proof is not None
    assert proof.status is PaymentProofStatus.submitted
    assert proof.reference == intent.reference
    assert proof.amount == Decimal("5000.00")
    assert persisted_intent is not None
    assert persisted_intent.status == TopupIntentStatus.submitted.value
    assert persisted_intent.metadata_["payment_proof_id"] == str(proof.id)
    assert persisted_intent.metadata_["selected_bank_account"] == {
        "id": "bank-primary",
        "bank_name": "Dotmac Test Bank",
        "account_name": "Dotmac Payments",
        "account_number": "0123456789",
        "sort_code": "",
    }
    event_types = {
        row.event_type
        for row in db_session.query(EventStore)
        .filter(
            EventStore.event_type.in_(
                {
                    "payment_proof.submitted",
                    "topup_intent.direct_transfer_submitted",
                }
            )
        )
        .all()
    }
    assert event_types == {
        "payment_proof.submitted",
        "topup_intent.direct_transfer_submitted",
    }


def test_direct_transfer_submission_rolls_back_proof_when_intent_staging_fails(
    db_session, monkeypatch
):
    from app.services import topup_intents

    sub = _account(db_session)
    intent = _direct_transfer_intent(db_session, sub)

    def fail_intent_stage(*_args, **_kwargs):
        raise RuntimeError("intent evidence unavailable")

    monkeypatch.setattr(
        topup_intents,
        "stage_direct_transfer_proof_submission",
        fail_intent_stage,
    )

    with pytest.raises(RuntimeError, match="intent evidence unavailable"):
        _submit_direct_transfer(db_session, sub, intent)

    db_session.expire_all()
    persisted_intent = db_session.get(TopupIntent, intent.id)
    assert persisted_intent is not None
    assert persisted_intent.status == TopupIntentStatus.pending.value
    assert "payment_proof_id" not in (persisted_intent.metadata_ or {})
    assert db_session.query(PaymentProof).count() == 0
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type.in_(
                {
                    "payment_proof.submitted",
                    "topup_intent.direct_transfer_submitted",
                }
            )
        )
        .count()
        == 0
    )


def test_direct_transfer_submission_rejects_stale_intent_before_proof_creation(
    db_session,
):
    sub = _account(db_session)
    intent = _direct_transfer_intent(
        db_session,
        sub,
        status=TopupIntentStatus.canceled.value,
    )

    with pytest.raises(TopupIntentError) as exc:
        _submit_direct_transfer(db_session, sub, intent)

    assert exc.value.code == "financial.topup_intents.invalid_transition"
    assert db_session.query(PaymentProof).count() == 0


def test_submit_validates_amount(db_session):
    sub = _account(db_session)
    with pytest.raises(svc.PaymentProofError) as exc:
        _submit_command(
            db_session,
            str(sub.id),
            submitted_by=str(sub.id),
            amount="0",
            file_path="uploads/payment_proofs/x.png",
        )
    assert exc.value.code == "financial.payment_proofs.amount_non_positive"


def test_submit_rolls_back_when_reviewer_work_item_cannot_be_staged(
    db_session, monkeypatch
):
    from app.services import staff_notifications

    sub = _account(db_session)

    def fail_queue(*_args, **_kwargs):
        raise RuntimeError("review work item unavailable")

    monkeypatch.setattr(
        staff_notifications,
        "queue_permission_review_request",
        fail_queue,
    )

    with pytest.raises(RuntimeError, match="review work item unavailable"):
        _submit(db_session, sub, reference="TRF-ATOMIC-SUBMIT")

    assert db_session.query(PaymentProof).count() == 0


def test_verify_creates_succeeded_payment_and_notifies(db_session):
    sub = _account(db_session)
    proof = _submit_command(
        db_session,
        str(sub.id),
        submitted_by=str(sub.id),
        amount="5000",
        bank_name="GTB",
        reference="TRF-123",
        paid_at=datetime(2026, 6, 9, tzinfo=UTC),
        file_path="uploads/payment_proofs/x.png",
    )
    assert proof["status"] == "submitted"

    out = _verify(db_session, proof["id"], verified_by="admin-1", auto_allocate=True)
    assert out["status"] == "verified"
    assert out["payment_id"] is not None

    payment = db_session.get(Payment, out["payment_id"])
    assert payment.status == PaymentStatus.succeeded
    assert Decimal(str(payment.amount)) == Decimal("5000.00")
    assert "TRF-123" == payment.external_id

    notes = (
        db_session.query(Notification)
        .filter(Notification.subscriber_id == sub.id)
        .filter(Notification.event_type == "payment_proof_verified")
        .all()
    )
    assert len(notes) == 2  # push + email

    # Double review is rejected.
    with pytest.raises(svc.PaymentProofError) as exc:
        _verify(db_session, proof["id"], verified_by="admin-1")
    assert exc.value.code == "financial.payment_proofs.already_reviewed"


def test_verify_rolls_back_money_and_review_state_when_delivery_staging_fails(
    db_session, monkeypatch
):
    from app.services.notification import notifications as notification_service

    sub = _account(db_session)
    proof = _submit(db_session, sub, reference="TRF-ATOMIC-VERIFY")

    def fail_delivery(*_args, **_kwargs):
        raise RuntimeError("customer delivery unavailable")

    monkeypatch.setattr(
        notification_service,
        "queue_customer_notification",
        fail_delivery,
    )

    with pytest.raises(RuntimeError, match="customer delivery unavailable"):
        _verify(db_session, proof["id"], verified_by="admin-1")

    persisted = db_session.get(PaymentProof, proof["id"])
    assert persisted is not None
    assert persisted.status is PaymentProofStatus.submitted
    assert persisted.payment_id is None
    assert db_session.query(Payment).count() == 0


def test_verify_resolves_delinquent_status_after_paid_invoice(db_session):
    from app.models.catalog import (
        AccessType,
        BillingMode,
        CatalogOffer,
        OfferStatus,
        PriceBasis,
        ServiceType,
        Subscription,
        SubscriptionStatus,
    )
    from app.models.collections import DunningCase, DunningCaseStatus

    sub = _account(db_session)
    sub.status = SubscriberStatus.delinquent
    offer = CatalogOffer(
        name="Proof Restore Plan",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    subscription = Subscription(
        subscriber_id=sub.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        unit_price=Decimal("3000.00"),
    )
    db_session.add(subscription)
    db_session.flush()
    invoice = _open_invoice(db_session, sub, "3000.00")
    coverage_now = datetime.now(UTC)
    invoice.billing_period_start = coverage_now - timedelta(days=1)
    invoice.billing_period_end = coverage_now + timedelta(days=29)
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid service period",
            quantity=Decimal("1"),
            unit_price=Decimal("3000.00"),
            amount=Decimal("3000.00"),
            is_active=True,
        )
    )
    db_session.add(DunningCase(account_id=sub.id, status=DunningCaseStatus.open))
    db_session.commit()

    proof = _submit(db_session, sub, amount="3000", reference="TRF-RESTORE")
    _verify(db_session, proof["id"], verified_by="admin-1")

    db_session.refresh(sub)
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert sub.status == SubscriberStatus.active
    assert (
        db_session.query(DunningCase)
        .filter(DunningCase.account_id == sub.id)
        .one()
        .status
        == DunningCaseStatus.resolved
    )


def test_reject_requires_reason_and_notifies(db_session):
    sub = _account(db_session)
    proof = _submit_command(
        db_session,
        str(sub.id),
        submitted_by=str(sub.id),
        amount="2500",
        file_path="uploads/payment_proofs/y.png",
    )
    with pytest.raises(svc.PaymentProofReviewError) as exc:
        _reject(db_session, proof["id"], verified_by="admin-1", review_notes="  ")
    assert exc.value.code == "financial.payment_proofs.rejection_reason_required"
    assert exc.value.field == "review_notes"
    out = _reject(
        db_session,
        proof["id"],
        verified_by="admin-1",
        review_notes="Amount does not match our statement",
    )
    assert out["status"] == "rejected"
    notes = (
        db_session.query(Notification)
        .filter(Notification.event_type == "payment_proof_rejected")
        .all()
    )
    assert len(notes) == 2
    assert "statement" in notes[0].body


def test_verify_with_admin_amount_override(db_session):
    """The Payment uses the reviewer-confirmed amount, stored alongside the
    customer-claimed amount."""
    sub = _account(db_session)
    proof = _submit(db_session, sub, amount="5000", reference="TRF-OVR")

    out = _verify(db_session, proof["id"], verified_by="admin-1", amount="4500")
    assert Decimal(str(out["amount"])) == Decimal("5000.00")  # claimed, unchanged
    assert Decimal(str(out["verified_amount"])) == Decimal("4500.00")

    payment = db_session.get(Payment, out["payment_id"])
    assert Decimal(str(payment.amount)) == Decimal("4500.00")


def test_verify_rejects_invalid_or_nonpositive_amount(db_session):
    sub = _account(db_session)
    proof = _submit(db_session, sub)
    with pytest.raises(svc.PaymentProofError) as exc:
        _verify(db_session, proof["id"], verified_by="admin-1", amount="0")
    assert isinstance(exc.value, svc.PaymentProofReviewError)
    assert exc.value.code == "financial.payment_proofs.verified_amount_non_positive"
    assert exc.value.field == "amount"
    with pytest.raises(svc.PaymentProofReviewError) as exc:
        _verify(db_session, proof["id"], verified_by="admin-1", amount="not-a-number")
    assert exc.value.code == "financial.payment_proofs.invalid_verified_amount"
    assert exc.value.field == "amount"


def test_verify_without_auto_allocate_keeps_money_as_credit(db_session):
    """auto_allocate=False must NOT silently fall back to auto-allocation:
    the open invoice stays open and the amount becomes account credit."""
    from app.services.billing._common import get_account_credit_balance

    sub = _account(db_session)
    invoice = _open_invoice(db_session, sub, amount="3000.00")
    proof = _submit(db_session, sub, amount="5000", reference="TRF-CRED")

    out = _verify(db_session, proof["id"], verified_by="admin-1", auto_allocate=False)
    payment = db_session.get(Payment, out["payment_id"])
    allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    assert allocations == []
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.issued
    assert Decimal(str(invoice.balance_due)) == Decimal("3000.00")
    assert get_account_credit_balance(db_session, str(sub.id)) == Decimal("5000.00")


def test_deposit_proof_review_uses_typed_account_credit_owner(db_session):
    sub = _account(db_session)
    intent, _preview, _replayed = AccountCreditDeposits.stage_intent(
        db_session,
        account_id=sub.id,
        amount="5000.00",
        currency="NGN",
        minimum="1000.00",
        maximum="500000.00",
        reference="TRF-TYPED-DEPOSIT",
        provider_type="direct_bank_transfer",
        provider_id=None,
        expires_at=datetime.now(UTC) + timedelta(days=7),
        idempotency_key="typed-deposit-proof-intent",
        channel=TopupIntentChannel.customer_selfcare,
        created_by=str(sub.id),
    )
    db_session.commit()
    invoice = _open_invoice(db_session, sub, amount="3000.00")
    proof = _submit(
        db_session,
        sub,
        amount="5000.00",
        reference=intent.reference,
        file_path="typed-deposit.png",
    )

    result = _verify(
        db_session,
        proof["id"],
        verified_by="admin-1",
    )

    payment = db_session.get(Payment, result["payment_id"])
    typed_intent = db_session.get(TopupIntent, intent.id)
    db_session.refresh(invoice)
    assert typed_intent.completed_payment_id == payment.id
    assert payment.settlement is not None
    assert payment.settlement.prepaid_amount == Decimal("0.00")
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")


def test_verify_with_auto_allocate_pays_open_invoice(db_session):
    sub = _account(db_session)
    invoice = _open_invoice(db_session, sub, amount="3000.00")
    proof = _submit(db_session, sub, amount="5000", reference="TRF-ALLOC")

    out = _verify(db_session, proof["id"], verified_by="admin-1", auto_allocate=True)
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    payment = db_session.get(Payment, out["payment_id"])
    allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    assert len(allocations) == 1
    assert Decimal(str(allocations[0].amount)) == Decimal("3000.00")


def test_verify_direct_transfer_wht_uses_snapshot_and_rejects_amount_tampering(
    db_session,
):
    from app.models.payment_proof import WithholdingTaxRecord

    sub = _account(db_session)
    invoice = _open_invoice(db_session, sub, amount="107500.00")
    intent = TopupIntent(
        account_id=sub.id,
        reference="TRF-WHT-SNAPSHOT",
        provider_type=DIRECT_TRANSFER_PROVIDER,
        currency="NGN",
        requested_amount=Decimal("102500.00"),
        status=TopupIntentStatus.submitted.value,
        metadata_={
            "payment_method": "bank_transfer",
            "payment_flow": "invoice_payment",
            "invoice_id": str(invoice.id),
            "withholding_tax": {
                "schema_version": 1,
                "account_id": str(sub.id),
                "policy_version": 5,
                "source_invoice_id": str(invoice.id),
                "currency": "NGN",
                "vat_exclusive_amount": "100000.00",
                "vat_amount": "7500.00",
                "gross_amount": "107500.00",
                "withholding_tax_rate_percent": "5.00",
                "withholding_tax_amount": "5000.00",
                "net_amount": "102500.00",
            },
        },
    )
    db_session.add(intent)
    db_session.commit()
    proof = _submit_command(
        db_session,
        str(sub.id),
        submitted_by=str(sub.id),
        amount="102500.00",
        reference="TRF-WHT-SNAPSHOT",
        file_path="uploads/payment_proofs/wht-snapshot.png",
    )

    with pytest.raises(svc.PaymentProofReviewError) as exc:
        _verify(db_session, proof["id"], verified_by="admin-1", amount="100000.00")
    assert exc.value.code == "financial.payment_proofs.verified_amount_conflict"

    out = _verify(db_session, proof["id"], verified_by="admin-1")
    payment = db_session.get(Payment, out["payment_id"])
    record = db_session.get(WithholdingTaxRecord, out["withholding_tax_record_id"])
    db_session.refresh(invoice)

    assert payment is not None
    assert payment.amount == Decimal("107500.00")
    assert record is not None
    assert record.account_id == sub.id
    assert record.billing_account_id is None
    assert record.source_invoice_id == invoice.id
    assert record.policy_version == 5
    assert record.vat_exclusive_amount == Decimal("100000.00")
    assert record.vat_amount == Decimal("7500.00")
    assert record.gross_amount == Decimal("107500.00")
    assert record.net_amount == Decimal("102500.00")
    assert record.wht_amount == Decimal("5000.00")
    assert record.wht_rate == Decimal("5.00")
    assert invoice.status == InvoiceStatus.paid


def test_duplicate_reference_is_flagged_on_submit(db_session):
    sub = _account(db_session)
    first = _submit(db_session, sub, reference="TRF-DUP", file_path="a.png")
    assert first["duplicate_reference"] is False
    second = _submit(db_session, sub, reference="TRF-DUP", file_path="b.png")
    assert second["duplicate_reference"] is True
    # A different reference is not flagged.
    other = _submit(db_session, sub, reference="TRF-OTHER", file_path="c.png")
    assert other["duplicate_reference"] is False


def test_verify_blocked_when_reference_already_verified(db_session):
    """The same receipt reference cannot be paid out twice."""
    sub = _account(db_session)
    first = _submit(db_session, sub, reference="TRF-TWICE", file_path="a.png")
    second = _submit(db_session, sub, reference="TRF-TWICE", file_path="b.png")

    _verify(db_session, first["id"], verified_by="admin-1")
    with pytest.raises(svc.PaymentProofError) as exc:
        _verify(db_session, second["id"], verified_by="admin-1")
    assert "TRF-TWICE" in exc.value.message
    assert isinstance(exc.value, svc.PaymentProofReviewError)
    assert exc.value.code == "financial.payment_proofs.duplicate_transfer_reference"

    # Only one payment was created for that reference.
    payments = (
        db_session.query(Payment).filter(Payment.external_id == "TRF-TWICE").all()
    )
    assert len(payments) == 1
    # The duplicate can still be rejected.
    out = _reject(
        db_session, second["id"], verified_by="admin-1", review_notes="Duplicate"
    )
    assert out["status"] == "rejected"


def test_verify_and_reject_emit_audit_events(db_session):
    from app.models.audit import AuditEvent

    sub = _account(db_session)
    proof = _submit(db_session, sub, reference="TRF-AUD", file_path="a.png")
    _verify(db_session, proof["id"], verified_by="admin-1", amount="4999.50")
    other = _submit(db_session, sub, reference="TRF-AUD-2", file_path="b.png")
    _reject(db_session, other["id"], verified_by="admin-2", review_notes="No match")

    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "payment_proof")
        .all()
    )
    by_action = {e.action: e for e in events}
    assert set(by_action) == {"verify", "reject"}
    verify_meta = by_action["verify"].metadata_ or {}
    assert verify_meta.get("verified_amount") == "4999.50"
    assert verify_meta.get("claimed_amount") == "5000.00"
    reject_meta = by_action["reject"].metadata_ or {}
    assert reject_meta.get("reason") == "No match"
