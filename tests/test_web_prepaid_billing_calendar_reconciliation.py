from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.services import web_prepaid_billing_calendar_reconciliation as web_service
from app.services.prepaid_billing_calendar_reconciliation import (
    PrepaidBillingCalendarCorrectionKind,
    PrepaidBillingCalendarDisposition,
    PrepaidBillingCalendarPreview,
    PrepaidBillingCalendarReconciliationResult,
)

INVOICE_ID = UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT_ID = UUID("22222222-2222-2222-2222-222222222222")
SUBSCRIPTION_ID = UUID("33333333-3333-3333-3333-333333333333")
LINE_ID = UUID("44444444-4444-4444-4444-444444444444")
ENTITLEMENT_ID = UUID("55555555-5555-5555-5555-555555555555")
PAYMENT_ID = UUID("66666666-6666-6666-6666-666666666666")
ACTOR = "system_user:77777777-7777-7777-7777-777777777777"
FINGERPRINT = "a" * 64
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
CURRENT_START = datetime(2026, 7, 6, tzinfo=UTC)
CURRENT_END = datetime(2026, 8, 6, tzinfo=UTC)
PROPOSED_START = datetime(2026, 7, 5, 23, tzinfo=UTC)
PROPOSED_END = datetime(2026, 8, 5, 23, tzinfo=UTC)


def _preview(
    disposition: PrepaidBillingCalendarDisposition = (
        PrepaidBillingCalendarDisposition.eligible
    ),
    correction_kind: PrepaidBillingCalendarCorrectionKind = (
        PrepaidBillingCalendarCorrectionKind.retired_utc_midnight
    ),
) -> PrepaidBillingCalendarPreview:
    return PrepaidBillingCalendarPreview(
        invoice_id=INVOICE_ID,
        account_id=ACCOUNT_ID,
        invoice_number="INV-UTC-001",
        subscription_id=SUBSCRIPTION_ID,
        invoice_line_id=LINE_ID,
        entitlement_id=ENTITLEMENT_ID,
        payment_id=PAYMENT_ID,
        payment_effective_at=datetime(2026, 7, 6, 12, 30, tzinfo=UTC),
        current_starts_at=CURRENT_START,
        current_ends_at=CURRENT_END,
        proposed_starts_at=PROPOSED_START,
        proposed_ends_at=PROPOSED_END,
        proposed_starts_on="2026-07-06",
        proposed_ends_on="2026-08-06",
        timezone_name="Africa/Lagos",
        disposition=disposition,
        reason="Exact retired UTC signature.",
        fingerprint=FINGERPRINT,
        correction_kind=correction_kind,
    )


def _claims(*, actor: str = ACTOR, fingerprint: str = FINGERPRINT) -> dict:
    return {
        "typ": "prepaid_billing_calendar_reconciliation_confirmation",
        "iss": "dotmac_sub.admin.prepaid_billing_calendar_reconciliation",
        "ver": 1,
        "jti": "8" * 32,
        "actor": actor,
        "invoice_id": str(INVOICE_ID),
        "preview_fingerprint": fingerprint,
        "iat": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 600,
    }


def test_review_is_signed_and_exposes_zero_value_consequences(monkeypatch):
    signed: dict[str, object] = {}
    monkeypatch.setattr(
        web_service,
        "preview_prepaid_billing_calendar_reconciliation",
        lambda _db, _invoice_id: _preview(),
    )

    def _sign(_db, claims):
        signed.update(claims)
        return "signed-calendar-review"

    monkeypatch.setattr(web_service.context_signing, "sign_context_token", _sign)

    review = web_service.build_admin_review(
        object(), invoice_id=INVOICE_ID, actor=ACTOR, now=NOW
    )

    hidden = {item.key: item.value for item in review.action_form.hidden_values}
    assert hidden == {
        "preview_fingerprint": FINGERPRINT,
        "confirmation_token": "signed-calendar-review",
    }
    assert review.action_form.allowed is True
    assert review.action_form.confirmation is not None
    assert "economic delta is NGN 0.00" in review.action_form.impact
    assert signed["actor"] == ACTOR
    assert signed["invoice_id"] == str(INVOICE_ID)
    assert signed["preview_fingerprint"] == FINGERPRINT


def test_blocked_review_has_no_confirmation_capability(monkeypatch):
    monkeypatch.setattr(
        web_service,
        "preview_prepaid_billing_calendar_reconciliation",
        lambda _db, _invoice_id: _preview(
            PrepaidBillingCalendarDisposition.anchor_changed
        ),
    )

    review = web_service.build_admin_review(
        object(), invoice_id=INVOICE_ID, actor=ACTOR, now=NOW
    )

    assert review.action_form.allowed is False
    assert review.action_form.hidden_values == ()
    assert review.action_form.confirmation is None


def test_lapsed_payment_review_explains_scoped_access_restoration(monkeypatch):
    monkeypatch.setattr(
        web_service,
        "preview_prepaid_billing_calendar_reconciliation",
        lambda _db, _invoice_id: _preview(
            correction_kind=PrepaidBillingCalendarCorrectionKind.lapsed_payment_period
        ),
    )
    monkeypatch.setattr(
        web_service.context_signing,
        "sign_context_token",
        lambda _db, _claims: "signed-calendar-review",
    )

    review = web_service.build_admin_review(
        object(), invoice_id=INVOICE_ID, actor=ACTOR, now=NOW
    )

    assert "resolves only the prepaid lock" in review.action_form.impact
    assert "independent blocker is preserved" in review.action_form.impact


def test_confirmation_binds_actor_invoice_fingerprint_and_idempotency(monkeypatch):
    captured = {}
    released = []
    expected = PrepaidBillingCalendarReconciliationResult(
        invoice_id=INVOICE_ID,
        subscription_id=SUBSCRIPTION_ID,
        entitlement_id=ENTITLEMENT_ID,
        previous_starts_at=CURRENT_START,
        previous_ends_at=CURRENT_END,
        corrected_starts_at=PROPOSED_START,
        corrected_ends_at=PROPOSED_END,
        preview_fingerprint=FINGERPRINT,
        replayed=False,
    )
    monkeypatch.setattr(
        web_service.context_signing,
        "verify_context_token",
        lambda _db, _token: _claims(),
    )
    monkeypatch.setattr(
        web_service.db_session_adapter,
        "release_read_transaction",
        lambda db: released.append(db),
    )

    def _reconcile(_db, command):
        captured["command"] = command
        return expected

    monkeypatch.setattr(web_service, "reconcile_prepaid_billing_calendar", _reconcile)

    result = web_service.confirm_admin_review(
        object(),
        invoice_id=INVOICE_ID,
        actor=ACTOR,
        preview_fingerprint=FINGERPRINT,
        confirmation_token="signed",
        confirmed="yes",
        reason="Reviewed historical UTC-to-WAT repair.",
    )

    command = captured["command"]
    assert result is expected
    assert command.invoice_id == INVOICE_ID
    assert command.context.actor == ACTOR
    assert command.context.scope == "billing:reconciliation:write"
    assert command.context.idempotency_key == f"billing-calendar-admin:{'8' * 32}"
    assert len(released) == 1


def test_confirmation_rejects_changed_actor(monkeypatch):
    monkeypatch.setattr(
        web_service.context_signing,
        "verify_context_token",
        lambda _db, _token: _claims(actor="system_user:someone-else"),
    )

    with pytest.raises(web_service.PrepaidBillingCalendarAdminError) as exc:
        web_service.confirm_admin_review(
            object(),
            invoice_id=INVOICE_ID,
            actor=ACTOR,
            preview_fingerprint=FINGERPRINT,
            confirmation_token="signed",
            confirmed="yes",
            reason="Reviewed historical UTC-to-WAT repair.",
        )

    assert exc.value.code.endswith("confirmation_context_changed")


def test_templates_and_routes_expose_review_only_workflow_and_granular_permissions():
    root = Path(__file__).resolve().parents[1]
    queue_template = (
        root / "templates/admin/billing/prepaid_billing_calendar_reconciliation.html"
    ).read_text()
    confirm_template = (
        root / "templates/admin/billing/prepaid_billing_calendar_confirm.html"
    ).read_text()
    route_source = (
        root / "app/web/admin/billing_calendar_reconciliation.py"
    ).read_text()

    assert '{% include "components/forms/csrf_input.html" %}' in queue_template
    assert "can(request, 'billing:reconciliation:write')" in queue_template
    assert "Blocked rows have no automatic action" in queue_template
    assert "economic delta" in confirm_template.lower()
    assert "Access consequence" in confirm_template
    assert "Active enforcement reasons" in confirm_template
    assert "action_form(review.action_form)" in confirm_template
    assert "service.READ_PERMISSION" in route_source
    assert "service.WRITE_PERMISSION" in route_source
    assert "Invoice." not in route_source
    assert "Subscription." not in route_source


def test_reconciliation_permissions_are_seeded_for_auditor_and_finance_roles():
    from scripts.seed.seed_rbac import DEFAULT_PERMISSIONS, ROLE_PERMISSIONS

    seeded = {key for key, _description in DEFAULT_PERMISSIONS}
    assert {
        "billing:reconciliation:read",
        "billing:reconciliation:write",
    } <= seeded
    assert "billing:reconciliation:read" in ROLE_PERMISSIONS["auditor"]
    assert "billing:reconciliation:write" not in ROLE_PERMISSIONS["auditor"]
    assert "billing:reconciliation:read" in ROLE_PERMISSIONS["finance_manager"]
    assert "billing:reconciliation:write" in ROLE_PERMISSIONS["finance_manager"]
