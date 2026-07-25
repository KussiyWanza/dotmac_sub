"""Service helpers for billing dunning web routes."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import selectinload

from app.models.collections import (
    DunningActionLog,
    DunningCase,
    DunningCaseStatus,
    FinancialAccessConsequence,
)
from app.services import collections as collections_service
from app.services import display_format, dunning_staff_actions
from app.services import web_billing_customers as web_billing_customers_service
from app.services.action_forms import (
    ActionConfirmation,
    ActionForm,
    ActionFormSubmission,
    ActionHiddenValue,
    ActionTone,
)
from app.services.bulk_actions import BulkActionDefinition, BulkResourceDefinition
from app.services.collections import _core as dunning_owner

_DUNNING_BULK_RESOURCE = BulkResourceDefinition(
    key="dunning_cases",
    actions=(
        BulkActionDefinition(
            key=dunning_owner.DunningStaffAction.pause.value,
            label="Pause selected",
            description="Pause eligible open dunning cases.",
            permission=dunning_staff_actions.ACTION_SCOPE,
            tone="warning",
        ),
        BulkActionDefinition(
            key=dunning_owner.DunningStaffAction.resume.value,
            label="Resume selected",
            description="Resume eligible paused dunning cases.",
            permission=dunning_staff_actions.ACTION_SCOPE,
            tone="positive",
        ),
    ),
)

_ACTION_KEYS = {
    dunning_owner.DunningStaffAction.pause: "dunning.pause",
    dunning_owner.DunningStaffAction.resume: "dunning.resume",
    dunning_owner.DunningStaffAction.close: "dunning.close",
}


@dataclass(frozen=True, slots=True)
class DunningImpactRow:
    case_id: str
    account_id: str
    current_status: str
    resulting_status: str
    eligible: bool
    reason: str
    receivables: str


def build_listing_data(
    db,
    *,
    page: int,
    per_page: int = 50,
    status: str | None,
    customer_ref: str | None,
    can_write: bool,
) -> dict[str, object]:
    """Build paginated listing data and status counts for dunning page."""
    offset = (page - 1) * per_page

    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [
            item["id"]
            for item in web_billing_customers_service.accounts_for_customer(
                db, customer_ref
            )
        ]

    if customer_filtered and not account_ids:
        status_counts = {
            "open": 0,
            "paused": 0,
            "resolved": 0,
            "closed": 0,
        }
    else:
        status_query = db.query(DunningCase)
        if account_ids:
            status_query = status_query.filter(DunningCase.account_id.in_(account_ids))
        status_counts = {
            "open": status_query.filter(
                DunningCase.status == DunningCaseStatus.open
            ).count(),
            "paused": status_query.filter(
                DunningCase.status == DunningCaseStatus.paused
            ).count(),
            "resolved": status_query.filter(
                DunningCase.status == DunningCaseStatus.resolved
            ).count(),
            "closed": status_query.filter(
                DunningCase.status == DunningCaseStatus.closed
            ).count(),
        }

    cases = []
    total = 0
    total_pages = 1
    if account_ids:
        query = db.query(DunningCase).filter(DunningCase.account_id.in_(account_ids))
        if status:
            query = query.filter(DunningCase.status == status)
        total = query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        cases = (
            query.order_by(DunningCase.created_at.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
    elif not customer_filtered:
        count_query = db.query(DunningCase)
        if status:
            count_query = count_query.filter(DunningCase.status == status)
        total = count_query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        cases = collections_service.dunning_cases.list(
            db=db,
            account_id=None,
            status=status,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
        )

    return {
        "cases": cases,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": status,
        "status_counts": status_counts,
        "customer_ref": customer_ref,
        "dunning_bulk_action_contract": _DUNNING_BULK_RESOURCE.project(
            authorized_permissions=(
                {dunning_staff_actions.ACTION_SCOPE} if can_write else set()
            )
        ).as_dict(),
    }


def _action_impact(
    impact: dunning_owner.DunningStaffCaseImpact,
    action: dunning_owner.DunningStaffAction,
) -> str:
    current = (
        impact.current_status.value.title() if impact.current_status else "Missing"
    )
    resulting = (
        impact.resulting_status.value.title()
        if impact.resulting_status
        else "No change"
    )
    if action is dunning_owner.DunningStaffAction.close:
        if impact.eligible:
            return (
                f"{current} → {resulting}. Closing this case does not restore "
                "service access; financial reconciliation remains authoritative."
            )
        receivables = display_format.format_currency_groups(
            dict(impact.unpaid_receivables)
        )
        return (
            f"No state change. {impact.reason or 'Case is ineligible'} "
            f"Current collectible receivables: {receivables}."
        )
    verb = (
        "automated collection progression stops"
        if action.value == "pause"
        else "automated collection progression resumes"
    )
    return f"{current} → {resulting}; {verb} for this case."


def _single_action_form(
    preview: dunning_owner.DunningStaffActionPreview,
) -> ActionForm:
    impact = preview.impacts[0]
    action = preview.action
    title = {
        dunning_owner.DunningStaffAction.pause: "Pause dunning case",
        dunning_owner.DunningStaffAction.resume: "Resume dunning case",
        dunning_owner.DunningStaffAction.close: "Close dunning case",
    }[action]
    description = {
        dunning_owner.DunningStaffAction.pause: (
            "Stop automated collection progression without clearing debt or access locks."
        ),
        dunning_owner.DunningStaffAction.resume: (
            "Return this case to active automated collection progression."
        ),
        dunning_owner.DunningStaffAction.close: (
            "End collection workflow only after collectible receivables are clear."
        ),
    }[action]
    tone = (
        ActionTone.negative
        if action is dunning_owner.DunningStaffAction.close
        else ActionTone.neutral
    )
    return ActionForm(
        key=_ACTION_KEYS[action],
        title=title,
        description=description,
        action_url=(f"/admin/billing/dunning/{impact.case_id}/{action.value}/confirm"),
        submit_label=title,
        fields=(),
        tone=tone,
        impact=_action_impact(impact, action),
        confirmation=ActionConfirmation(
            title=f"Confirm {action.value}",
            message=(
                "I reviewed the current case state and consequence and authorize "
                f"this {action.value} action."
            ),
        ),
        hidden_values=(
            ActionHiddenValue(
                key="preview_fingerprint",
                value=preview.fingerprint,
            ),
        ),
        allowed=impact.eligible,
        disabled_reason=impact.reason,
    )


def _detail_action_forms(
    db,
    *,
    case_id: str,
    status: DunningCaseStatus,
    can_write: bool,
    submission: ActionFormSubmission | None,
) -> tuple[ActionForm, ...]:
    if not can_write or status in {
        DunningCaseStatus.resolved,
        DunningCaseStatus.closed,
    }:
        return ()
    actions = (
        (
            dunning_owner.DunningStaffAction.pause,
            dunning_owner.DunningStaffAction.close,
        )
        if status is DunningCaseStatus.open
        else (
            dunning_owner.DunningStaffAction.resume,
            dunning_owner.DunningStaffAction.close,
        )
    )
    forms = tuple(
        _single_action_form(
            dunning_staff_actions.preview_staff_action(
                db,
                action=action,
                case_ids=(case_id,),
            )
        )
        for action in actions
    )
    if submission is None:
        return forms
    return tuple(
        form.bind(submission) if form.key == submission.action_key else form
        for form in forms
    )


def build_detail_data(
    db,
    *,
    case_id: str,
    can_write: bool,
    submission: ActionFormSubmission | None = None,
) -> dict[str, object]:
    case = collections_service.dunning_cases.get(db, case_id)
    actions = (
        db.query(DunningActionLog)
        .filter(DunningActionLog.case_id == case.id)
        .options(
            selectinload(DunningActionLog.access_consequence).selectinload(
                FinancialAccessConsequence.evidence
            )
        )
        .order_by(DunningActionLog.executed_at.desc())
        .limit(100)
        .all()
    )
    return {
        "case": case,
        "account": case.subscriber,
        "actions": actions,
        "case_actions": _detail_action_forms(
            db,
            case_id=case_id,
            status=case.status,
            can_write=can_write,
            submission=submission,
        ),
    }


def _impact_rows(
    preview: dunning_owner.DunningStaffActionPreview,
) -> tuple[DunningImpactRow, ...]:
    return tuple(
        DunningImpactRow(
            case_id=str(impact.case_id),
            account_id=str(impact.account_id or "—"),
            current_status=(
                impact.current_status.value.title()
                if impact.current_status
                else "Missing"
            ),
            resulting_status=(
                impact.resulting_status.value.title()
                if impact.resulting_status
                else "No change"
            ),
            eligible=impact.eligible,
            reason=impact.reason or "Eligible",
            receivables=(
                display_format.format_currency_groups(dict(impact.unpaid_receivables))
                if impact.unpaid_receivables
                else "—"
            ),
        )
        for impact in preview.impacts
    )


def build_bulk_preview_data(
    db,
    *,
    action: dunning_owner.DunningStaffAction,
    case_ids_csv: str,
) -> dict[str, object]:
    """Project an exact selected-scope preview and shared confirmation form."""

    preview = dunning_staff_actions.preview_staff_action(
        db,
        action=action,
        case_ids=tuple(case_ids_csv.split(",")),
    )
    case_ids = ",".join(str(case_id) for case_id in preview.requested_case_ids)
    action_label = action.value.title()
    form = ActionForm(
        key=f"dunning.bulk.{action.value}",
        title=f"{action_label} selected dunning cases",
        description=(
            "Apply the action only to the eligible cases shown below. "
            "Skipped cases will remain unchanged."
        ),
        action_url=f"/admin/billing/dunning/bulk/{action.value}/confirm",
        submit_label=f"Confirm {action.value}",
        fields=(),
        tone=(
            ActionTone.positive
            if action is dunning_owner.DunningStaffAction.resume
            else ActionTone.neutral
        ),
        impact=(
            f"{preview.eligible_count} of {preview.selected_count} selected cases "
            f"will be {action.value}d; {preview.skipped_count} will be skipped."
        ),
        confirmation=ActionConfirmation(
            title=f"Confirm bulk {action.value}",
            message=(
                "I reviewed the exact eligible and skipped scope and authorize "
                f"this {action.value} action."
            ),
        ),
        hidden_values=(
            ActionHiddenValue(key="case_ids", value=case_ids),
            ActionHiddenValue(
                key="preview_fingerprint",
                value=preview.fingerprint,
            ),
        ),
        allowed=preview.eligible_count > 0,
        disabled_reason=(
            None
            if preview.eligible_count
            else "None of the selected cases is eligible for this action."
        ),
    )
    return {
        "bulk_preview": preview,
        "bulk_impact_rows": _impact_rows(preview),
        "bulk_action_form": form,
    }


def action_error_submission(
    *,
    action: dunning_owner.DunningStaffAction,
    error: Exception,
) -> ActionFormSubmission:
    message = getattr(error, "message", str(error))
    return ActionFormSubmission.from_mapping(
        _ACTION_KEYS[action],
        {},
        general_error=message,
    )
