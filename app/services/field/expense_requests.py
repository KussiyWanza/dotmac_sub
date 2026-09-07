"""Native field expense requests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.dispatch import TechnicianProfile
from app.models.field_attachment import FieldAttachment
from app.models.field_expense import (
    FIELD_EXPENSE_STATUSES,
    FieldExpenseRequest,
    FieldExpenseRequestItem,
)
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services.backoffice import (
    BackofficeDeliveryView,
    BackofficeEnqueueResult,
    BackofficeEnqueueStatus,
    get_expense_claim_deliveries,
)
from app.services.common import apply_pagination, coerce_uuid
from app.services.domain_errors import DomainError
from app.services.field.jobs import _profile_from_principal, _scoped_query
from app.services.field.source import (
    mark_sub_authoritative as _mark_source_authoritative,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)


class ExpenseRequestAccessMode(StrEnum):
    FIELD_ASSIGNMENT = "field_assignment"
    STAFF_WORK_ORDER = "staff_work_order"


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
class SubmitFieldExpenseRequest:
    context: CommandContext
    requester_person_id: UUID | None
    work_order_public_id: str
    request_id: UUID
    purpose: str
    expense_date: date | None
    currency: str
    notes: str | None
    items: tuple[ExpenseRequestLineInput, ...]
    access_mode: ExpenseRequestAccessMode = ExpenseRequestAccessMode.FIELD_ASSIGNMENT
    authorized_work_order_id: UUID | None = None
    category_rules: tuple[ExpenseCategoryRule, ...] = ()


@dataclass(frozen=True, slots=True)
class ApproveFieldExpenseRequest:
    context: CommandContext
    expense_request_id: UUID
    reviewer_system_user_id: UUID


class ExpenseErpSyncStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEAD = "dead"
    NOT_CONFIGURED = "not_configured"
    NOT_QUEUED = "not_queued"


@dataclass(frozen=True, slots=True)
class ExpenseRequestApprovalOutcome:
    id: UUID
    status: Literal["approved"]
    approved_at: datetime
    erp_sync_status: ExpenseErpSyncStatus
    erp_sync_event_id: UUID | None
    erp_sync_error: str | None


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


def _expense_fingerprint(command: SubmitFieldExpenseRequest) -> str:
    payload = {
        "work_order_public_id": command.work_order_public_id,
        "purpose": command.purpose.strip(),
        "expense_date": str(command.expense_date) if command.expense_date else None,
        "currency": command.currency.strip().upper(),
        "notes": (command.notes or "").strip() or None,
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
        profile = _requesting_technician(
            db,
            requester_person_id=command.requester_person_id,
            system_user_id=system_user_id,
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
                or existing.work_order_mirror.public_id != command.work_order_public_id
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
        if command.access_mode == ExpenseRequestAccessMode.FIELD_ASSIGNMENT:
            if profile is None:
                raise FieldExpenseRequestError(
                    code="operations.expense_requests.requester_not_found",
                    message="Technician profile not found.",
                )
            row = (
                _scoped_query(db, profile)
                .filter(WorkOrder.public_id == command.work_order_public_id)
                .with_for_update()
                .one_or_none()
            )
        else:
            if command.authorized_work_order_id is None:
                raise FieldExpenseRequestError(
                    code="operations.expense_requests.work_order_not_found",
                    message="Authorized work-order access evidence is required.",
                )
            row = db.execute(
                select(WorkOrder)
                .where(
                    WorkOrder.id == command.authorized_work_order_id,
                    WorkOrder.public_id == command.work_order_public_id,
                    WorkOrder.is_active.is_(True),
                )
                .with_for_update()
            ).scalar_one_or_none()
        if row is None:
            raise FieldExpenseRequestError(
                code="operations.expense_requests.work_order_not_found",
                message="Job not found.",
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
            planned_items = _validate_items(
                db,
                row,
                raw_items,
                category_rules=command.category_rules,
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
        now = datetime.now(UTC)
        request = FieldExpenseRequest(
            work_order_mirror_id=row.id,
            requested_by_technician_id=profile.id if profile else None,
            requested_by_person_id=system_user.person_party_id or system_user.id,
            requested_by_system_user_id=system_user.id,
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
        _mark_sub_authoritative(row)
        db.flush()
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

        now = datetime.now(UTC)
        request.status = "approved"
        request.approved_at = now
        request.rejection_reason = None
        _note_approval_command(request, command, occurred_at=now)
        _mark_sub_authoritative(request.work_order_mirror)

        try:
            result = _enqueue_approved_backoffice(db, request)
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
        return "ERP delivery failed after automatic retries."
    return None


def serialize_expense_request(
    request: FieldExpenseRequest,
    *,
    delivery: BackofficeDeliveryView | None = None,
) -> dict:
    sync_status = _expense_sync_status(request, delivery)
    return {
        "id": request.id,
        "work_order_id": request.work_order_mirror.public_id,
        "crm_expense_request_id": request.crm_expense_request_id,
        "requested_by_person_id": request.requested_by_person_id,
        "requested_by_system_user_id": request.requested_by_system_user_id,
        "status": request.status,
        "purpose": request.purpose,
        "expense_date": request.expense_date,
        "currency": request.currency,
        "notes": request.notes,
        "rejection_reason": request.rejection_reason,
        "expense_system": request.expense_system,
        "expense_claim_reference": request.expense_claim_reference,
        "expense_claim_number": request.expense_claim_number,
        "expense_claim_status": request.expense_claim_status,
        "erp_sync_status": sync_status.value if sync_status is not None else None,
        "erp_sync_error": _expense_sync_error(delivery),
        "client_ref": request.client_ref,
        "total_amount": request.total_amount,
        "submitted_at": request.submitted_at,
        "approved_at": request.approved_at,
        "rejected_at": request.rejected_at,
        "paid_at": request.paid_at,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
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
            for item in request.items
        ],
    }


def _serialize_expense_requests(
    db: Session, requests: list[FieldExpenseRequest]
) -> list[dict]:
    deliveries = get_expense_claim_deliveries(db, [request.id for request in requests])
    return [
        serialize_expense_request(request, delivery=deliveries.get(request.id))
        for request in requests
    ]


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
        profile = _profile_from_principal(db, principal)
        ownership = _expense_request_ownership(profile)
        query = (
            db.query(FieldExpenseRequest)
            .options(selectinload(FieldExpenseRequest.items))
            .filter(ownership)
            .filter(FieldExpenseRequest.is_active.is_(True))
            .order_by(FieldExpenseRequest.created_at.desc())
        )
        if crm_work_order_id:
            query = query.join(FieldExpenseRequest.work_order_mirror).filter(
                WorkOrder.public_id == crm_work_order_id
            )
        if status:
            query = query.filter(FieldExpenseRequest.status == _status(status))
        requests = apply_pagination(query, limit, offset).all()
        return _serialize_expense_requests(db, requests)

    @staticmethod
    def get(
        db: Session, principal: dict[str, Any], expense_request_id: str | UUID
    ) -> dict:
        request = _get_scoped_request(db, principal, expense_request_id)
        return _serialize_expense_requests(db, [request])[0]

    @staticmethod
    def create(
        db: Session,
        principal: dict[str, Any],
        *,
        crm_work_order_id: str,
        purpose: str,
        expense_date,
        currency: str,
        notes: str | None,
        client_ref,
        items: list[dict[str, Any]],
    ) -> dict:
        if not items:
            raise HTTPException(status_code=422, detail="At least one item is required")
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        client_uuid = coerce_uuid(client_ref) if client_ref else None
        if client_uuid is not None:
            existing = (
                db.query(FieldExpenseRequest)
                .options(selectinload(FieldExpenseRequest.items))
                .filter(FieldExpenseRequest.client_ref == client_uuid)
                .filter(FieldExpenseRequest.requested_by_technician_id == profile.id)
                .one_or_none()
            )
            if existing is not None:
                return serialize_expense_request(existing)
        planned_items = _validate_items(db, row, items)
        request = FieldExpenseRequest(
            work_order_mirror_id=row.id,
            requested_by_technician_id=profile.id,
            requested_by_person_id=profile.person_id,
            requested_by_system_user_id=profile.system_user_id,
            status="draft",
            purpose=(purpose or "").strip(),
            expense_date=expense_date,
            currency=_currency(currency),
            notes=(notes or "").strip() or None,
            client_ref=client_uuid,
        )
        if not request.purpose:
            raise HTTPException(status_code=422, detail="purpose is required")
        db.add(request)
        db.flush()
        for item in planned_items:
            request.items.append(FieldExpenseRequestItem(**item))
        _mark_sub_authoritative(row)
        db.commit()
        db.refresh(request)
        return serialize_expense_request(request)

    @staticmethod
    def submit(
        db: Session, principal: dict[str, Any], expense_request_id: str | UUID
    ) -> dict:
        request = _get_scoped_request(db, principal, expense_request_id)
        if request.status != "draft":
            raise HTTPException(status_code=409, detail="Only draft requests submit")
        request.status = "submitted"
        request.submitted_at = datetime.now(UTC)
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_expense_request(request)

    @staticmethod
    def list_all(
        db: Session,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Manager view: expense requests across all technicians."""
        query = (
            db.query(FieldExpenseRequest)
            .options(selectinload(FieldExpenseRequest.items))
            .filter(FieldExpenseRequest.is_active.is_(True))
            .order_by(FieldExpenseRequest.created_at.desc())
        )
        if status:
            query = query.filter(FieldExpenseRequest.status == _status(status))
        requests = apply_pagination(query, limit, offset).all()
        return _serialize_expense_requests(db, requests)

    @staticmethod
    def reject(db: Session, expense_request_id: str | UUID, reason: str) -> dict:
        request = _get_request(db, expense_request_id)
        if request.status != "submitted":
            raise HTTPException(
                status_code=409, detail="Only submitted requests reject"
            )
        cleaned = (reason or "").strip()
        if not cleaned:
            raise HTTPException(status_code=422, detail="reason is required")
        request.status = "rejected"
        request.rejected_at = datetime.now(UTC)
        request.rejection_reason = cleaned[:500]
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_expense_request(request)

    @staticmethod
    def cancel(
        db: Session, principal: dict[str, Any], expense_request_id: str | UUID
    ) -> dict:
        request = _get_scoped_request(db, principal, expense_request_id)
        if request.status not in {"draft", "submitted"}:
            raise HTTPException(
                status_code=409, detail="Only draft or submitted requests cancel"
            )
        request.status = "canceled"
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_expense_request(request)


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


def _get_scoped_request(
    db: Session, principal: dict[str, Any], expense_request_id: str | UUID
) -> FieldExpenseRequest:
    profile = _profile_from_principal(db, principal)
    request = (
        db.query(FieldExpenseRequest)
        .options(selectinload(FieldExpenseRequest.items))
        .filter(FieldExpenseRequest.id == _expense_request_uuid(expense_request_id))
        .filter(_expense_request_ownership(profile))
        .filter(FieldExpenseRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Expense request not found")
    return request


def _expense_request_ownership(profile: TechnicianProfile):
    ownership = or_(
        FieldExpenseRequest.requested_by_person_id == profile.person_id,
        FieldExpenseRequest.requested_by_technician_id == profile.id,
    )
    if profile.system_user_id is not None:
        ownership = or_(
            ownership,
            FieldExpenseRequest.requested_by_system_user_id == profile.system_user_id,
        )
    return ownership


def _validate_items(
    db: Session,
    row: WorkOrder,
    items: list[dict[str, Any]],
    *,
    category_rules: tuple[ExpenseCategoryRule, ...] = (),
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


def _enqueue_approved_backoffice(
    db: Session, request: FieldExpenseRequest
) -> BackofficeEnqueueResult:
    """Flush-only required participant used by the approval owner after cutover."""
    from app.services.backoffice import enqueue_expense_claim

    result = enqueue_expense_claim(db, request)
    db.flush()
    return result


def _note_approval_command(
    request: FieldExpenseRequest,
    command: ApproveFieldExpenseRequest,
    *,
    occurred_at: datetime,
) -> None:
    metadata = dict(request.metadata_ or {})
    events = list(metadata.get("manager_events") or [])
    events.append(
        {
            "event": "approved",
            "occurred_at": occurred_at.isoformat(),
            "actor": command.context.actor,
            "reviewer_system_user_id": str(command.reviewer_system_user_id),
            "command_id": str(command.context.command_id),
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
    delivery = get_expense_claim_deliveries(db, [request.id])[request.id]
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
