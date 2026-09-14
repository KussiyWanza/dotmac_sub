"""Typed work-order expense form and claim projection for the staff web UI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
)
from app.models.field_expense import FieldExpenseRequest
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.domain_errors import DomainError
from app.services.dotmac_erp.expense_form_contracts import (
    ExpenseBankOption,
    ExpenseDestinationMode,
    ExpenseProfileDestination,
)
from app.services.field.expense_categories import (
    ExpenseCategoryQueryError,
    ListExpenseCategories,
    list_expense_categories,
)
from app.services.field.expense_requests import (
    ExpenseCategoryRule,
    ExpenseReceiptUploadInput,
    FieldExpenseRequestError,
    FieldExpenseVendorOption,
    GetFieldExpenseFormContext,
    ListFieldExpenseVendors,
    evaluate_expense_work_order_eligibility,
    get_field_expense_form_context,
    list_expense_vendors,
)
from app.services.integrations.diagnostics import (
    DELIVERY_DIAGNOSTIC_KEY,
    parse_diagnostic_evidence,
    safe_diagnostic_summary,
)
from app.services.status_presentation import (
    StatusPresentation,
    field_expense_status_presentation,
)
from app.services.ui_contracts import Action


class ExpenseDeliveryState(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    PENDING = "pending"
    ACCEPTED = "accepted"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ExpenseFieldError:
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class ExpenseLineFormInput:
    key: str
    category_code: str
    description: str
    amount: str
    expense_date: str
    vendor_name: str
    receipt_url: str
    notes: str
    receipt_upload: ExpenseReceiptUploadInput | None = None


@dataclass(frozen=True, slots=True)
class WorkOrderExpenseFormInput:
    request_id: str
    purpose: str
    expense_date: str
    currency: str
    notes: str
    selected_approver_id: str
    payment_destination_mode: str
    bank_code: str
    account_number: str
    beneficiary_name: str
    lines: tuple[ExpenseLineFormInput, ...]


@dataclass(frozen=True, slots=True)
class ExpenseApproverView:
    erp_employee_id: UUID
    system_user_id: UUID
    display_name: str
    email: str


@dataclass(frozen=True, slots=True)
class PreparedExpenseLine:
    category_code: str
    category_name: str
    description: str
    amount: Decimal
    expense_date: date | None
    vendor_name: str | None
    receipt_url: str | None
    notes: str | None
    receipt_upload: ExpenseReceiptUploadInput | None


@dataclass(frozen=True, slots=True)
class PreparedWorkOrderExpense:
    request_id: UUID
    purpose: str
    expense_date: date
    currency: str
    notes: str | None
    lines: tuple[PreparedExpenseLine, ...]
    category_rules: tuple[ExpenseCategoryRule, ...]
    selected_approver: ExpenseApproverView
    payment_destination_mode: ExpenseDestinationMode
    bank_code: str | None
    account_number: str | None
    beneficiary_name: str | None


@dataclass(frozen=True, slots=True)
class WorkOrderExpenseClaimView:
    id: UUID
    purpose: str
    expense_date: date | None
    currency: str
    total_amount: Decimal
    status: StatusPresentation
    delivery_state: ExpenseDeliveryState
    delivery_label: str
    delivery_detail: str | None
    erp_claim_number: str | None
    erp_claim_status: str | None
    rejection_reason: str | None


@dataclass(frozen=True, slots=True)
class WorkOrderExpensePanel:
    work_order_id: UUID
    work_order_public_id: str
    claims: tuple[WorkOrderExpenseClaimView, ...]
    categories: tuple[ExpenseCategoryRule, ...]
    vendors: tuple[FieldExpenseVendorOption, ...]
    approvers: tuple[ExpenseApproverView, ...]
    banks: tuple[ExpenseBankOption, ...]
    profile_destination: ExpenseProfileDestination
    create_action: Action
    form: WorkOrderExpenseFormInput
    errors: tuple[ExpenseFieldError, ...]
    form_open: bool
    category_message: str | None

    def error_for(self, field: str) -> str | None:
        return next(
            (error.message for error in self.errors if error.field == field),
            None,
        )


class WorkOrderExpenseFormError(DomainError):
    def __init__(
        self,
        *,
        message: str,
        form: WorkOrderExpenseFormInput,
        errors: tuple[ExpenseFieldError, ...],
    ) -> None:
        self.form = form
        self.errors = errors
        super().__init__(
            code="ui.work_order_expense_projection.invalid_form",
            message=message,
            details={"fields": tuple(error.field for error in errors)},
        )


def default_expense_form() -> WorkOrderExpenseFormInput:
    return WorkOrderExpenseFormInput(
        request_id=str(uuid4()),
        purpose="",
        expense_date=date.today().isoformat(),
        currency="NGN",
        notes="",
        selected_approver_id="",
        payment_destination_mode="erp_profile",
        bank_code="",
        account_number="",
        beneficiary_name="",
        lines=(_empty_line(),),
    )


def build_work_order_expense_panel(
    db: Session,
    *,
    work_order_public_id: str,
    actor_system_user_id: UUID,
    form: WorkOrderExpenseFormInput | None = None,
    errors: tuple[ExpenseFieldError, ...] = (),
) -> WorkOrderExpensePanel:
    work_order = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.public_id == work_order_public_id,
            WorkOrder.is_active.is_(True),
        )
        .one_or_none()
    )
    user = db.get(SystemUser, actor_system_user_id)
    if work_order is None or user is None or not user.is_active:
        raise WorkOrderExpenseFormError(
            message="The work order or requesting staff user is unavailable.",
            form=form or default_expense_form(),
            errors=(ExpenseFieldError("form", "Expense entry is unavailable."),),
        )

    categories: tuple[ExpenseCategoryRule, ...] = ()
    category_message: str | None = None
    try:
        observed = list_expense_categories(db, ListExpenseCategories())
        categories = tuple(
            ExpenseCategoryRule(
                category_code=item.category_code,
                category_name=item.category_name,
                requires_receipt=item.requires_receipt,
                max_amount_per_claim=item.max_amount_per_claim,
            )
            for item in observed
        )
        if not categories:
            category_message = "ERP has no active expense categories."
    except ExpenseCategoryQueryError:
        category_message = "Expense categories are temporarily unavailable from ERP."

    approvers: tuple[ExpenseApproverView, ...] = ()
    banks: tuple[ExpenseBankOption, ...] = ()
    profile_destination = ExpenseProfileDestination(available=False)
    try:
        context = get_field_expense_form_context(
            db,
            GetFieldExpenseFormContext(requester_system_user_id=user.id),
        )
        banks = context.banks
        profile_destination = context.profile_destination
        approvers = tuple(
            ExpenseApproverView(
                erp_employee_id=item.erp_employee_id,
                system_user_id=item.system_user_id,
                display_name=item.display_name,
                email=item.email,
            )
            for item in context.approvers
        )
        if not approvers:
            category_message = "No ERP expense approver has a matching active Sub user."
    except FieldExpenseRequestError:
        category_message = "Expense approvers and payment details are temporarily unavailable from ERP."

    vendors = list_expense_vendors(
        db=db,
        query=ListFieldExpenseVendors(limit=100),
    )
    claims = _claim_views(db, work_order=work_order, user=user)
    eligibility = evaluate_expense_work_order_eligibility(
        db,
        work_order=work_order,
    )
    action_reason = eligibility.reason or category_message
    create_action = Action(
        key="create_work_order_expense",
        label="New Expense Claim",
        allowed=action_reason is None,
        reason=action_reason,
        permission="operations:dispatch:read",
    )
    return WorkOrderExpensePanel(
        work_order_id=work_order.id,
        work_order_public_id=work_order.public_id,
        claims=claims,
        categories=categories,
        vendors=vendors,
        approvers=approvers,
        banks=banks,
        profile_destination=profile_destination,
        create_action=create_action,
        form=form or default_expense_form(),
        errors=errors,
        form_open=bool(errors),
        category_message=category_message,
    )


def validate_work_order_expense_form(
    form: WorkOrderExpenseFormInput,
    *,
    category_rules: tuple[ExpenseCategoryRule, ...],
    approvers: tuple[ExpenseApproverView, ...],
) -> PreparedWorkOrderExpense:
    errors: list[ExpenseFieldError] = []
    try:
        request_id = UUID(form.request_id)
    except ValueError:
        request_id = uuid4()
        errors.append(ExpenseFieldError("form", "Refresh the form and try again."))

    purpose = form.purpose.strip()
    if not purpose:
        errors.append(ExpenseFieldError("purpose", "Purpose is required."))
    elif len(purpose) > 500:
        errors.append(
            ExpenseFieldError("purpose", "Purpose must not exceed 500 characters.")
        )
    expense_date = _date_value(form.expense_date, "expense_date", errors, required=True)
    currency = form.currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        errors.append(
            ExpenseFieldError("currency", "Use a three-letter currency code.")
        )
    notes = form.notes.strip() or None
    if notes and len(notes) > 2000:
        errors.append(
            ExpenseFieldError("notes", "Notes must not exceed 2,000 characters.")
        )
    try:
        selected_approver_id = UUID(form.selected_approver_id)
    except ValueError:
        selected_approver_id = None
    selected_approver = next(
        (item for item in approvers if item.erp_employee_id == selected_approver_id),
        None,
    )
    if selected_approver is None:
        errors.append(
            ExpenseFieldError("selected_approver_id", "Select an expense approver.")
        )
    try:
        destination_mode = ExpenseDestinationMode(form.payment_destination_mode.strip())
    except ValueError:
        destination_mode = ExpenseDestinationMode.ERP_PROFILE
        errors.append(
            ExpenseFieldError("payment_destination_mode", "Select payment details.")
        )
    bank_code = form.bank_code.strip() or None
    account_number = form.account_number.strip() or None
    beneficiary_name = form.beneficiary_name.strip() or None
    if destination_mode is ExpenseDestinationMode.EXPENSE_OVERRIDE:
        if not bank_code:
            errors.append(ExpenseFieldError("bank_code", "Select a bank."))
        if not account_number or not account_number.isdigit():
            errors.append(
                ExpenseFieldError("account_number", "Enter a valid account number.")
            )
        if not beneficiary_name:
            errors.append(
                ExpenseFieldError("beneficiary_name", "Enter the beneficiary name.")
            )
    if not form.lines:
        errors.append(ExpenseFieldError("lines", "Add at least one expense item."))
    elif len(form.lines) > 50:
        errors.append(
            ExpenseFieldError("lines", "A claim may contain at most 50 items.")
        )

    rules = {rule.category_code: rule for rule in category_rules}
    if not rules:
        errors.append(
            ExpenseFieldError("lines", "Expense categories are unavailable from ERP.")
        )
    prepared_lines: list[PreparedExpenseLine] = []
    totals: dict[str, Decimal] = {}
    for line in form.lines:
        prefix = f"line.{line.key}"
        rule = rules.get(line.category_code.strip())
        if rule is None:
            errors.append(
                ExpenseFieldError(
                    f"{prefix}.category_code",
                    "Select an available expense category.",
                )
            )
        description = line.description.strip()
        if not description:
            errors.append(
                ExpenseFieldError(f"{prefix}.description", "Description is required.")
            )
        elif len(description) > 500:
            errors.append(
                ExpenseFieldError(
                    f"{prefix}.description",
                    "Description must not exceed 500 characters.",
                )
            )
        amount = _amount_value(line.amount, f"{prefix}.amount", errors)
        line_date = _date_value(
            line.expense_date,
            f"{prefix}.expense_date",
            errors,
            required=False,
        )
        vendor_name = line.vendor_name.strip() or None
        if vendor_name and len(vendor_name) > 200:
            errors.append(
                ExpenseFieldError(
                    f"{prefix}.vendor_name",
                    "Vendor must not exceed 200 characters.",
                )
            )
        receipt_url = line.receipt_url.strip() or None
        if receipt_url and not _valid_receipt_url(receipt_url):
            errors.append(
                ExpenseFieldError(
                    f"{prefix}.receipt_url",
                    "Receipt URL must start with http:// or https://.",
                )
            )
        line_notes = line.notes.strip() or None
        if line_notes and len(line_notes) > 2000:
            errors.append(
                ExpenseFieldError(
                    f"{prefix}.notes",
                    "Line notes must not exceed 2,000 characters.",
                )
            )
        if (
            rule is not None
            and rule.requires_receipt
            and not (receipt_url or line.receipt_upload)
        ):
            errors.append(
                ExpenseFieldError(
                    f"{prefix}.receipt",
                    f"A receipt is required for {rule.category_name}.",
                )
            )
        if rule is not None and amount is not None:
            totals[rule.category_code] = (
                totals.get(rule.category_code, Decimal("0")) + amount
            )
            prepared_lines.append(
                PreparedExpenseLine(
                    category_code=rule.category_code,
                    category_name=rule.category_name,
                    description=description,
                    amount=amount,
                    expense_date=line_date,
                    vendor_name=vendor_name,
                    receipt_url=receipt_url,
                    notes=line_notes,
                    receipt_upload=line.receipt_upload,
                )
            )
    for category_code, total in totals.items():
        rule = rules[category_code]
        if rule.max_amount_per_claim is not None and total > rule.max_amount_per_claim:
            errors.append(
                ExpenseFieldError(
                    "lines",
                    f"{rule.category_name} cannot exceed "
                    f"{rule.max_amount_per_claim:.2f} per claim.",
                )
            )
    if errors or expense_date is None:
        raise WorkOrderExpenseFormError(
            message="Correct the highlighted expense details.",
            form=form,
            errors=tuple(errors),
        )
    assert selected_approver is not None
    return PreparedWorkOrderExpense(
        request_id=request_id,
        purpose=purpose,
        expense_date=expense_date,
        currency=currency,
        notes=notes,
        lines=tuple(prepared_lines),
        category_rules=category_rules,
        selected_approver=selected_approver,
        payment_destination_mode=destination_mode,
        bank_code=(
            bank_code
            if destination_mode is ExpenseDestinationMode.EXPENSE_OVERRIDE
            else None
        ),
        account_number=(
            account_number
            if destination_mode is ExpenseDestinationMode.EXPENSE_OVERRIDE
            else None
        ),
        beneficiary_name=(
            beneficiary_name
            if destination_mode is ExpenseDestinationMode.EXPENSE_OVERRIDE
            else None
        ),
    )


def prepare_form_redisplay(
    form: WorkOrderExpenseFormInput,
    errors: tuple[ExpenseFieldError, ...],
) -> tuple[WorkOrderExpenseFormInput, tuple[ExpenseFieldError, ...]]:
    """Preserve entered text while truthfully clearing browser-only file values."""

    redisplay_errors = list(errors)
    lines: list[ExpenseLineFormInput] = []
    for line in form.lines:
        if line.receipt_upload is not None:
            redisplay_errors.append(
                ExpenseFieldError(
                    f"line.{line.key}.receipt",
                    "Re-select the receipt file before submitting.",
                )
            )
            line = replace(line, receipt_upload=None)
        lines.append(line)
    return replace(
        form,
        lines=tuple(lines),
        account_number="",
    ), tuple(redisplay_errors)


def _claim_views(
    db: Session,
    *,
    work_order: WorkOrder,
    user: SystemUser,
) -> tuple[WorkOrderExpenseClaimView, ...]:
    person_ids = {user.id}
    if user.person_party_id is not None:
        person_ids.add(user.person_party_id)
    rows = (
        db.query(FieldExpenseRequest)
        .options(selectinload(FieldExpenseRequest.items))
        .filter(
            FieldExpenseRequest.work_order_mirror_id == work_order.id,
            FieldExpenseRequest.is_active.is_(True),
            or_(
                FieldExpenseRequest.requested_by_system_user_id == user.id,
                FieldExpenseRequest.requested_by_person_id.in_(person_ids),
            ),
        )
        .order_by(FieldExpenseRequest.created_at.desc())
        .all()
    )
    event_by_request: dict[UUID, FieldErpSyncEvent] = {}
    if rows:
        events = (
            db.query(FieldErpSyncEvent)
            .filter(
                FieldErpSyncEvent.flow == FieldErpSyncFlow.expense_claim.value,
                FieldErpSyncEvent.entity_id.in_([row.id for row in rows]),
            )
            .order_by(FieldErpSyncEvent.updated_at.desc())
            .all()
        )
        for event in events:
            event_by_request.setdefault(event.entity_id, event)
    return tuple(
        WorkOrderExpenseClaimView(
            id=row.id,
            purpose=row.purpose,
            expense_date=row.expense_date,
            currency=row.currency,
            total_amount=row.total_amount,
            status=field_expense_status_presentation(row.status),
            delivery_state=_delivery_state(row, event_by_request.get(row.id)),
            delivery_label=_delivery_label(row, event_by_request.get(row.id)),
            delivery_detail=_delivery_detail(event_by_request.get(row.id)),
            erp_claim_number=row.expense_claim_number,
            erp_claim_status=row.expense_claim_status,
            rejection_reason=row.rejection_reason,
        )
        for row in rows
    )


def _delivery_state(
    request: FieldExpenseRequest, event: FieldErpSyncEvent | None
) -> ExpenseDeliveryState:
    if request.expense_claim_reference or (
        event and event.status == FieldErpSyncStatus.accepted.value
    ):
        return ExpenseDeliveryState.ACCEPTED
    if event and event.status in {
        FieldErpSyncStatus.rejected.value,
        FieldErpSyncStatus.dead.value,
    }:
        return ExpenseDeliveryState.FAILED
    if event and event.status in {
        FieldErpSyncStatus.pending.value,
        FieldErpSyncStatus.sent.value,
    }:
        return ExpenseDeliveryState.PENDING
    if request.status in {"draft", "submitted"}:
        return ExpenseDeliveryState.AWAITING_APPROVAL
    if request.status in {"canceled", "rejected"}:
        return ExpenseDeliveryState.NOT_APPLICABLE
    return ExpenseDeliveryState.UNAVAILABLE


def _delivery_label(
    request: FieldExpenseRequest, event: FieldErpSyncEvent | None
) -> str:
    state = _delivery_state(request, event)
    if event and event.status == FieldErpSyncStatus.sent.value:
        return "Delivered; awaiting ERP acceptance"
    return {
        ExpenseDeliveryState.AWAITING_APPROVAL: "Awaiting manager approval",
        ExpenseDeliveryState.ACCEPTED: "Accepted by ERP",
        ExpenseDeliveryState.FAILED: "ERP synchronization failed",
        ExpenseDeliveryState.PENDING: "Waiting for ERP delivery",
        ExpenseDeliveryState.NOT_APPLICABLE: "Not sent to ERP",
        ExpenseDeliveryState.UNAVAILABLE: "ERP delivery evidence is unavailable",
    }[state]


def _delivery_detail(event: FieldErpSyncEvent | None) -> str | None:
    if (
        event is None
        or event.status
        not in {FieldErpSyncStatus.rejected.value, FieldErpSyncStatus.dead.value}
        or not isinstance(event.erp_response, dict)
    ):
        return None
    diagnostic = parse_diagnostic_evidence(
        event.erp_response.get(DELIVERY_DIAGNOSTIC_KEY)
    )
    return safe_diagnostic_summary(diagnostic) if diagnostic is not None else None


def _empty_line() -> ExpenseLineFormInput:
    return ExpenseLineFormInput(
        key=uuid4().hex,
        category_code="",
        description="",
        amount="",
        expense_date="",
        vendor_name="",
        receipt_url="",
        notes="",
    )


def _amount_value(
    value: str, field: str, errors: list[ExpenseFieldError]
) -> Decimal | None:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        errors.append(ExpenseFieldError(field, "Enter a valid amount."))
        return None
    if amount <= 0:
        errors.append(ExpenseFieldError(field, "Amount must be greater than zero."))
        return None
    return amount.quantize(Decimal("0.01"))


def _date_value(
    value: str,
    field: str,
    errors: list[ExpenseFieldError],
    *,
    required: bool,
) -> date | None:
    raw = value.strip()
    if not raw and not required:
        return None
    if not raw:
        errors.append(ExpenseFieldError(field, "Expense date is required."))
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        errors.append(ExpenseFieldError(field, "Enter a valid date."))
        return None


def _valid_receipt_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
