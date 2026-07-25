"""Server-owned bulk interaction projection for the admin invoice list."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.billing import Invoice
from app.services import web_billing_invoice_bulk as invoice_bulk_service
from app.services.action_forms import (
    ActionConfirmation,
    ActionForm,
    ActionHiddenValue,
    ActionTone,
)
from app.services.auth_dependencies import has_permission
from app.services.bulk_actions import BulkActionDefinition, BulkResourceDefinition

INVOICE_BULK_ACTION_DEFINITION = BulkResourceDefinition(
    key="billing_invoices",
    actions=(
        BulkActionDefinition(
            key="issue",
            label="Issue",
            description="Issue selected draft invoices.",
            permission="billing:invoice:update",
            tone="info",
        ),
        BulkActionDefinition(
            key="send",
            label="Send",
            description="Queue email delivery for eligible issued invoices.",
            permission="billing:invoice:update",
            tone="positive",
        ),
        BulkActionDefinition(
            key="void",
            label="Void",
            description="Void eligible invoices and reverse their ledger effect.",
            permission="billing:invoice:delete",
            tone="negative",
        ),
        BulkActionDefinition(
            key="mark_paid",
            label="Mark paid",
            description="Record and allocate payment for open invoice balances.",
            permission="billing:invoice:update",
            tone="positive",
        ),
        BulkActionDefinition(
            key="generate_pdf",
            label="Generate PDFs",
            description="Queue PDF generation for selected invoices.",
            permission="billing:invoice:read",
            tone="info",
            execution_mode="queued",
            result_reference="/admin/billing/invoices/bulk/pdf-ready",
        ),
        BulkActionDefinition(
            key="export_csv",
            label="Export CSV",
            description="Download selected invoices as CSV.",
            permission="billing:invoice:read",
            tone="neutral",
            requires_preview=False,
            requires_confirmation=False,
        ),
        BulkActionDefinition(
            key="export_pdf",
            label="Export PDFs",
            description="Download ready invoice PDFs as a ZIP archive.",
            permission="billing:invoice:read",
            tone="neutral",
            requires_preview=False,
            requires_confirmation=False,
        ),
    ),
)

_REVIEW_ACTIONS = {"issue", "send", "mark_paid", "generate_pdf"}


def invoice_bulk_action_definition(action_key: str) -> BulkActionDefinition:
    for action in INVOICE_BULK_ACTION_DEFINITION.actions:
        if action.key == action_key:
            return action
    raise ValueError("Unsupported invoice bulk action")


def invoice_bulk_review_action_definition(action_key: str) -> BulkActionDefinition:
    if action_key not in _REVIEW_ACTIONS:
        raise ValueError("Unsupported invoice bulk review action")
    return invoice_bulk_action_definition(action_key)


def build_invoice_bulk_review(
    db: Session,
    *,
    action_key: str,
    invoice_ids_csv: str,
) -> dict[str, object]:
    """Build one server-rendered exact-scope review and shared action form."""
    definition = invoice_bulk_review_action_definition(action_key)
    preview = invoice_bulk_service.preview_invoice_bulk_action(
        db,
        action=action_key,
        invoice_ids_csv=invoice_ids_csv,
    )
    endpoint = action_key.replace("_", "-")
    allowed = bool(preview.eligible_ids)
    form = ActionForm(
        key=f"invoice_bulk.{action_key}",
        title=f"Confirm {definition.label.lower()}",
        description=definition.description,
        action_url=f"/admin/billing/invoices/bulk/confirm/{endpoint}",
        submit_label=definition.label,
        fields=(),
        tone=(
            ActionTone.positive
            if definition.tone == "positive"
            else ActionTone.negative
            if definition.tone == "negative"
            else ActionTone.neutral
        ),
        impact=(
            f"{len(preview.eligible_ids)} of {len(preview.selected_ids)} selected "
            f"invoice(s) are eligible; {len(preview.skipped)} will be skipped."
        ),
        confirmation=ActionConfirmation(
            title=f"Confirm {definition.label.lower()}",
            message=(
                "The exact selected membership and eligibility are rechecked "
                "before execution."
            ),
        ),
        hidden_values=(
            ActionHiddenValue(key="invoice_ids", value=",".join(preview.selected_ids)),
            ActionHiddenValue(
                key="expected_count",
                value=str(len(preview.resolved_ids)),
            ),
            ActionHiddenValue(
                key="expected_scope_token",
                value=preview.scope_token,
            ),
        ),
        allowed=allowed,
        disabled_reason=(
            None if allowed else "No selected invoices are eligible for this action."
        ),
    )
    return {
        "action_definition": definition,
        "bulk_preview": preview,
        "bulk_action_form": form,
    }


def build_invoice_bulk_action_contract(
    db: Session,
    *,
    auth: dict,
    invoices: Sequence[Invoice],
) -> dict[str, object]:
    """Project authorized actions and page-row eligibility without policy copies."""

    declared_permissions = {
        action.permission for action in INVOICE_BULK_ACTION_DEFINITION.actions
    }
    authorized_permissions = {
        permission
        for permission in declared_permissions
        if auth and has_permission(auth, db, permission)
    }
    contract = INVOICE_BULK_ACTION_DEFINITION.project(
        authorized_permissions=authorized_permissions
    ).as_dict()
    actions = contract["actions"]
    assert isinstance(actions, list)
    for action in actions:
        assert isinstance(action, dict)
        action_key = str(action["key"])
        eligible_ids: list[str] = []
        ineligible_reasons: dict[str, str] = {}
        for invoice in invoices:
            invoice_id = str(invoice.id)
            reason = invoice_bulk_service.invoice_bulk_action_ineligibility(
                invoice, action_key
            )
            if reason:
                ineligible_reasons[invoice_id] = reason
            else:
                eligible_ids.append(invoice_id)
        action["eligible_ids"] = eligible_ids
        action["ineligible_reasons"] = ineligible_reasons
    return contract
