"""Native field expense requests."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.field_attachment import FieldAttachment
from app.models.field_expense import (
    FIELD_EXPENSE_STATUSES,
    FieldExpenseRequest,
    FieldExpenseRequestItem,
)
from app.models.work_order import WorkOrder
from app.services.common import apply_pagination, coerce_uuid
from app.services.field.jobs import _profile_from_principal, _scoped_query
from app.services.field.source import (
    mark_sub_authoritative as _mark_source_authoritative,
)

logger = logging.getLogger(__name__)


def serialize_expense_request(request: FieldExpenseRequest) -> dict:
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
        scoped = _scoped_query(db, profile)
        if crm_work_order_id:
            scoped = scoped.filter(WorkOrder.public_id == crm_work_order_id)
        scoped_ids = scoped.with_entities(WorkOrder.id)
        query = (
            db.query(FieldExpenseRequest)
            .options(selectinload(FieldExpenseRequest.items))
            .filter(FieldExpenseRequest.work_order_mirror_id.in_(scoped_ids))
            .filter(FieldExpenseRequest.is_active.is_(True))
            .order_by(FieldExpenseRequest.created_at.desc())
        )
        if status:
            query = query.filter(FieldExpenseRequest.status == _status(status))
        return [
            serialize_expense_request(request)
            for request in apply_pagination(query, limit, offset).all()
        ]

    @staticmethod
    def get(db: Session, principal: dict[str, Any], expense_request_id: str) -> dict:
        return serialize_expense_request(
            _get_scoped_request(db, principal, expense_request_id)
        )

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
    def submit(db: Session, principal: dict[str, Any], expense_request_id: str) -> dict:
        request = _get_scoped_request(db, principal, expense_request_id)
        profile = _profile_from_principal(db, principal)
        if request.requested_by_technician_id != profile.id:
            raise HTTPException(status_code=404, detail="Expense request not found")
        if request.status != "draft":
            raise HTTPException(status_code=409, detail="Only draft requests submit")
        request.status = "submitted"
        request.submitted_at = datetime.now(UTC)
        _mark_sub_authoritative(request.work_order_mirror)
        _maybe_enqueue_backoffice(db, request)
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
        return [
            serialize_expense_request(request)
            for request in apply_pagination(query, limit, offset).all()
        ]

    @staticmethod
    def approve(db: Session, expense_request_id: str) -> dict:
        request = _get_request(db, expense_request_id)
        if request.status != "submitted":
            raise HTTPException(
                status_code=409, detail="Only submitted requests approve"
            )
        request.status = "approved"
        request.approved_at = datetime.now(UTC)
        request.rejection_reason = None
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_expense_request(request)

    @staticmethod
    def reject(db: Session, expense_request_id: str, reason: str) -> dict:
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
    def cancel(db: Session, principal: dict[str, Any], expense_request_id: str) -> dict:
        request = _get_scoped_request(db, principal, expense_request_id)
        profile = _profile_from_principal(db, principal)
        if request.requested_by_technician_id != profile.id:
            raise HTTPException(status_code=404, detail="Expense request not found")
        if request.status not in {"draft", "submitted"}:
            raise HTTPException(
                status_code=409, detail="Only draft or submitted requests cancel"
            )
        request.status = "canceled"
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_expense_request(request)


def _get_request(db: Session, expense_request_id: str) -> FieldExpenseRequest:
    request = (
        db.query(FieldExpenseRequest)
        .options(selectinload(FieldExpenseRequest.items))
        .filter(FieldExpenseRequest.id == coerce_uuid(expense_request_id))
        .filter(FieldExpenseRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Expense request not found")
    return request


def _get_scoped_request(
    db: Session, principal: dict[str, Any], expense_request_id: str
) -> FieldExpenseRequest:
    profile = _profile_from_principal(db, principal)
    scoped_ids = _scoped_query(db, profile).with_entities(WorkOrder.id)
    request = (
        db.query(FieldExpenseRequest)
        .options(selectinload(FieldExpenseRequest.items))
        .filter(FieldExpenseRequest.id == coerce_uuid(expense_request_id))
        .filter(FieldExpenseRequest.work_order_mirror_id.in_(scoped_ids))
        .filter(FieldExpenseRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Expense request not found")
    return request


def _validate_items(
    db: Session, row: WorkOrder, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
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
        planned.append(
            {
                "category_code": (entry.get("category_code") or "").strip(),
                "category_name": (entry.get("category_name") or "").strip() or None,
                "description": (entry.get("description") or "").strip(),
                "amount": amount,
                "expense_date": entry.get("expense_date"),
                "vendor_name": (entry.get("vendor_name") or "").strip() or None,
                "receipt_url": (entry.get("receipt_url") or "").strip() or None,
                "receipt_attachment_id": receipt_attachment_id,
                "notes": (entry.get("notes") or "").strip() or None,
            }
        )
        if not planned[-1]["category_code"]:
            raise HTTPException(status_code=422, detail="category_code is required")
        if not planned[-1]["description"]:
            raise HTTPException(status_code=422, detail="description is required")
    return planned


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


def _maybe_enqueue_backoffice(db: Session, request: FieldExpenseRequest) -> None:
    """Stage a replaceable back-office claim without blocking Sub submission."""
    from app.services.backoffice import enqueue_expense_claim

    try:
        with db.begin_nested():
            result = enqueue_expense_claim(db, request)
        if result.requires_attention:
            _note_backoffice_delivery_pending(request)
    except Exception:
        _note_backoffice_delivery_pending(request)
        logger.warning(
            "field expense %s: back-office enqueue failed; submission retained",
            request.id,
            exc_info=True,
        )


def _note_backoffice_delivery_pending(request: FieldExpenseRequest) -> None:
    metadata = dict(request.metadata_ or {})
    events = list(metadata.get("backoffice_events") or [])
    events.append(
        {
            "event": "backoffice_delivery_pending",
            "at": datetime.now(UTC).isoformat(),
        }
    )
    metadata["backoffice_events"] = events[-100:]
    request.metadata_ = metadata


field_expense_requests = FieldExpenseRequests()
