from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.models.billing import InvoiceStatus
from app.services import web_prepaid_draft_reconciliation as web_reconciliation
from app.services.prepaid_draft_reconciliation import (
    PrepaidDraftAction,
    PrepaidDraftDisposition,
    PrepaidDraftReconciliationError,
    PrepaidDraftReconciliationPreview,
    PrepaidDraftReconciliationResult,
)
from app.web.admin import billing_invoice_actions

INVOICE_ID = UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT_ID = UUID("22222222-2222-2222-2222-222222222222")
SUBSCRIPTION_ID = UUID("33333333-3333-3333-3333-333333333333")
BASELINE_ID = UUID("44444444-4444-4444-4444-444444444444")
FINGERPRINT = "a" * 64
ACTOR = "system_user:55555555-5555-5555-5555-555555555555"
NOW = datetime(2026, 7, 31, 12, 30, tzinfo=UTC)


def _preview(
    *,
    disposition: PrepaidDraftDisposition = (
        PrepaidDraftDisposition.reviewed_opening_fundable
    ),
    action: PrepaidDraftAction = PrepaidDraftAction.settle_paid,
) -> PrepaidDraftReconciliationPreview:
    return PrepaidDraftReconciliationPreview(
        invoice_id=INVOICE_ID,
        account_id=ACCOUNT_ID,
        invoice_number="INV-112850",
        disposition=disposition,
        recommended_action=action,
        currency="NGN",
        invoice_total=Decimal("18812.50"),
        balance_due=Decimal("18812.50"),
        payment_backed_credit=Decimal("2000.00"),
        authoritative_funding=Decimal("20760.64"),
        opening_funding_available=Decimal("18760.64"),
        opening_funding_required=Decimal("16812.50"),
        opening_funding_baseline_id=BASELINE_ID,
        unbacked_credit=Decimal("0.00"),
        shortfall=Decimal("0.00"),
        subscription_ids=(SUBSCRIPTION_ID,),
        entitlement_ids=(),
        renewal_adjustment_ids=(),
        reason="exact payment funding plus reviewed opening funding covers the draft",
        fingerprint=FINGERPRINT,
    )


def _claims(*, actor: str = ACTOR, fingerprint: str = FINGERPRINT) -> dict:
    return {
        "typ": "prepaid_draft_reconciliation_confirmation",
        "iss": "dotmac_sub.admin.prepaid_draft_reconciliation",
        "ver": 1,
        "jti": "6" * 32,
        "actor": actor,
        "invoice_id": str(INVOICE_ID),
        "preview_fingerprint": fingerprint,
        "effective_at": int(NOW.timestamp()),
        "iat": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 600,
    }


def test_admin_review_exposes_exact_owner_breakdown_and_signed_confirmation(
    monkeypatch,
):
    preview = _preview()
    signed: dict[str, object] = {}
    monkeypatch.setattr(
        web_reconciliation,
        "preview_prepaid_draft_reconciliation",
        lambda _db, _invoice_id: preview,
    )

    def _sign(_db, claims):
        signed.update(claims)
        return "signed-review-token"

    monkeypatch.setattr(web_reconciliation.context_signing, "sign_context_token", _sign)

    review = web_reconciliation.build_admin_review(
        object(), invoice_id=INVOICE_ID, actor=ACTOR, now=NOW
    )

    hidden = {item.key: item.value for item in review.action_form.hidden_values}
    assert review.preview.payment_backed_credit == Decimal("2000.00")
    assert review.preview.opening_funding_required == Decimal("16812.50")
    assert review.preview.authoritative_funding == Decimal("20760.64")
    assert hidden == {
        "preview_fingerprint": FINGERPRINT,
        "confirmation_token": "signed-review-token",
    }
    assert review.action_form.allowed is True
    assert review.action_form.confirmation is not None
    assert "NGN 16,812.50 of reviewed opening funding" in review.action_form.impact
    assert signed["actor"] == ACTOR
    assert signed["invoice_id"] == str(INVOICE_ID)
    assert signed["preview_fingerprint"] == FINGERPRINT
    assert signed["effective_at"] == int(NOW.timestamp())


def test_confirm_uses_signed_time_fingerprint_and_stable_token_idempotency(
    monkeypatch,
):
    captured = {}
    released = []
    expected = PrepaidDraftReconciliationResult(
        invoice_id=INVOICE_ID,
        disposition=PrepaidDraftDisposition.reviewed_opening_fundable,
        action=PrepaidDraftAction.settle_paid,
        final_status=InvoiceStatus.paid,
        applied_amount=Decimal("18812.50"),
        payment_applied_amount=Decimal("2000.00"),
        opening_funding_applied_amount=Decimal("16812.50"),
        opening_funding_consumption_id=UUID("77777777-7777-7777-7777-777777777777"),
        preview_fingerprint=FINGERPRINT,
        replayed=False,
    )
    monkeypatch.setattr(
        web_reconciliation.context_signing,
        "verify_context_token",
        lambda _db, _token: _claims(),
    )
    monkeypatch.setattr(
        web_reconciliation.db_session_adapter,
        "release_read_transaction",
        lambda db: released.append(db),
    )

    def _reconcile(db, command):
        captured["db"] = db
        captured["command"] = command
        return expected

    monkeypatch.setattr(
        web_reconciliation, "reconcile_prepaid_draft_invoice", _reconcile
    )
    db = object()

    result = web_reconciliation.confirm_admin_review(
        db,
        invoice_id=INVOICE_ID,
        actor=ACTOR,
        preview_fingerprint=FINGERPRINT,
        confirmation_token="signed-review-token",
        confirmed="yes",
        reason="Reviewed exact customer funding evidence",
    )

    command = captured["command"]
    assert result is expected
    assert released == [db]
    assert command.invoice_id == INVOICE_ID
    assert command.preview_fingerprint == FINGERPRINT
    assert command.effective_at == NOW
    assert command.context.actor == ACTOR
    assert command.context.reason == "Reviewed exact customer funding evidence"
    assert command.context.idempotency_key == f"prepaid-draft-admin:{'6' * 32}"


def test_confirmation_fails_closed_when_actor_context_changes(monkeypatch):
    monkeypatch.setattr(
        web_reconciliation.context_signing,
        "verify_context_token",
        lambda _db, _token: _claims(actor="system_user:someone-else"),
    )
    monkeypatch.setattr(
        web_reconciliation,
        "reconcile_prepaid_draft_invoice",
        lambda *_args, **_kwargs: pytest.fail("owner must not be called"),
    )

    with pytest.raises(web_reconciliation.PrepaidDraftAdminError) as exc_info:
        web_reconciliation.confirm_admin_review(
            object(),
            invoice_id=INVOICE_ID,
            actor=ACTOR,
            preview_fingerprint=FINGERPRINT,
            confirmation_token="signed-review-token",
            confirmed="yes",
            reason="Reviewed exact evidence",
        )

    assert exc_info.value.code.endswith(".confirmation_context_changed")


def test_authoritative_stale_preview_error_is_not_bypassed(monkeypatch):
    monkeypatch.setattr(
        web_reconciliation.context_signing,
        "verify_context_token",
        lambda _db, _token: _claims(),
    )
    monkeypatch.setattr(
        web_reconciliation.db_session_adapter,
        "release_read_transaction",
        lambda _db: None,
    )
    stale = PrepaidDraftReconciliationError(
        code="financial.prepaid_draft_reconciliation.stale_preview",
        message="Draft evidence changed after preview; preview again.",
    )
    monkeypatch.setattr(
        web_reconciliation,
        "reconcile_prepaid_draft_invoice",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(stale),
    )

    with pytest.raises(PrepaidDraftReconciliationError) as exc_info:
        web_reconciliation.confirm_admin_review(
            object(),
            invoice_id=INVOICE_ID,
            actor=ACTOR,
            preview_fingerprint=FINGERPRINT,
            confirmation_token="signed-review-token",
            confirmed="yes",
            reason="Reviewed exact evidence",
        )

    assert exc_info.value is stale


def test_invoice_action_hint_uses_owner_actionability(monkeypatch):
    actionable = _preview()
    monkeypatch.setattr(
        web_reconciliation,
        "preview_prepaid_draft_reconciliation",
        lambda _db, _invoice_id: actionable,
    )
    assert (
        web_reconciliation.preview_for_invoice_detail(object(), invoice_id=INVOICE_ID)
        is actionable
    )

    blocked = _preview(
        disposition=PrepaidDraftDisposition.insufficient_funding,
        action=PrepaidDraftAction.none,
    )
    monkeypatch.setattr(
        web_reconciliation,
        "preview_prepaid_draft_reconciliation",
        lambda _db, _invoice_id: blocked,
    )
    assert (
        web_reconciliation.preview_for_invoice_detail(object(), invoice_id=INVOICE_ID)
        is None
    )


def test_invoice_page_uses_authoritative_permission_gated_action_form():
    detail = Path("templates/admin/billing/invoice_detail.html").read_text()
    confirmation = Path(
        "templates/admin/billing/prepaid_pay_now_confirm.html"
    ).read_text()
    route = Path("app/web/admin/billing_invoice_actions.py").read_text()

    assert "prepaid_draft_reconciliation_preview" in detail
    assert "can(request, 'billing:invoice:update')" in detail
    assert "prepaid-draft-reconciliation/preview" in detail
    assert "prepaid_recovery_settlement" not in detail
    assert "action_form(review.action_form)" in confirmation
    assert "payment_backed_credit" in confirmation
    assert "opening_funding_required" in confirmation
    assert "authoritative_funding" in confirmation
    assert "unbacked_credit" in confirmation
    assert "shortfall" in confirmation
    assert "prepaid-draft-reconciliation/confirm" in route
    assert 'require_permission("billing:invoice:update")' in route


def test_prepaid_draft_review_renders_csrf_action_form_with_request_context(
    monkeypatch,
):
    monkeypatch.setattr(
        web_reconciliation,
        "preview_prepaid_draft_reconciliation",
        lambda _db, _invoice_id: _preview(),
    )
    monkeypatch.setattr(
        web_reconciliation.context_signing,
        "sign_context_token",
        lambda _db, _claims: "signed-review-token",
    )
    review = web_reconciliation.build_admin_review(
        object(), invoice_id=INVOICE_ID, actor=ACTOR, now=NOW
    )
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={"permission_keys": frozenset({"billing:invoice:update"})},
            csrf_token="csrf-review-token",
        ),
        url=SimpleNamespace(
            path=f"/admin/billing/invoices/{INVOICE_ID}/prepaid-draft-reconciliation/preview"
        ),
    )

    html = billing_invoice_actions.templates.env.get_template(
        "admin/billing/prepaid_pay_now_confirm.html"
    ).render(
        request=request,
        review=review,
        invoice_id=INVOICE_ID,
        current_user=None,
        sidebar_stats=None,
    )

    assert 'name="_csrf_token" value="csrf-review-token"' in html
    assert 'name="confirmation_token" value="signed-review-token"' in html
    assert "Settle invoice" in html


def test_recovery_billing_no_longer_owns_a_parallel_settlement_writer():
    source = Path("app/services/prepaid_recovery_billing.py").read_text()

    assert "settle_prepaid_recovery_invoice" not in source
    assert "preview_prepaid_recovery_settlement" not in source
    assert "settle_single_invoice_from_credit" not in source
    assert "restore_account_services" not in source
