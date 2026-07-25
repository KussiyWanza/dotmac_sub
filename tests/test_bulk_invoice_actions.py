"""Bulk invoice actions money-correctness (review #A5).

- bulk_mark_paid records a real payment so the 'paid' status survives a recalc
  (the raw status poke silently reverted).
- bulk_void (service) skips paid/void invoices instead of stranding payments.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.billing import Invoice, InvoiceStatus
from app.services import billing as billing_service
from app.services.billing._common import _recalculate_invoice_totals
from app.services.web_billing_invoice_bulk import (
    bulk_mark_paid,
    bulk_mark_paid_result,
    preview_invoice_bulk_action,
    require_invoice_bulk_confirmation,
)
from app.services.web_billing_invoice_bulk_actions import (
    build_invoice_bulk_action_contract,
    build_invoice_bulk_review,
)


def _issued(db, subscriber, num):
    inv = Invoice(
        account_id=subscriber.id,
        invoice_number=num,
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        currency="NGN",
        metadata_={},
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_bulk_review_projects_exact_scope_into_shared_action_form(
    db_session, subscriber
):
    eligible = _issued(db_session, subscriber, "INV-BULK-REVIEW")
    missing_id = uuid4()

    state = build_invoice_bulk_review(
        db_session,
        action_key="mark_paid",
        invoice_ids_csv=f"{eligible.id},{missing_id}",
    )

    preview = state["bulk_preview"]
    form = state["bulk_action_form"]
    assert preview.eligible_ids == (str(eligible.id),)
    assert preview.skipped[0]["id"] == str(missing_id)
    assert form.action_url.endswith("/confirm/mark-paid")
    assert {item.key: item.value for item in form.hidden_values}[
        "expected_scope_token"
    ] == preview.scope_token


def test_bulk_mark_paid_survives_recalc(db_session, subscriber):
    inv = _issued(db_session, subscriber, "INV-BULK-1")
    updated = bulk_mark_paid(db_session, str(inv.id))
    assert updated == [str(inv.id)]

    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid
    assert inv.balance_due == Decimal("0.00")

    # The key fix: a later recalc must NOT revert it (a real allocation backs it).
    _recalculate_invoice_totals(db_session, inv)
    db_session.flush()
    assert inv.status == InvoiceStatus.paid


def test_bulk_mark_paid_result_reports_skipped_invoices(db_session, subscriber):
    eligible = _issued(db_session, subscriber, "INV-BULK-RESULT-ELIGIBLE")
    draft = _issued(db_session, subscriber, "INV-BULK-RESULT-DRAFT")
    draft.status = InvoiceStatus.draft
    db_session.commit()

    result = bulk_mark_paid_result(
        db_session,
        f"{eligible.id},{draft.id},{uuid4()}",
    )

    assert result.selected == 3
    assert result.processed_ids == [str(eligible.id)]
    assert result.skipped_ids[0] == str(draft.id)
    assert len(result.skipped_ids) == 2
    assert result.failed_ids == []
    assert result.message("Marked paid") == (
        "Marked paid 1 of 3 selected invoices; 2 skipped"
    )


def test_bulk_void_service_skips_paid_invoice(db_session, subscriber):
    from app.schemas.billing import InvoiceBulkVoidRequest

    paid = _issued(db_session, subscriber, "INV-BULK-PAID")
    paid.status = InvoiceStatus.paid
    paid.balance_due = Decimal("0.00")
    issued = _issued(db_session, subscriber, "INV-BULK-ISSUED")
    db_session.commit()

    count = billing_service.invoices.bulk_void(
        db_session,
        InvoiceBulkVoidRequest(invoice_ids=[str(paid.id), str(issued.id)]),
    )
    db_session.refresh(paid)
    db_session.refresh(issued)
    assert count == 1  # only the issued one voided
    assert paid.status == InvoiceStatus.paid  # paid invoice NOT voided
    assert issued.status == InvoiceStatus.void


def test_invoice_bulk_contract_projects_authorization_and_page_eligibility(
    db_session, subscriber
):
    draft = _issued(db_session, subscriber, "INV-CONTRACT-DRAFT")
    draft.status = InvoiceStatus.draft
    issued = _issued(db_session, subscriber, "INV-CONTRACT-ISSUED")
    db_session.commit()

    contract = build_invoice_bulk_action_contract(
        db_session,
        auth={"principal_id": str(uuid4()), "roles": ["admin"]},
        invoices=[draft, issued],
    )
    actions = {action["key"]: action for action in contract["actions"]}

    assert contract["selection_enabled"] is True
    assert str(draft.id) in actions["issue"]["eligible_ids"]
    assert actions["issue"]["ineligible_reasons"][str(issued.id)] == "Already issued"
    assert str(issued.id) in actions["mark_paid"]["eligible_ids"]
    assert actions["mark_paid"]["ineligible_reasons"][str(draft.id)] == (
        "Invoice is not open for payment"
    )

    unauthorized = build_invoice_bulk_action_contract(
        db_session,
        auth={},
        invoices=[draft, issued],
    )
    assert unauthorized["selection_enabled"] is False
    assert unauthorized["actions"] == []


def test_invoice_bulk_confirmation_detects_eligibility_drift(db_session, subscriber):
    draft = _issued(db_session, subscriber, "INV-PREVIEW-DRAFT")
    draft.status = InvoiceStatus.draft
    issued = _issued(db_session, subscriber, "INV-PREVIEW-ISSUED")
    db_session.commit()
    ids_csv = f"{draft.id},{issued.id}"

    preview = preview_invoice_bulk_action(
        db_session,
        action="issue",
        invoice_ids_csv=ids_csv,
    )

    assert preview.eligible_ids == (str(draft.id),)
    assert preview.skipped[0]["id"] == str(issued.id)
    assert preview.skipped[0]["reason"] == "Already issued"
    confirmed = require_invoice_bulk_confirmation(
        db_session,
        action="issue",
        invoice_ids_csv=ids_csv,
        expected_count=len(preview.resolved_ids),
        expected_scope_token=preview.scope_token,
    )
    assert confirmed.scope_token == preview.scope_token

    issued.status = InvoiceStatus.draft
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        require_invoice_bulk_confirmation(
            db_session,
            action="issue",
            invoice_ids_csv=ids_csv,
            expected_count=len(preview.resolved_ids),
            expected_scope_token=preview.scope_token,
        )
    assert exc_info.value.status_code == 409


def test_invoice_bulk_preview_treats_malformed_ids_as_missing(db_session, subscriber):
    issued = _issued(db_session, subscriber, "INV-PREVIEW-MALFORMED")

    preview = preview_invoice_bulk_action(
        db_session,
        action="mark_paid",
        invoice_ids_csv=f"not-a-uuid,{issued.id}",
    )

    assert preview.resolved_ids == (str(issued.id),)
    assert preview.eligible_ids == (str(issued.id),)
    assert preview.skipped == ({"id": "not-a-uuid", "reason": "Invoice not found"},)


def test_invoice_bulk_route_requires_preview_confirmation(db_session):
    from app.web.admin import billing_invoice_bulk as bulk_routes

    with pytest.raises(HTTPException) as unconfirmed:
        bulk_routes._require_confirmed_invoice_scope(
            db_session,
            action="issue",
            invoice_ids="not-a-uuid",
            confirmed=False,
            expected_count=None,
            expected_scope_token=None,
        )
    assert unconfirmed.value.status_code == 400
    assert unconfirmed.value.detail == "Invoice action confirmation required"

    with pytest.raises(HTTPException) as missing_preview:
        bulk_routes._require_confirmed_invoice_scope(
            db_session,
            action="issue",
            invoice_ids="not-a-uuid",
            confirmed=True,
            expected_count=None,
            expected_scope_token=None,
        )
    assert missing_preview.value.status_code == 400
    assert (
        missing_preview.value.detail == "Preview the invoice action before confirming"
    )
