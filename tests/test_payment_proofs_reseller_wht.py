"""Consolidated reseller transfer proofs fail closed for automatic WHT."""

from decimal import Decimal

import pytest

from app.models.billing import Payment, PaymentStatus
from app.models.payment_proof import WithholdingTaxRecord
from app.models.subscriber import Reseller
from app.services import billing as billing_service
from app.services import payment_proofs as svc
from app.services import tax_accounting
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _context(action: str) -> CommandContext:
    return CommandContext.system(
        actor="test:reseller-payment-proof-reviewer",
        scope=(svc.SUBMISSION_SCOPE if action == "submit" else svc.REVIEW_SCOPE),
        reason=f"Reseller payment-proof {action} behavior test",
    )


def _submit(db_session, *args, **kwargs) -> dict[str, object | None]:
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


def _reseller_account(db_session):
    reseller = Reseller(name="Acme Reseller", contact_email="ops@acme.example.com")
    db_session.add(reseller)
    db_session.commit()
    ba = billing_service.billing_accounts.get_for_reseller(db_session, str(reseller.id))
    return reseller, ba


@pytest.mark.parametrize(
    ("gross_amount", "wht_rate"),
    [
        ("100000", None),
        (None, "5"),
        ("100000", "5"),
    ],
)
def test_submit_consolidated_rejects_customer_entered_wht(
    db_session,
    gross_amount,
    wht_rate,
):
    _, ba = _reseller_account(db_session)

    with pytest.raises(svc.PaymentProofError) as exc:
        _submit(
            db_session,
            None,
            submitted_by=None,
            amount="95000",
            billing_account_id=str(ba.id),
            gross_amount=gross_amount,
            wht_rate=wht_rate,
            reference="BULK-WHT-BLOCKED",
            file_path="uploads/payment_proofs/bulk-blocked.png",
        )

    assert (
        exc.value.code == "financial.payment_proofs.withholding_tax_basis_unavailable"
    )


def test_verify_consolidated_without_wht_credits_verified_cash_only(db_session):
    _, ba = _reseller_account(db_session)
    proof = _submit(
        db_session,
        None,
        submitted_by=None,
        amount="50000",
        billing_account_id=str(ba.id),
        reference="BULK-NET-ONLY",
        file_path="uploads/payment_proofs/bulk-net-only.png",
    )

    out = _verify(db_session, proof["id"], verified_by="admin-1")

    assert out["withholding_tax_record_id"] is None
    payment = db_session.get(Payment, out["payment_id"])
    assert payment is not None
    assert payment.status == PaymentStatus.succeeded
    assert payment.billing_account_id == ba.id
    assert Decimal(str(payment.amount)) == Decimal("50000.00")
    assert db_session.query(WithholdingTaxRecord).count() == 0
    assert not tax_accounting.list_withholding_tax_records(
        db_session,
        billing_account_id=str(ba.id),
    )
