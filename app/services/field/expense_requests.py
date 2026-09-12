"""Native field expense requests."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.field_attachment import FieldAttachment
from app.models.field_expense import (
    FIELD_EXPENSE_STATUSES,
    FieldExpenseRequest,
    FieldExpenseRequestItem,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services.backoffice import (
    BackofficeDeliveryView,
    BackofficeEnqueueResult,
    BackofficeEnqueueStatus,
    expense_payment_projection,
    get_expense_claim_deliveries,
    get_expense_decision_delivery,
    get_expense_payment_deliveries,
)
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.field.jobs import _scoped_query
from app.services.field.source import (
    mark_sub_authoritative as _mark_source_authoritative,
)
from app.services.integrations import installations
from app.services.integrations.diagnostics import safe_diagnostic_summary
from app.services.integrations.erp_capability import (
    ErpCapabilityError,
    ErpCapabilityTransientError,
    ExpenseApproverOption,
    ExpenseBankOption,
    ExpenseDestinationMode,
    ExpenseProfileDestination,
    InspectExpenseDestination,
    VerifyExpenseDestination,
    capability_client,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.staff_provisioning import (
    StaffDisplayIdentityQuery,
    resolve_staff_display_identities,
)


class ExpenseRequestAccessMode(StrEnum):
    FIELD_ASSIGNMENT = "field_assignment"
    STAFF_WORK_ORDER = "staff_work_order"


@dataclass(frozen=True, slots=True)
class ExpenseWorkOrderIdentity:
    """Typed public identity resolved by the expense owner."""

    public_id: str


@dataclass(frozen=True, slots=True)
class ExpenseWorkOrderScopeGrant:
    scope_type: str
    scope_id: str


@dataclass(frozen=True, slots=True)
class StaffWorkOrderAccess:
    """Authenticated staff grant evidence, never a browser database UUID."""

    global_access: bool
    scopes: tuple[ExpenseWorkOrderScopeGrant, ...] = ()


@dataclass(frozen=True, slots=True)
class ExpenseWorkOrderEligibility:
    allowed: bool
    reason: str | None


def evaluate_expense_work_order_eligibility(
    db: Session,
    *,
    work_order: WorkOrder,
) -> ExpenseWorkOrderEligibility:
    """Return whether the work order has authoritative assignment evidence."""
    if work_order.assigned_to_crm_person_id:
        return ExpenseWorkOrderEligibility(allowed=True, reason=None)
    assigned_queue_entry = (
        db.query(WorkOrderAssignmentQueue.id)
        .filter(WorkOrderAssignmentQueue.work_order_mirror_id == work_order.id)
        .filter(WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned)
        .filter(WorkOrderAssignmentQueue.assigned_technician_id.isnot(None))
        .first()
    )
    if assigned_queue_entry is not None:
        return ExpenseWorkOrderEligibility(allowed=True, reason=None)
    return ExpenseWorkOrderEligibility(
        allowed=False,
        reason="Assign a technician first.",
    )


@dataclass(frozen=True, slots=True)
class ExpenseCategoryRule:
    category_code: str
    category_name: str
    requires_receipt: bool
    max_amount_per_claim: Decimal | None


@dataclass(frozen=True, slots=True)
class ExpenseReceiptUploadInput:
    file_name: str
    mime_type: str | None
    content: bytes
    client_ref: UUID


@dataclass(frozen=True, slots=True)
class ExpenseRequestLineInput:
    category_code: str
    category_name: str | None
    description: str
    amount: Decimal
    expense_date: date | None
    vendor_name: str | None
    receipt_url: str | None
    receipt_attachment_id: UUID | None
    notes: str | None
    receipt_upload: ExpenseReceiptUploadInput | None = None


@dataclass(frozen=True, slots=True)
class SelectedExpenseApprover:
    erp_employee_id: UUID
    system_user_id: UUID
    display_name: str
    email: str


@dataclass(frozen=True, slots=True)
class VerifiedExpenseDestinationInput:
    mode: Literal["erp_profile", "expense_override"]
    destination_token: str
    bank_code: str
    bank_name: str
    masked_account_number: str
    verified_beneficiary_name: str
    verified_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GetFieldExpenseFormContext:
    requester_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class FieldExpenseFormContext:
    approvers: tuple[SelectedExpenseApprover, ...]
    banks: tuple[ExpenseBankOption, ...]
    profile_destination: ExpenseProfileDestination


@dataclass(frozen=True, slots=True)
class VerifyFieldExpenseDestination:
    requester_system_user_id: UUID
    source_claim_id: UUID
    mode: ExpenseDestinationMode
    bank_code: str | None
    account_number: str | None
    beneficiary_name: str | None


@dataclass(frozen=True, slots=True)
class ResolveFieldExpenseSubmissionContext:
    requester_system_user_id: UUID
    selected_approver_erp_id: UUID
    source_claim_id: UUID
    destination_token: str


@dataclass(frozen=True, slots=True)
class ResolvedFieldExpenseSubmissionContext:
    selected_approver: SelectedExpenseApprover
    payment_destination: VerifiedExpenseDestinationInput


@dataclass(frozen=True, slots=True)
class SubmitFieldExpenseRequest:
    context: CommandContext
    requester_person_id: UUID | None
    work_order: ExpenseWorkOrderIdentity
    request_id: UUID
    purpose: str
    expense_date: date | None
    currency: str
    notes: str | None
    items: tuple[ExpenseRequestLineInput, ...]
    access_mode: ExpenseRequestAccessMode = ExpenseRequestAccessMode.FIELD_ASSIGNMENT
    staff_access: StaffWorkOrderAccess | None = None
    selected_approver: SelectedExpenseApprover | None = None
    payment_destination: VerifiedExpenseDestinationInput | None = None


@dataclass(frozen=True, slots=True)
class ApproveFieldExpenseRequest:
    context: CommandContext
    expense_request_id: UUID
    reviewer_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class RejectFieldExpenseRequest:
    context: CommandContext
    expense_request_id: UUID
    reviewer_system_user_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class CancelFieldExpenseRequest:
    context: CommandContext
    expense_request_id: UUID
    requester_person_id: UUID | None


@dataclass(frozen=True, slots=True)
class RecoverExpenseDelivery:
    context: CommandContext
    dead_event_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class InitiateFieldExpensePayment:
    context: CommandContext
    expense_request_id: UUID
    manager_system_user_id: UUID


class ExpenseErpSyncStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEAD = "dead"
    NOT_CONFIGURED = "not_configured"
    NOT_QUEUED = "not_queued"


class ExpenseRequestStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class ExpenseRequestApprovalOutcome:
    id: UUID
    status: Literal["approved"]
    approved_at: datetime
    erp_sync_status: ExpenseErpSyncStatus
    erp_sync_event_id: UUID | None
    erp_sync_error: str | None


@dataclass(frozen=True, slots=True)
class ExpenseRequestRejectionOutcome:
    id: UUID
    status: Literal["rejected"]
    rejected_at: datetime
    rejection_reason: str
    erp_sync_event_id: UUID | None


@dataclass(frozen=True, slots=True)
class ExpenseRequestCancellationOutcome:
    id: UUID
    status: Literal["canceled"]


@dataclass(frozen=True, slots=True)
class ExpenseDeliveryRecoveryOutcome:
    original_event_id: UUID
    replacement_event_id: UUID
    replacement_idempotency_key: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ExpensePaymentInitiationOutcome:
    id: UUID
    status: Literal["approved"]
    payment_status: Literal["queued"]
    payment_command_id: UUID
    erp_sync_event_id: UUID


@dataclass(frozen=True, slots=True)
class ExpenseRequestItemOutcome:
    id: UUID
    category_code: str
    category_name: str | None
    description: str
    amount: Decimal
    expense_date: date | None
    vendor_name: str | None
    receipt_url: str | None
    receipt_attachment_id: UUID | None
    notes: str | None


@dataclass(frozen=True, slots=True)
class ExpenseRequestSubmissionOutcome:
    id: UUID
    work_order_id: str
    requested_by_person_id: UUID
    requested_by_system_user_id: UUID | None
    status: str
    purpose: str
    expense_date: date | None
    currency: str
    notes: str | None
    client_ref: UUID
    total_amount: Decimal
    submitted_at: datetime
    created_at: datetime
    updated_at: datetime
    items: tuple[ExpenseRequestItemOutcome, ...]


@dataclass(frozen=True, slots=True)
class ExpenseRequestView:
    id: UUID
    work_order_id: str
    crm_expense_request_id: str | None
    requested_by_person_id: UUID
    requested_by_system_user_id: UUID | None
    requested_by_name: str | None
    selected_approver_erp_id: UUID | None
    selected_approver_name: str | None
    selected_approver_email: str | None
    payment_destination_mode: str | None
    recipient_bank_name: str | None
    masked_account_number: str | None
    verified_beneficiary_name: str | None
    status: ExpenseRequestStatus
    purpose: str
    expense_date: date | None
    currency: str
    notes: str | None
    rejection_reason: str | None
    expense_system: str | None
    expense_claim_reference: str | None
    expense_claim_number: str | None
    expense_claim_status: str | None
    erp_sync_status: ExpenseErpSyncStatus | None
    erp_sync_error: str | None
    payment_status: str | None
    payment_intent_id: str | None
    payment_error: str | None
    client_ref: UUID | None
    total_amount: Decimal
    submitted_at: datetime | None
    approved_at: datetime | None
    rejected_at: datetime | None
    paid_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: tuple[ExpenseRequestItemOutcome, ...]


@dataclass(frozen=True, slots=True)
class RequesterExpenseHistoryQuery:
    system_user_id: UUID
    work_order_id: str | None = None
    status: ExpenseRequestStatus | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class RequesterExpenseDetailQuery:
    system_user_id: UUID
    request_id: UUID


@dataclass(frozen=True, slots=True)
class RequesterExpenseHistoryPage:
    items: tuple[ExpenseRequestView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ManagerExpenseReviewQuery:
    approver_system_user_id: UUID | None
    status: ExpenseRequestStatus | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True, slots=True)
class ManagerExpenseReviewPage:
    items: tuple[ExpenseRequestView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class _ExpenseRequesterIdentity:
    system_user_id: UUID
    person_ids: tuple[UUID, ...]
    technician_profile_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ListFieldExpenseVendors:
    search: str | None = None
    limit: int = 25
    offset: int = 0


@dataclass(frozen=True, slots=True)
class FieldExpenseVendorOption:
    id: UUID
    label: str


class FieldExpenseRequestError(DomainError):
    pass


def _active_expense_requester(db: Session, system_user_id: UUID) -> SystemUser:
    requester = db.get(SystemUser, system_user_id)
    if requester is None or not requester.is_active or not requester.email.strip():
        raise FieldExpenseRequestError(
            code="operations.expense_requests.invalid_request",
            message="An active staff user with an email address is required.",
        )
    return requester


def _local_expense_approvers(
    db: Session,
    *,
    erp_approvers: tuple[ExpenseApproverOption, ...],
) -> tuple[SelectedExpenseApprover, ...]:
    emails = {item.email.strip().lower() for item in erp_approvers}
    if not emails:
        return ()
    local_users = {
        user.email.strip().lower(): user
        for user in db.scalars(
            select(SystemUser).where(
                SystemUser.is_active.is_(True),
                func.lower(SystemUser.email).in_(emails),
            )
        ).all()
    }
    return tuple(
        SelectedExpenseApprover(
            erp_employee_id=item.employee_id,
            system_user_id=local_users[item.email.strip().lower()].id,
            display_name=item.display_name,
            email=item.email,
        )
        for item in erp_approvers
        if item.email.strip().lower() in local_users
    )


def get_field_expense_form_context(
    db: Session,
    query: GetFieldExpenseFormContext,
) -> FieldExpenseFormContext:
    requester = _active_expense_requester(db, query.requester_system_user_id)
    client = capability_client(db)
    try:
        erp_approvers = client.get_expense_approvers(requested_by_email=requester.email)
        banks = client.get_expense_banks()
        profile_destination = client.get_expense_profile_destination(
            requested_by_email=requester.email
        )
    except (ErpCapabilityError, installations.InstallationError) as exc:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.form_context_unavailable",
            message="Expense approvers and payment details are unavailable from ERP.",
        ) from exc
    return FieldExpenseFormContext(
        approvers=_local_expense_approvers(db, erp_approvers=erp_approvers),
        banks=banks,
        profile_destination=profile_destination,
    )


def verify_field_expense_destination(
    db: Session,
    command: VerifyFieldExpenseDestination,
) -> VerifiedExpenseDestinationInput:
    requester = _active_expense_requester(db, command.requester_system_user_id)
    try:
        verified = capability_client(db).verify_expense_destination(
            VerifyExpenseDestination(
                requested_by_email=requester.email,
                source_claim_id=command.source_claim_id,
                mode=command.mode,
                bank_code=command.bank_code,
                account_number=command.account_number,
                beneficiary_name=command.beneficiary_name,
            )
        )
    except (ErpCapabilityTransientError, installations.InstallationError) as exc:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.destination_unavailable",
            message="Bank account verification is temporarily unavailable.",
        ) from exc
    except ErpCapabilityError as exc:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.destination_invalid",
            message="ERP could not verify the payment details.",
        ) from exc
    return VerifiedExpenseDestinationInput(
        mode=verified.mode.value,
        destination_token=verified.destination_token,
        bank_code=verified.bank_code,
        bank_name=verified.bank_name,
        masked_account_number=verified.masked_account_number,
        verified_beneficiary_name=verified.verified_beneficiary_name,
        verified_at=verified.verified_at,
        expires_at=verified.expires_at,
    )


def resolve_field_expense_submission_context(
    db: Session,
    query: ResolveFieldExpenseSubmissionContext,
) -> ResolvedFieldExpenseSubmissionContext:
    requester = _active_expense_requester(db, query.requester_system_user_id)
    client = capability_client(db)
    try:
        erp_approvers = client.get_expense_approvers(requested_by_email=requester.email)
    except (ErpCapabilityError, installations.InstallationError) as exc:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.form_context_unavailable",
            message="Expense approvers are unavailable from ERP.",
        ) from exc
    selected = next(
        (
            approver
            for approver in _local_expense_approvers(db, erp_approvers=erp_approvers)
            if approver.erp_employee_id == query.selected_approver_erp_id
        ),
        None,
    )
    if selected is None:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.approver_invalid",
            message="Select an eligible expense approver with an active Sub account.",
        )
    try:
        verified = client.inspect_expense_destination(
            InspectExpenseDestination(
                requested_by_email=requester.email,
                source_claim_id=query.source_claim_id,
                destination_token=query.destination_token,
            )
        )
    except (ErpCapabilityTransientError, installations.InstallationError) as exc:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.destination_unavailable",
            message="Payment details cannot be checked with ERP right now.",
        ) from exc
    except ErpCapabilityError as exc:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.destination_invalid",
            message="Payment details expired or do not belong to this expense.",
        ) from exc
    return ResolvedFieldExpenseSubmissionContext(
        selected_approver=selected,
        payment_destination=VerifiedExpenseDestinationInput(
            mode=verified.mode.value,
            destination_token=verified.destination_token,
            bank_code=verified.bank_code,
            bank_name=verified.bank_name,
            masked_account_number=verified.masked_account_number,
            verified_beneficiary_name=verified.verified_beneficiary_name,
            verified_at=verified.verified_at,
            expires_at=verified.expires_at,
        ),
    )


_EXPENSE_SUBMIT_COMMAND = OwnerCommandDefinition(
    owner="operations.expense_requests",
    concern="field expense request submission",
    name="submit_field_expense_request",
)

_EXPENSE_APPROVAL_COMMAND = OwnerCommandDefinition(
    owner="operations.expense_requests",
    concern="field expense approval and ERP delivery staging",
    name="approve_field_expense_request",
)

_EXPENSE_REJECTION_COMMAND = OwnerCommandDefinition(
    owner="operations.expense_requests",
    concern="field expense approval and ERP delivery staging",
    name="reject_field_expense_request",
)

_EXPENSE_PAYMENT_COMMAND = OwnerCommandDefinition(
    owner="operations.expense_requests",
    concern="field expense payment initiation and ERP delivery staging",
    name="initiate_field_expense_payment",
)

_CANCEL_EXPENSE_REQUEST = OwnerCommandDefinition(
    owner="operations.expense_requests",
    concern="field expense request cancellation",
    name="cancel_field_expense_request",
)

_RECOVER_EXPENSE_DELIVERY = OwnerCommandDefinition(
    owner="operations.expense_requests",
    concern="dead expense delivery recovery",
    name="recover_dead_expense_delivery",
)


def _expense_fingerprint(command: SubmitFieldExpenseRequest) -> str:
    payload = {
        "work_order_public_id": command.work_order.public_id,
        "purpose": command.purpose.strip(),
        "expense_date": str(command.expense_date) if command.expense_date else None,
        "currency": command.currency.strip().upper(),
        "notes": (command.notes or "").strip() or None,
        "selected_approver": (
            {
                "erp_employee_id": str(command.selected_approver.erp_employee_id),
                "system_user_id": str(command.selected_approver.system_user_id),
                "email": command.selected_approver.email.strip().lower(),
            }
            if command.selected_approver
            else None
        ),
        "payment_destination": (
            {
                "mode": command.payment_destination.mode,
                "destination_token_sha256": hashlib.sha256(
                    command.payment_destination.destination_token.encode()
                ).hexdigest(),
                "bank_code": command.payment_destination.bank_code,
                "account_last4": command.payment_destination.masked_account_number[-4:],
                "beneficiary": command.payment_destination.verified_beneficiary_name,
                "expires_at": command.payment_destination.expires_at.isoformat(),
            }
            if command.payment_destination
            else None
        ),
        "items": [
            {
                "category_code": item.category_code.strip(),
                "category_name": (item.category_name or "").strip() or None,
                "description": item.description.strip(),
                "amount": str(item.amount),
                "expense_date": str(item.expense_date) if item.expense_date else None,
                "vendor_name": (item.vendor_name or "").strip() or None,
                "receipt_url": (item.receipt_url or "").strip() or None,
                "receipt_attachment_id": str(item.receipt_attachment_id)
                if item.receipt_attachment_id
                else None,
                "notes": (item.notes or "").strip() or None,
                "receipt_upload": (
                    {
                        "file_name": item.receipt_upload.file_name,
                        "mime_type": item.receipt_upload.mime_type,
                        "sha256": hashlib.sha256(
                            item.receipt_upload.content
                        ).hexdigest(),
                        "client_ref": str(item.receipt_upload.client_ref),
                    }
                    if item.receipt_upload
                    else None
                ),
            }
            for item in command.items
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def submit_field_expense_request_command(
    db: Session, command: SubmitFieldExpenseRequest
) -> ExpenseRequestSubmissionOutcome:
    fingerprint = _expense_fingerprint(command)

    def operation() -> ExpenseRequestSubmissionOutcome:
        system_user_id = _system_user_id_for_actor(db, command.context)
        if system_user_id is None:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.requester_not_found",
                message="An authenticated staff user is required.",
            )
        system_user = db.get(SystemUser, system_user_id)
        if system_user is None or not system_user.is_active:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.requester_not_found",
                message="The requesting staff user is unavailable.",
            )
        existing = (
            db.query(FieldExpenseRequest)
            .options(selectinload(FieldExpenseRequest.items))
            .filter(FieldExpenseRequest.client_ref == command.request_id)
            .one_or_none()
        )
        if existing is not None:
            requester_ids = {system_user.id}
            if system_user.person_party_id is not None:
                requester_ids.add(system_user.person_party_id)
            if (
                existing.requested_by_system_user_id not in {None, system_user.id}
                or existing.requested_by_person_id not in requester_ids
                or existing.work_order_mirror.public_id != command.work_order.public_id
            ):
                raise FieldExpenseRequestError(
                    code="operations.expense_requests.idempotency_conflict",
                    message="Request identity belongs to another expense claim.",
                )
            metadata = (
                existing.metadata_ if isinstance(existing.metadata_, dict) else {}
            )
            if metadata.get("command_fingerprint") != fingerprint:
                raise FieldExpenseRequestError(
                    code="operations.expense_requests.idempotency_conflict",
                    message="Request identity was already used with different expense details.",
                )
            return _submission_outcome(existing)
        approver = command.selected_approver
        destination = command.payment_destination
        if (approver is None) != (destination is None):
            raise FieldExpenseRequestError(
                code="operations.expense_requests.form_context_required",
                message="Approver and verified payment details must be supplied together.",
            )
        now = datetime.now(UTC)
        if approver is not None and destination is not None:
            approver_user = db.get(SystemUser, approver.system_user_id)
            if (
                approver_user is None
                or not approver_user.is_active
                or approver_user.email.strip().lower() != approver.email.strip().lower()
            ):
                raise FieldExpenseRequestError(
                    code="operations.expense_requests.approver_invalid",
                    message="The selected expense approver is no longer available.",
                )
            if destination.expires_at <= now:
                raise FieldExpenseRequestError(
                    code="operations.expense_requests.destination_expired",
                    message="Payment details expired. Verify them again.",
                )
            if len(destination.masked_account_number) < 4:
                raise FieldExpenseRequestError(
                    code="operations.expense_requests.destination_invalid",
                    message="Verified payment details are invalid.",
                )
        profile = _requesting_technician(
            db,
            requester_person_id=command.requester_person_id,
            system_user_id=system_user_id,
        )
        if command.access_mode == ExpenseRequestAccessMode.FIELD_ASSIGNMENT:
            if profile is None:
                raise FieldExpenseRequestError(
                    code="operations.expense_requests.requester_not_found",
                    message="Technician profile not found.",
                )
            row = (
                _scoped_query(db, profile)
                .filter(WorkOrder.public_id == command.work_order.public_id)
                .with_for_update()
                .one_or_none()
            )
        else:
            row = db.execute(
                select(WorkOrder)
                .where(
                    WorkOrder.public_id == command.work_order.public_id,
                    WorkOrder.is_active.is_(True),
                )
                .with_for_update()
            ).scalar_one_or_none()
        if row is None:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.work_order_not_found",
                message="Job not found.",
            )
        if command.access_mode == ExpenseRequestAccessMode.STAFF_WORK_ORDER:
            _require_staff_work_order_access(
                db, work_order=row, access=command.staff_access
            )
        eligibility = evaluate_expense_work_order_eligibility(db, work_order=row)
        if not eligibility.allowed:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.work_order_unassigned",
                message=eligibility.reason or "Assign a technician first.",
            )
        raw_items = [
            {
                "category_code": item.category_code,
                "category_name": item.category_name,
                "description": item.description,
                "amount": item.amount,
                "expense_date": item.expense_date,
                "vendor_name": item.vendor_name,
                "receipt_url": item.receipt_url,
                "receipt_attachment_id": item.receipt_attachment_id,
                "notes": item.notes,
                "receipt_upload": item.receipt_upload,
            }
            for item in command.items
        ]
        if not raw_items:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.invalid_request",
                message="At least one item is required.",
            )
        try:
            category_rules = resolve_authoritative_expense_category_rules(db)
            planned_items = _validate_items(
                db,
                row,
                raw_items,
                category_rules=category_rules,
                attachment_owner_ids={
                    system_user.id,
                    *(
                        (system_user.person_party_id,)
                        if system_user.person_party_id is not None
                        else ()
                    ),
                    *((profile.id, profile.person_id) if profile is not None else ()),
                },
            )
            currency = _currency(command.currency)
        except HTTPException as exc:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.invalid_request",
                message=str(exc.detail),
            ) from exc
        purpose = command.purpose.strip()
        if not purpose:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.invalid_request",
                message="purpose is required",
            )
        from app.services.field.attachments import (
            StageExpenseReceiptAttachment,
            stage_expense_receipt_attachment,
        )

        for item in planned_items:
            upload = item.pop("_receipt_upload", None)
            if upload is None:
                continue
            receipt = stage_expense_receipt_attachment(
                db,
                StageExpenseReceiptAttachment(
                    work_order_id=row.id,
                    work_order_public_id=row.public_id,
                    uploaded_by_person_id=system_user.person_party_id or system_user.id,
                    uploaded_by_system_user_id=system_user.id,
                    uploaded_by_technician_id=profile.id if profile else None,
                    file_name=upload.file_name,
                    mime_type=upload.mime_type,
                    content=upload.content,
                    client_ref=upload.client_ref,
                ),
            )
            item["receipt_attachment_id"] = receipt.id
        request = FieldExpenseRequest(
            work_order_mirror_id=row.id,
            requested_by_technician_id=profile.id if profile else None,
            requested_by_person_id=system_user.person_party_id or system_user.id,
            requested_by_system_user_id=system_user.id,
            selected_approver_erp_id=(approver.erp_employee_id if approver else None),
            selected_approver_system_user_id=(
                approver.system_user_id if approver else None
            ),
            selected_approver_name=(
                approver.display_name.strip() if approver else None
            ),
            selected_approver_email=(
                approver.email.strip().lower() if approver else None
            ),
            payment_destination_mode=(destination.mode if destination else None),
            payment_destination_token=(
                destination.destination_token if destination else None
            ),
            recipient_bank_code=(destination.bank_code if destination else None),
            recipient_bank_name=(destination.bank_name if destination else None),
            recipient_account_last4=(
                destination.masked_account_number[-4:] if destination else None
            ),
            verified_beneficiary_name=(
                destination.verified_beneficiary_name if destination else None
            ),
            destination_verified_at=(destination.verified_at if destination else None),
            destination_token_expires_at=(
                destination.expires_at if destination else None
            ),
            status="submitted",
            purpose=purpose,
            expense_date=command.expense_date,
            currency=currency,
            notes=(command.notes or "").strip() or None,
            client_ref=command.request_id,
            submitted_at=now,
            metadata_={"command_fingerprint": fingerprint},
        )
        db.add(request)
        db.flush()
        for item in planned_items:
            request.items.append(FieldExpenseRequestItem(**item))
        validate_expense_receipt_delivery(db, request, category_rules=category_rules)
        _mark_sub_authoritative(row)
        db.flush()
        # Submission is a local review transition only. Manager approval is
        # the sole ERP release point.
        return _submission_outcome(request)

    return execute_owner_command(
        db,
        definition=_EXPENSE_SUBMIT_COMMAND,
        context=command.context,
        operation=operation,
    )


def approve_field_expense_request_command(
    db: Session, *, command: ApproveFieldExpenseRequest
) -> ExpenseRequestApprovalOutcome:
    """Approve one expense and durably stage its ERP delivery intent."""

    def operation() -> ExpenseRequestApprovalOutcome:
        request = _locked_expense_request(db, command.expense_request_id)
        if request.status == "approved":
            return _approval_outcome(db, request)
        if request.status != "submitted":
            raise FieldExpenseRequestError(
                code="operations.expense_requests.invalid_transition",
                message="Only submitted expense requests can be approved.",
            )
        if (
            request.selected_approver_system_user_id is not None
            and request.selected_approver_system_user_id
            != command.reviewer_system_user_id
        ):
            raise FieldExpenseRequestError(
                code="operations.expense_requests.approver_mismatch",
                message="Only the selected expense approver can approve this request.",
            )

        category_rules = resolve_authoritative_expense_category_rules(db)
        validate_expense_receipt_delivery(
            db,
            request,
            category_rules=category_rules,
        )

        now = datetime.now(UTC)
        request.status = "approved"
        request.approved_at = now
        request.payment_destination_locked_at = now
        request.rejection_reason = None
        _note_approval_command(request, command, occurred_at=now)
        _mark_sub_authoritative(request.work_order_mirror)

        try:
            result = _enqueue_decision_backoffice(
                db,
                request,
                action="approve",
                decision_id=command.context.command_id,
                reviewer_system_user_id=command.reviewer_system_user_id,
                decided_at=now,
            )
        except Exception as exc:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.erp_staging_failed",
                message=(
                    "The expense was not approved because its ERP delivery "
                    "could not be queued. Please retry."
                ),
                details={"error_type": type(exc).__name__},
            ) from exc
        if result.status is not BackofficeEnqueueStatus.ENQUEUED:
            code = (
                "operations.expense_requests.erp_delivery_not_configured"
                if result.status is BackofficeEnqueueStatus.NOT_OWNED
                else "operations.expense_requests.erp_staging_failed"
            )
            raise FieldExpenseRequestError(
                code=code,
                message=(
                    "The expense was not approved because its ERP delivery "
                    "could not be queued. Please retry."
                ),
            )
        db.flush()
        return _approval_outcome(db, request)

    return execute_owner_command(
        db,
        definition=_EXPENSE_APPROVAL_COMMAND,
        context=command.context,
        operation=operation,
    )


def reject_field_expense_request_command(
    db: Session, *, command: RejectFieldExpenseRequest
) -> ExpenseRequestRejectionOutcome:
    """Reject locally; an unapproved expense has no ERP claim to update."""

    def operation() -> ExpenseRequestRejectionOutcome:
        request = _locked_expense_request(db, command.expense_request_id)
        if request.status == "rejected":
            if request.rejected_at is None:
                raise FieldExpenseRequestError(
                    code="operations.expense_requests.invalid_request",
                    message="Rejected expense evidence is incomplete.",
                )
            return ExpenseRequestRejectionOutcome(
                id=request.id,
                status="rejected",
                rejected_at=request.rejected_at,
                rejection_reason=request.rejection_reason or "Rejected",
                erp_sync_event_id=None,
            )
        if request.status != "submitted":
            raise FieldExpenseRequestError(
                code="operations.expense_requests.invalid_transition",
                message="Only submitted expense requests can be rejected.",
            )
        if (
            request.selected_approver_system_user_id is not None
            and request.selected_approver_system_user_id
            != command.reviewer_system_user_id
        ):
            raise FieldExpenseRequestError(
                code="operations.expense_requests.approver_mismatch",
                message="Only the selected expense approver can reject this request.",
            )
        reason = command.reason.strip()
        if not reason:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.invalid_request",
                message="A rejection reason is required.",
            )
        now = datetime.now(UTC)
        request.status = "rejected"
        request.rejected_at = now
        request.rejection_reason = reason[:500]
        _note_manager_command(
            request,
            event="rejected",
            context=command.context,
            reviewer_system_user_id=command.reviewer_system_user_id,
            occurred_at=now,
        )
        _mark_sub_authoritative(request.work_order_mirror)
        db.flush()
        return ExpenseRequestRejectionOutcome(
            id=request.id,
            status="rejected",
            rejected_at=now,
            rejection_reason=request.rejection_reason,
            erp_sync_event_id=None,
        )

    return execute_owner_command(
        db,
        definition=_EXPENSE_REJECTION_COMMAND,
        context=command.context,
        operation=operation,
    )


def cancel_field_expense_request_command(
    db: Session, *, command: CancelFieldExpenseRequest
) -> ExpenseRequestCancellationOutcome:
    """Cancel a requester-owned unapproved expense without ERP delivery."""

    def operation() -> ExpenseRequestCancellationOutcome:
        system_user_id = _system_user_id_for_actor(db, command.context)
        if system_user_id is None:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.requester_not_found",
                message="An authenticated staff user is required.",
            )
        profile = _requesting_technician(
            db,
            requester_person_id=command.requester_person_id,
            system_user_id=system_user_id,
        )
        request = _locked_expense_request(db, command.expense_request_id)
        allowed_ids = {system_user_id}
        user = db.get(SystemUser, system_user_id)
        if user is not None and user.person_party_id is not None:
            allowed_ids.add(user.person_party_id)
        if profile is not None:
            allowed_ids.update({profile.id, profile.person_id})
        if not {
            request.requested_by_system_user_id,
            request.requested_by_person_id,
            request.requested_by_technician_id,
        }.intersection(allowed_ids):
            raise FieldExpenseRequestError(
                code="operations.expense_requests.request_not_found",
                message="Expense request was not found.",
            )
        if request.status == "canceled":
            return ExpenseRequestCancellationOutcome(id=request.id, status="canceled")
        if request.status not in {"draft", "submitted"}:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.invalid_transition",
                message="Only draft or submitted expense requests can be canceled.",
            )
        request.status = "canceled"
        _mark_sub_authoritative(request.work_order_mirror)
        db.flush()
        return ExpenseRequestCancellationOutcome(id=request.id, status="canceled")

    return execute_owner_command(
        db,
        definition=_CANCEL_EXPENSE_REQUEST,
        context=command.context,
        operation=operation,
    )


def recover_expense_delivery(
    db: Session,
    *,
    command: RecoverExpenseDelivery,
) -> ExpenseDeliveryRecoveryOutcome:
    """Append one verified replacement for a dead expense delivery."""

    from app.services.backoffice import stage_expense_delivery_recovery
    from app.services.field.expense_recovery import (
        RECOVERY_CONTRACT_VERSION,
        ExpenseDeliveryRecoveryError,
        _preview,
    )

    def operation() -> ExpenseDeliveryRecoveryOutcome:
        preview = _preview(db, command.dead_event_id, lock=True)
        if preview.fingerprint != command.preview_fingerprint:
            raise ExpenseDeliveryRecoveryError(
                code="operations.expense_requests.recovery_preview_stale",
                message="Recovery evidence changed; preview it again.",
            )
        request = db.get(FieldExpenseRequest, preview.expense_request_id)
        if request is None:
            raise ExpenseDeliveryRecoveryError(
                code="operations.expense_requests.recovery_state_invalid",
                message="The expense recovery evidence is no longer available.",
            )
        staged = stage_expense_delivery_recovery(
            db,
            dead_event_id=command.dead_event_id,
            replacement_idempotency_key=preview.replacement_idempotency_key,
            recovery_contract_version=RECOVERY_CONTRACT_VERSION,
        )

        metadata = dict(request.metadata_ or {})
        recoveries = list(metadata.get("expense_delivery_recoveries") or [])
        evidence = {
            "contract_version": RECOVERY_CONTRACT_VERSION,
            "original_event_id": str(staged.original_event_id),
            "replacement_event_id": str(staged.replacement_event_id),
            "command_id": str(command.context.command_id),
            "actor": command.context.actor,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        if not any(
            item.get("replacement_event_id") == str(staged.replacement_event_id)
            for item in recoveries
            if isinstance(item, dict)
        ):
            recoveries.append(evidence)
            metadata["expense_delivery_recoveries"] = recoveries[-100:]
            request.metadata_ = metadata
        db.flush()
        return ExpenseDeliveryRecoveryOutcome(
            original_event_id=staged.original_event_id,
            replacement_event_id=staged.replacement_event_id,
            replacement_idempotency_key=staged.replacement_idempotency_key,
            replayed=staged.replayed,
        )

    return execute_owner_command(
        db,
        definition=_RECOVER_EXPENSE_DELIVERY,
        context=command.context,
        operation=operation,
    )


def initiate_field_expense_payment_command(
    db: Session, *, command: InitiateFieldExpensePayment
) -> ExpensePaymentInitiationOutcome:
    """Queue one manager-authorized ERP reimbursement command."""

    def operation() -> ExpensePaymentInitiationOutcome:
        request = _locked_expense_request(db, command.expense_request_id)
        if request.status != "approved":
            raise FieldExpenseRequestError(
                code="operations.expense_requests.invalid_transition",
                message="Only approved expense requests can be paid.",
            )
        current = expense_payment_projection(request)
        if current.status in {
            "queued",
            "pending",
            "processing",
            "completed",
            "indeterminate",
        }:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.payment_already_active",
                message="This expense already has an active or unresolved payment.",
            )
        manager = db.get(SystemUser, command.manager_system_user_id)
        manager_email = (manager.email if manager else "").strip()
        if not manager_email:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.manager_email_required",
                message="The manager must have an ERP-matched email to pay expenses.",
            )
        from app.services.backoffice import enqueue_expense_payment

        now = datetime.now(UTC)
        result = enqueue_expense_payment(
            db,
            request,
            command_id=command.context.command_id,
            initiated_by_email=manager_email,
            initiated_at=now,
        )
        if (
            result.status is not BackofficeEnqueueStatus.ENQUEUED
            or result.event is None
        ):
            raise FieldExpenseRequestError(
                code="operations.expense_requests.erp_staging_failed",
                message="Payment could not be queued for ERP. Please retry.",
            )
        metadata = dict(request.metadata_ or {})
        metadata["erp_payment"] = {
            "status": "queued",
            "command_id": str(command.context.command_id),
            "event_id": str(result.event.id),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        request.metadata_ = metadata
        _note_manager_command(
            request,
            event="payment_queued",
            context=command.context,
            reviewer_system_user_id=command.manager_system_user_id,
            occurred_at=now,
        )
        db.flush()
        return ExpensePaymentInitiationOutcome(
            id=request.id,
            status="approved",
            payment_status="queued",
            payment_command_id=command.context.command_id,
            erp_sync_event_id=result.event.id,
        )

    return execute_owner_command(
        db,
        definition=_EXPENSE_PAYMENT_COMMAND,
        context=command.context,
        operation=operation,
    )


def _submission_outcome(
    request: FieldExpenseRequest,
) -> ExpenseRequestSubmissionOutcome:
    if request.client_ref is None or request.submitted_at is None:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.invalid_request",
            message="Submitted expense request evidence is incomplete.",
        )
    return ExpenseRequestSubmissionOutcome(
        id=request.id,
        work_order_id=request.work_order_mirror.public_id,
        requested_by_person_id=request.requested_by_person_id,
        requested_by_system_user_id=request.requested_by_system_user_id,
        status=request.status,
        purpose=request.purpose,
        expense_date=request.expense_date,
        currency=request.currency,
        notes=request.notes,
        client_ref=request.client_ref,
        total_amount=request.total_amount,
        submitted_at=request.submitted_at,
        created_at=request.created_at,
        updated_at=request.updated_at,
        items=tuple(
            ExpenseRequestItemOutcome(
                id=item.id,
                category_code=item.category_code,
                category_name=item.category_name,
                description=item.description,
                amount=item.amount,
                expense_date=item.expense_date,
                vendor_name=item.vendor_name,
                receipt_url=item.receipt_url,
                receipt_attachment_id=item.receipt_attachment_id,
                notes=item.notes,
            )
            for item in request.items
        ),
    )


def _expense_sync_status(
    request: FieldExpenseRequest, delivery: BackofficeDeliveryView | None
) -> ExpenseErpSyncStatus | None:
    if delivery is not None and delivery.event_status is not None:
        try:
            return ExpenseErpSyncStatus(delivery.event_status)
        except ValueError:
            return ExpenseErpSyncStatus.NOT_QUEUED
    if request.status not in {"approved", "paid"}:
        return None
    if delivery is not None and not delivery.sub_owns_delivery:
        return ExpenseErpSyncStatus.NOT_CONFIGURED
    return ExpenseErpSyncStatus.NOT_QUEUED


def _expense_sync_error(delivery: BackofficeDeliveryView | None) -> str | None:
    if delivery is None:
        return None
    if delivery.event_status == ExpenseErpSyncStatus.REJECTED.value:
        return "ERP rejected this expense claim."
    if delivery.event_status == ExpenseErpSyncStatus.DEAD.value:
        if delivery.diagnostic is not None:
            return safe_diagnostic_summary(delivery.diagnostic)
        return "ERP delivery failed after automatic retries."
    return None


def _expense_request_view(
    request: FieldExpenseRequest,
    *,
    requested_by_name: str | None = None,
    delivery: BackofficeDeliveryView | None = None,
    payment_delivery: BackofficeDeliveryView | None = None,
) -> ExpenseRequestView:
    sync_status = _expense_sync_status(request, delivery)
    payment = expense_payment_projection(request)
    payment_status = payment.status
    payment_error = payment.error
    if payment_delivery is not None:
        if payment_delivery.event_status in {"pending", "sent"} and not payment_status:
            payment_status = "queued"
        elif payment_delivery.event_status == "dead":
            payment_status = "delivery_failed"
            payment_error = payment_delivery.last_error
    return ExpenseRequestView(
        id=request.id,
        work_order_id=request.work_order_mirror.public_id,
        crm_expense_request_id=request.crm_expense_request_id,
        requested_by_person_id=request.requested_by_person_id,
        requested_by_system_user_id=request.requested_by_system_user_id,
        requested_by_name=requested_by_name,
        selected_approver_erp_id=request.selected_approver_erp_id,
        selected_approver_name=request.selected_approver_name,
        selected_approver_email=request.selected_approver_email,
        payment_destination_mode=request.payment_destination_mode,
        recipient_bank_name=request.recipient_bank_name,
        masked_account_number=(
            f"******{request.recipient_account_last4}"
            if request.recipient_account_last4
            else None
        ),
        verified_beneficiary_name=request.verified_beneficiary_name,
        status=ExpenseRequestStatus(request.status),
        purpose=request.purpose,
        expense_date=request.expense_date,
        currency=request.currency,
        notes=request.notes,
        rejection_reason=request.rejection_reason,
        expense_system=request.expense_system,
        expense_claim_reference=request.expense_claim_reference,
        expense_claim_number=request.expense_claim_number,
        expense_claim_status=request.expense_claim_status,
        erp_sync_status=sync_status,
        erp_sync_error=_expense_sync_error(delivery),
        payment_status=payment_status,
        payment_intent_id=payment.intent_id,
        payment_error=payment_error,
        client_ref=request.client_ref,
        total_amount=request.total_amount,
        submitted_at=request.submitted_at,
        approved_at=request.approved_at,
        rejected_at=request.rejected_at,
        paid_at=request.paid_at,
        created_at=request.created_at,
        updated_at=request.updated_at,
        items=tuple(
            ExpenseRequestItemOutcome(
                id=item.id,
                category_code=item.category_code,
                category_name=item.category_name,
                description=item.description,
                amount=item.amount,
                expense_date=item.expense_date,
                vendor_name=item.vendor_name,
                receipt_url=item.receipt_url,
                receipt_attachment_id=item.receipt_attachment_id,
                notes=item.notes,
            )
            for item in request.items
        ),
    )


def _legacy_expense_request_view(view: ExpenseRequestView) -> dict[str, object]:
    return {
        "id": view.id,
        "work_order_id": view.work_order_id,
        "crm_expense_request_id": view.crm_expense_request_id,
        "requested_by_person_id": view.requested_by_person_id,
        "requested_by_system_user_id": view.requested_by_system_user_id,
        "requested_by_name": view.requested_by_name,
        "selected_approver_erp_id": view.selected_approver_erp_id,
        "selected_approver_name": view.selected_approver_name,
        "selected_approver_email": view.selected_approver_email,
        "payment_destination_mode": view.payment_destination_mode,
        "recipient_bank_name": view.recipient_bank_name,
        "masked_account_number": view.masked_account_number,
        "verified_beneficiary_name": view.verified_beneficiary_name,
        "status": view.status.value,
        "purpose": view.purpose,
        "expense_date": view.expense_date,
        "currency": view.currency,
        "notes": view.notes,
        "rejection_reason": view.rejection_reason,
        "expense_system": view.expense_system,
        "expense_claim_reference": view.expense_claim_reference,
        "expense_claim_number": view.expense_claim_number,
        "expense_claim_status": view.expense_claim_status,
        "erp_sync_status": (
            view.erp_sync_status.value if view.erp_sync_status is not None else None
        ),
        "erp_sync_error": view.erp_sync_error,
        "payment_status": view.payment_status,
        "payment_intent_id": view.payment_intent_id,
        "payment_error": view.payment_error,
        "client_ref": view.client_ref,
        "total_amount": view.total_amount,
        "submitted_at": view.submitted_at,
        "approved_at": view.approved_at,
        "rejected_at": view.rejected_at,
        "paid_at": view.paid_at,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
        "items": [
            {
                "id": item.id,
                "category_code": item.category_code,
                "category_name": item.category_name,
                "description": item.description,
                "amount": item.amount,
                "expense_date": item.expense_date,
                "vendor_name": item.vendor_name,
                "receipt_url": item.receipt_url,
                "receipt_attachment_id": item.receipt_attachment_id,
                "notes": item.notes,
            }
            for item in view.items
        ],
    }


def serialize_expense_request(
    request: FieldExpenseRequest,
    *,
    delivery: BackofficeDeliveryView | None = None,
    payment_delivery: BackofficeDeliveryView | None = None,
) -> dict[str, object]:
    return _legacy_expense_request_view(
        _expense_request_view(
            request,
            delivery=delivery,
            payment_delivery=payment_delivery,
        )
    )


def _expense_request_views(
    db: Session, requests: list[FieldExpenseRequest]
) -> tuple[ExpenseRequestView, ...]:
    deliveries = get_expense_claim_deliveries(db, [request.id for request in requests])
    payment_deliveries = get_expense_payment_deliveries(
        db, [request.id for request in requests]
    )
    requester_user_ids = _expense_requester_system_user_ids(db, requests)
    requester_identities = resolve_staff_display_identities(
        db,
        query=StaffDisplayIdentityQuery(
            user_ids=frozenset(requester_user_ids.values())
        ),
    )
    requester_names = {
        identity.user_id: identity.display_name
        for identity in requester_identities.identities
    }
    return tuple(
        _expense_request_view(
            request,
            requested_by_name=requester_names.get(requester_user_ids.get(request.id)),
            delivery=deliveries.get(request.id),
            payment_delivery=payment_deliveries.get(request.id),
        )
        for request in requests
    )


def _expense_requester_system_user_ids(
    db: Session, requests: list[FieldExpenseRequest]
) -> dict[UUID, UUID]:
    """Resolve only exact persisted requester bridges for display projection."""

    resolved = {
        request.id: request.requested_by_system_user_id
        for request in requests
        if request.requested_by_system_user_id is not None
    }
    unresolved = [request for request in requests if request.id not in resolved]
    technician_ids = {
        request.requested_by_technician_id
        for request in unresolved
        if request.requested_by_technician_id is not None
    }
    technician_users = (
        dict(
            db.execute(
                select(TechnicianProfile.id, TechnicianProfile.system_user_id).where(
                    TechnicianProfile.id.in_(technician_ids),
                    TechnicianProfile.system_user_id.is_not(None),
                )
            ).all()
        )
        if technician_ids
        else {}
    )
    for request in unresolved:
        system_user_id = technician_users.get(request.requested_by_technician_id)
        if system_user_id is not None:
            resolved[request.id] = system_user_id

    unresolved = [request for request in unresolved if request.id not in resolved]
    person_ids = {request.requested_by_person_id for request in unresolved}
    if not person_ids:
        return resolved
    person_candidates: dict[UUID, set[UUID]] = {
        person_id: set() for person_id in person_ids
    }
    staff_rows = db.execute(
        select(SystemUser.id, SystemUser.person_party_id).where(
            or_(
                SystemUser.id.in_(person_ids),
                SystemUser.person_party_id.in_(person_ids),
            )
        )
    ).all()
    for system_user_id, person_party_id in staff_rows:
        if system_user_id in person_candidates:
            person_candidates[system_user_id].add(system_user_id)
        if person_party_id in person_candidates:
            person_candidates[person_party_id].add(system_user_id)
    profile_rows = db.execute(
        select(TechnicianProfile.person_id, TechnicianProfile.system_user_id).where(
            TechnicianProfile.person_id.in_(person_ids),
            TechnicianProfile.system_user_id.is_not(None),
        )
    ).all()
    for person_id, system_user_id in profile_rows:
        if system_user_id is not None:
            person_candidates[person_id].add(system_user_id)
    for request in unresolved:
        candidates = person_candidates[request.requested_by_person_id]
        if len(candidates) == 1:
            resolved[request.id] = next(iter(candidates))
    return resolved


def list_expense_vendors(
    *, db: Session, query: ListFieldExpenseVendors
) -> tuple[FieldExpenseVendorOption, ...]:
    limit = min(max(query.limit, 1), 100)
    offset = max(query.offset, 0)
    search = (query.search or "").strip()
    rows = db.query(Vendor).filter(Vendor.is_active.is_(True))
    if search:
        rows = rows.filter(Vendor.name.ilike(f"%{search}%"))
    return tuple(
        FieldExpenseVendorOption(id=vendor.id, label=vendor.name)
        for vendor in rows.order_by(Vendor.name.asc(), Vendor.id.asc())
        .limit(limit)
        .offset(offset)
        .all()
    )


def _requester_identity(db: Session, system_user_id: UUID) -> _ExpenseRequesterIdentity:
    system_user = db.execute(
        select(SystemUser).where(
            SystemUser.id == system_user_id,
            SystemUser.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if system_user is None:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.requester_not_found",
            message="The authenticated staff requester was not found.",
        )
    person_ids = tuple(
        dict.fromkeys(
            value
            for value in (system_user.id, system_user.person_party_id)
            if value is not None
        )
    )
    technician_profiles = tuple(
        db.execute(
            select(TechnicianProfile.id, TechnicianProfile.person_id).where(
                or_(
                    TechnicianProfile.system_user_id == system_user.id,
                    TechnicianProfile.person_id.in_(person_ids),
                )
            )
        ).all()
    )
    person_ids = tuple(
        dict.fromkeys(
            (*person_ids, *(person_id for _, person_id in technician_profiles))
        )
    )
    technician_profile_ids = tuple(profile_id for profile_id, _ in technician_profiles)
    return _ExpenseRequesterIdentity(
        system_user_id=system_user.id,
        person_ids=person_ids,
        technician_profile_ids=technician_profile_ids,
    )


def _expense_request_ownership(identity: _ExpenseRequesterIdentity):
    return or_(
        FieldExpenseRequest.requested_by_system_user_id == identity.system_user_id,
        FieldExpenseRequest.requested_by_person_id.in_(identity.person_ids),
        FieldExpenseRequest.requested_by_technician_id.in_(
            identity.technician_profile_ids
        ),
    )


def _requester_expense_query(db: Session, identity: _ExpenseRequesterIdentity):
    return (
        db.query(FieldExpenseRequest)
        .options(selectinload(FieldExpenseRequest.items))
        .filter(_expense_request_ownership(identity))
        .filter(FieldExpenseRequest.is_active.is_(True))
    )


def list_requester_expense_requests(
    db: Session, query: RequesterExpenseHistoryQuery
) -> RequesterExpenseHistoryPage:
    if query.limit < 1 or query.limit > 200 or query.offset < 0:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.invalid_request",
            message="Expense history pagination is invalid.",
        )
    identity = _requester_identity(db, query.system_user_id)
    history = _requester_expense_query(db, identity)
    if query.work_order_id:
        history = history.join(FieldExpenseRequest.work_order_mirror).filter(
            WorkOrder.public_id == query.work_order_id.strip()
        )
    if query.status is not None:
        history = history.filter(FieldExpenseRequest.status == query.status.value)
    total = int(history.with_entities(func.count(FieldExpenseRequest.id)).scalar() or 0)
    rows = (
        history.order_by(FieldExpenseRequest.created_at.desc())
        .offset(query.offset)
        .limit(query.limit)
        .all()
    )
    return RequesterExpenseHistoryPage(
        items=_expense_request_views(db, rows),
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


def get_requester_expense_request(
    db: Session, query: RequesterExpenseDetailQuery
) -> ExpenseRequestView:
    identity = _requester_identity(db, query.system_user_id)
    request = (
        _requester_expense_query(db, identity)
        .filter(FieldExpenseRequest.id == query.request_id)
        .one_or_none()
    )
    if request is None:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.request_not_found",
            message="Expense request was not found.",
        )
    return _expense_request_views(db, [request])[0]


def list_manager_expense_requests(
    db: Session, query: ManagerExpenseReviewQuery
) -> ManagerExpenseReviewPage:
    if query.limit < 1 or query.limit > 200 or query.offset < 0:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.invalid_request",
            message="Manager expense review pagination is invalid.",
        )
    review = (
        db.query(FieldExpenseRequest)
        .options(selectinload(FieldExpenseRequest.items))
        .filter(FieldExpenseRequest.is_active.is_(True))
    )
    if query.status is not None:
        review = review.filter(FieldExpenseRequest.status == query.status.value)
    if query.approver_system_user_id is not None:
        review = review.filter(
            or_(
                FieldExpenseRequest.status != ExpenseRequestStatus.SUBMITTED.value,
                FieldExpenseRequest.selected_approver_system_user_id.is_(None),
                FieldExpenseRequest.selected_approver_system_user_id
                == query.approver_system_user_id,
            )
        )
    total = int(review.with_entities(func.count(FieldExpenseRequest.id)).scalar() or 0)
    rows = (
        review.order_by(FieldExpenseRequest.created_at.desc())
        .offset(query.offset)
        .limit(query.limit)
        .all()
    )
    return ManagerExpenseReviewPage(
        items=_expense_request_views(db, rows),
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


class FieldExpenseRequests:
    @staticmethod
    def list_mine(
        db: Session,
        principal: dict[str, Any],
        *,
        crm_work_order_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        page = list_requester_expense_requests(
            db,
            RequesterExpenseHistoryQuery(
                system_user_id=_principal_system_user_id(principal),
                work_order_id=crm_work_order_id,
                status=ExpenseRequestStatus(_status(status)) if status else None,
                limit=limit,
                offset=offset,
            ),
        )
        return [_legacy_expense_request_view(item) for item in page.items]

    @staticmethod
    def get(
        db: Session, principal: dict[str, Any], expense_request_id: str | UUID
    ) -> dict:
        try:
            return _legacy_expense_request_view(
                get_requester_expense_request(
                    db,
                    RequesterExpenseDetailQuery(
                        system_user_id=_principal_system_user_id(principal),
                        request_id=_expense_request_uuid(expense_request_id),
                    ),
                )
            )
        except FieldExpenseRequestError as exc:
            if exc.code == "operations.expense_requests.request_not_found":
                raise HTTPException(
                    status_code=404, detail="Expense request not found"
                ) from exc
            raise

    @staticmethod
    def list_all(
        db: Session,
        *,
        status: str | None = None,
        approver_system_user_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Manager view: expense requests across all technicians."""
        page = list_manager_expense_requests(
            db,
            ManagerExpenseReviewQuery(
                approver_system_user_id=approver_system_user_id,
                status=ExpenseRequestStatus(_status(status)) if status else None,
                limit=limit,
                offset=offset,
            ),
        )
        return [_legacy_expense_request_view(item) for item in page.items]


def _expense_request_uuid(expense_request_id: str | UUID) -> UUID:
    try:
        return coerce_uuid(expense_request_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail="Expense request not found"
        ) from exc


def _get_request(db: Session, expense_request_id: str | UUID) -> FieldExpenseRequest:
    request = (
        db.query(FieldExpenseRequest)
        .options(selectinload(FieldExpenseRequest.items))
        .filter(FieldExpenseRequest.id == _expense_request_uuid(expense_request_id))
        .filter(FieldExpenseRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Expense request not found")
    return request


def _locked_expense_request(
    db: Session, expense_request_id: UUID
) -> FieldExpenseRequest:
    request = (
        db.query(FieldExpenseRequest)
        .options(
            selectinload(FieldExpenseRequest.items),
            selectinload(FieldExpenseRequest.requested_by_system_user),
            selectinload(FieldExpenseRequest.work_order_mirror),
        )
        .filter(
            FieldExpenseRequest.id == expense_request_id,
            FieldExpenseRequest.is_active.is_(True),
        )
        .with_for_update()
        .one_or_none()
    )
    if request is None:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.request_not_found",
            message="Expense request was not found.",
        )
    return request


def _principal_system_user_id(principal: dict[str, Any]) -> UUID:
    value = principal.get("principal_id")
    try:
        return coerce_uuid(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated user is required"
        ) from exc


def _require_staff_work_order_access(
    db: Session,
    *,
    work_order: WorkOrder,
    access: StaffWorkOrderAccess | None,
) -> None:
    if access is None:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.work_order_unauthorized",
            message="Authorized work-order access evidence is required.",
        )
    if access.global_access:
        return
    subscriber = db.get(Subscriber, work_order.subscriber_id)
    candidates: set[tuple[str, str]] = set()
    if subscriber is not None:
        if subscriber.reseller_id is not None:
            candidates.add(("reseller", str(subscriber.reseller_id)))
        if subscriber.region:
            candidates.add(("region", subscriber.region))
    granted = {(grant.scope_type, grant.scope_id) for grant in access.scopes}
    if not candidates.intersection(granted):
        raise FieldExpenseRequestError(
            code="operations.expense_requests.work_order_unauthorized",
            message="You are not authorized to create expenses for this work order.",
        )


def resolve_authoritative_expense_category_rules(
    db: Session,
) -> tuple[ExpenseCategoryRule, ...]:
    """Resolve the current ERP-owned receipt and amount rules fail-closed."""
    from app.services.field.expense_categories import (
        ExpenseCategoryQueryError,
        ListExpenseCategories,
        list_expense_categories,
    )

    try:
        observed = list_expense_categories(db, ListExpenseCategories())
    except ExpenseCategoryQueryError as exc:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.category_rules_unavailable",
            message="Expense category rules are temporarily unavailable.",
        ) from exc
    rules = tuple(
        ExpenseCategoryRule(
            category_code=item.category_code,
            category_name=item.category_name,
            requires_receipt=item.requires_receipt,
            max_amount_per_claim=item.max_amount_per_claim,
        )
        for item in observed
    )
    if not rules:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.category_rules_unavailable",
            message="ERP has no active expense category rules.",
        )
    return rules


def _supported_receipt_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = parsed.hostname
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or len(value) > 500
    ):
        return False
    if host.casefold() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_expense_receipt_delivery(
    db: Session,
    request: FieldExpenseRequest,
    *,
    category_rules: tuple[ExpenseCategoryRule, ...],
) -> None:
    """Revalidate the exact private/URL evidence that ERP will receive."""
    from app.services.field.attachments import resolve_expense_receipt_attachment

    work_order = request.work_order_mirror
    if work_order is None or not work_order.is_active:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.work_order_not_found",
            message="The authoritative work order is unavailable.",
        )
    rules = {rule.category_code: rule for rule in category_rules}
    allowed_owner_ids = frozenset(
        value
        for value in (
            request.requested_by_system_user_id,
            request.requested_by_person_id,
            request.requested_by_technician_id,
        )
        if value is not None
    )
    for item in request.items:
        rule = rules.get(item.category_code)
        if rule is None:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.category_invalid",
                message="An expense category is no longer available in ERP.",
            )
        receipt_url = (item.receipt_url or "").strip()
        if receipt_url and not _supported_receipt_url(receipt_url):
            raise FieldExpenseRequestError(
                code="operations.expense_requests.receipt_url_invalid",
                message="A receipt URL is not supported for ERP delivery.",
            )
        if item.receipt_attachment_id is not None:
            resolve_expense_receipt_attachment(
                db,
                work_order_id=work_order.id,
                attachment_id=item.receipt_attachment_id,
                allowed_owner_ids=allowed_owner_ids,
            )
        if rule.requires_receipt and not (
            item.receipt_attachment_id is not None or receipt_url
        ):
            raise FieldExpenseRequestError(
                code="operations.expense_requests.receipt_required",
                message=f"A receipt is required for {rule.category_name}.",
            )


def _validate_items(
    db: Session,
    row: WorkOrder,
    items: list[dict[str, Any]],
    *,
    category_rules: tuple[ExpenseCategoryRule, ...] = (),
    attachment_owner_ids: set[UUID] | None = None,
) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    rules_by_code = {
        rule.category_code.strip(): rule
        for rule in category_rules
        if rule.category_code.strip()
    }
    category_totals: dict[str, Decimal] = {}
    for entry in items:
        receipt_attachment_id = entry.get("receipt_attachment_id")
        if receipt_attachment_id:
            attachment = db.get(FieldAttachment, coerce_uuid(receipt_attachment_id))
            if (
                attachment is None
                or not attachment.is_active
                or attachment.work_order_mirror_id != row.id
            ):
                raise HTTPException(
                    status_code=404, detail="Receipt attachment not found"
                )
            if attachment_owner_ids is not None and not {
                owner_id
                for owner_id in (
                    attachment.uploaded_by_system_user_id,
                    attachment.uploaded_by_person_id,
                    attachment.uploaded_by_technician_id,
                )
                if owner_id is not None
            }.intersection(attachment_owner_ids):
                raise HTTPException(
                    status_code=404, detail="Receipt attachment not found"
                )
        amount = _amount(entry.get("amount"))
        category_code = (entry.get("category_code") or "").strip()
        category_rule = rules_by_code.get(category_code) if rules_by_code else None
        if rules_by_code and category_rule is None:
            raise HTTPException(
                status_code=422,
                detail="A selected expense category is unavailable.",
            )
        planned_item = {
            "category_code": category_code,
            "category_name": (
                category_rule.category_name
                if category_rule is not None
                else (entry.get("category_name") or "").strip() or None
            ),
            "description": (entry.get("description") or "").strip(),
            "amount": amount,
            "expense_date": entry.get("expense_date"),
            "vendor_name": (entry.get("vendor_name") or "").strip() or None,
            "receipt_url": (entry.get("receipt_url") or "").strip() or None,
            "receipt_attachment_id": receipt_attachment_id,
            "notes": (entry.get("notes") or "").strip() or None,
        }
        receipt_upload = entry.get("receipt_upload")
        if receipt_upload is not None:
            planned_item["_receipt_upload"] = receipt_upload
        planned.append(planned_item)
        if not planned[-1]["category_code"]:
            raise HTTPException(status_code=422, detail="category_code is required")
        if not planned[-1]["description"]:
            raise HTTPException(status_code=422, detail="description is required")
        if len(planned[-1]["description"]) > 500:
            raise HTTPException(
                status_code=422, detail="description must not exceed 500 characters"
            )
        if category_rule is not None:
            if category_rule.requires_receipt and not (
                planned[-1]["receipt_attachment_id"]
                or planned[-1]["receipt_url"]
                or receipt_upload
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"A receipt is required for {category_rule.category_name}."
                    ),
                )
            category_totals[category_code] = (
                category_totals.get(category_code, Decimal("0")) + amount
            )
    for code, total in category_totals.items():
        rule = rules_by_code[code]
        if rule.max_amount_per_claim is not None and total > rule.max_amount_per_claim:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{rule.category_name} cannot exceed "
                    f"{rule.max_amount_per_claim:.2f} per claim."
                ),
            )
    return planned


def _system_user_id_for_actor(db: Session, context: CommandContext) -> UUID | None:
    raw = context.actor.rsplit(":", 1)[-1]
    try:
        actor_id = UUID(raw)
    except ValueError:
        return None
    return actor_id if db.get(SystemUser, actor_id) is not None else None


def _requesting_technician(
    db: Session,
    *,
    requester_person_id: UUID | None,
    system_user_id: UUID,
) -> TechnicianProfile | None:
    query = db.query(TechnicianProfile).filter(TechnicianProfile.is_active.is_(True))
    if requester_person_id is not None:
        query = query.filter(
            or_(
                TechnicianProfile.person_id == requester_person_id,
                TechnicianProfile.system_user_id == system_user_id,
            )
        )
    else:
        query = query.filter(TechnicianProfile.system_user_id == system_user_id)
    return query.order_by(TechnicianProfile.created_at.desc()).first()


def _amount(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise HTTPException(status_code=422, detail="amount must be numeric") from exc
    if amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be greater than zero")
    return amount.quantize(Decimal("0.01"))


def _currency(value: str | None) -> str:
    currency = (value or "NGN").strip().upper()
    if len(currency) != 3:
        raise HTTPException(status_code=422, detail="currency must be a 3-letter code")
    return currency


def _status(value: str) -> str:
    status = (value or "").strip().lower()
    if status not in FIELD_EXPENSE_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unsupported status: {value}")
    return status


def _mark_sub_authoritative(row: WorkOrder) -> None:
    _mark_source_authoritative(row, "expense_requests")


def _enqueue_decision_backoffice(
    db: Session,
    request: FieldExpenseRequest,
    *,
    action: str,
    decision_id: UUID,
    reviewer_system_user_id: UUID,
    decided_at: datetime,
    reason: str | None = None,
) -> BackofficeEnqueueResult:
    """Stage the single approval-gated ERP release event."""
    from app.services.backoffice import enqueue_expense_decision

    reviewer = db.get(SystemUser, reviewer_system_user_id)
    reviewer_email = (reviewer.email if reviewer else "").strip()
    if not reviewer_email:
        return BackofficeEnqueueResult(status=BackofficeEnqueueStatus.NOT_ENQUEUED)
    result = enqueue_expense_decision(
        db,
        request,
        action=action,
        decision_id=decision_id,
        decided_by_email=reviewer_email,
        decided_at=decided_at,
        reason=reason,
    )
    db.flush()
    return result


def _note_approval_command(
    request: FieldExpenseRequest,
    command: ApproveFieldExpenseRequest,
    *,
    occurred_at: datetime,
) -> None:
    _note_manager_command(
        request,
        event="approved",
        context=command.context,
        reviewer_system_user_id=command.reviewer_system_user_id,
        occurred_at=occurred_at,
    )


def _note_manager_command(
    request: FieldExpenseRequest,
    *,
    event: str,
    context: CommandContext,
    reviewer_system_user_id: UUID,
    occurred_at: datetime,
) -> None:
    metadata = dict(request.metadata_ or {})
    events = list(metadata.get("manager_events") or [])
    events.append(
        {
            "event": event,
            "occurred_at": occurred_at.isoformat(),
            "actor": context.actor,
            "reviewer_system_user_id": str(reviewer_system_user_id),
            "command_id": str(context.command_id),
        }
    )
    metadata["manager_events"] = events[-100:]
    request.metadata_ = metadata


def _approval_outcome(
    db: Session, request: FieldExpenseRequest
) -> ExpenseRequestApprovalOutcome:
    if request.approved_at is None:
        raise FieldExpenseRequestError(
            code="operations.expense_requests.incomplete_approval",
            message="Approved expense evidence is incomplete.",
        )
    delivery = get_expense_decision_delivery(db, request.id, "approve")
    sync_status = _expense_sync_status(request, delivery)
    if sync_status is None:
        sync_status = ExpenseErpSyncStatus.NOT_QUEUED
    return ExpenseRequestApprovalOutcome(
        id=request.id,
        status="approved",
        approved_at=request.approved_at,
        erp_sync_status=sync_status,
        erp_sync_event_id=delivery.event_id,
        erp_sync_error=_expense_sync_error(delivery),
    )


field_expense_requests = FieldExpenseRequests()
