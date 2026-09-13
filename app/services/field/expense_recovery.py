"""Explicit, append-only recovery for dead approved-expense deliveries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
)
from app.models.field_expense import FieldExpenseRequest
from app.services.domain_errors import DomainError
from app.services.field.expense_requests import (
    resolve_authoritative_expense_category_rules,
    validate_expense_receipt_delivery,
)
from app.services.integrations.diagnostics import (
    DELIVERY_DIAGNOSTIC_KEY,
    parse_diagnostic_evidence,
)
from app.services.integrations.erp_capability import (
    ErpCapabilityError,
    capability_client,
)

RECOVERY_CONTRACT_VERSION = "expense-delivery-recovery.v1"
PAYMENT_RECOVERY_CONTRACT_VERSION = "expense-payment-delivery-recovery.v1"


class ExpenseDeliveryRecoveryError(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class PreviewExpenseDeliveryRecovery:
    dead_event_id: UUID


@dataclass(frozen=True, slots=True)
class ExpenseDeliveryRecoveryPreview:
    dead_event_id: UUID
    expense_request_id: UUID
    replacement_idempotency_key: str
    fingerprint: str
    erp_claim_status: str | None


@dataclass(frozen=True, slots=True)
class PreviewExpensePaymentDeliveryRecovery:
    dead_event_id: UUID


@dataclass(frozen=True, slots=True)
class ExpensePaymentDeliveryRecoveryPreview:
    dead_event_id: UUID
    expense_request_id: UUID
    idempotency_key: str
    fingerprint: str
    erp_claim_status: Literal["approved"]


def _replacement_key(event: FieldErpSyncEvent) -> str:
    return f"{event.idempotency_key}:{RECOVERY_CONTRACT_VERSION}"


def _load_recoverable(
    db: Session,
    event_id: UUID,
    *,
    lock: bool,
) -> tuple[FieldErpSyncEvent, FieldExpenseRequest]:
    query = db.query(FieldErpSyncEvent).filter(FieldErpSyncEvent.id == event_id)
    if lock:
        query = query.with_for_update()
    event = query.one_or_none()
    if (
        event is None
        or event.flow != FieldErpSyncFlow.expense_claim.value
        or event.status != FieldErpSyncStatus.dead.value
        or str((event.payload or {}).get("_expense_action")) != "release_approved_v2"
    ):
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_not_available",
            message="The event is not a recoverable dead expense delivery.",
        )
    request = db.get(FieldExpenseRequest, event.entity_id)
    if (
        request is None
        or request.status != "approved"
        or request.approved_at is None
        or request.work_order_mirror is None
        or not request.work_order_mirror.is_active
    ):
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_state_invalid",
            message="The expense or work-order approval evidence is no longer valid.",
        )
    return event, request


def _erp_status(db: Session, request_id: UUID) -> str | None:
    try:
        with capability_client(db) as client:
            observed = client.get_expense_claim_status(str(request_id))
    except ErpCapabilityError as exc:
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_erp_unavailable",
            message="ERP state could not be verified for recovery.",
        ) from exc
    if observed is None:
        return None
    raw = observed.get("claim_status") or observed.get("status")
    status = str(raw or "").strip().lower()
    if status not in {"draft", "submitted", "pending_approval"}:
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_ambiguous",
            message="ERP state does not permit an unambiguous expense recovery.",
        )
    return status


def _preview(
    db: Session,
    event_id: UUID,
    *,
    lock: bool,
) -> ExpenseDeliveryRecoveryPreview:
    event, request = _load_recoverable(db, event_id, lock=lock)
    rules = resolve_authoritative_expense_category_rules(db)
    validate_expense_receipt_delivery(db, request, category_rules=rules)
    erp_status = _erp_status(db, request.id)
    approved_at = request.approved_at
    if approved_at is None:
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_ambiguous",
            message="Only an approved expense can be recovered.",
        )
    evidence = {
        "contract_version": RECOVERY_CONTRACT_VERSION,
        "event_id": str(event.id),
        "event_updated_at": event.updated_at.isoformat(),
        "expense_request_id": str(request.id),
        "expense_updated_at": request.updated_at.isoformat(),
        "approved_at": approved_at.isoformat(),
        "work_order_id": request.work_order_mirror.public_id,
        "erp_claim_status": erp_status,
        "lines": [
            {
                "id": str(item.id),
                "category": item.category_code,
                "attachment_id": (
                    str(item.receipt_attachment_id)
                    if item.receipt_attachment_id is not None
                    else None
                ),
                "receipt_url": item.receipt_url,
            }
            for item in request.items
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    replacement_key = _replacement_key(event)
    return ExpenseDeliveryRecoveryPreview(
        dead_event_id=event.id,
        expense_request_id=request.id,
        replacement_idempotency_key=replacement_key,
        fingerprint=fingerprint,
        erp_claim_status=erp_status,
    )


def preview_expense_delivery_recovery(
    db: Session,
    query: PreviewExpenseDeliveryRecovery,
) -> ExpenseDeliveryRecoveryPreview:
    return _preview(db, query.dead_event_id, lock=False)


def _load_recoverable_payment(
    db: Session,
    event_id: UUID,
    *,
    lock: bool,
) -> tuple[FieldErpSyncEvent, FieldExpenseRequest]:
    query = db.query(FieldErpSyncEvent).filter(FieldErpSyncEvent.id == event_id)
    if lock:
        query = query.with_for_update()
    event = query.one_or_none()
    diagnostic = (
        parse_diagnostic_evidence(
            (event.erp_response or {}).get(DELIVERY_DIAGNOSTIC_KEY)
        )
        if event is not None and isinstance(event.erp_response, dict)
        else None
    )
    if (
        event is None
        or event.flow != FieldErpSyncFlow.expense_claim.value
        or event.status != FieldErpSyncStatus.dead.value
        or str((event.payload or {}).get("_expense_action")) != "initiate_payment"
        or diagnostic is None
        or diagnostic.code != "permission_denied"
        or diagnostic.http_status != 403
        or diagnostic.operation != "initiate_expense_payment"
    ):
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.payment_recovery_not_available",
            message=(
                "The event is not an eligible permission-denied payment delivery."
            ),
        )
    request = db.get(FieldExpenseRequest, event.entity_id)
    if (
        request is None
        or request.status != "approved"
        or request.approved_at is None
        or request.work_order_mirror is None
        or not request.work_order_mirror.is_active
    ):
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.payment_recovery_state_invalid",
            message="The expense approval evidence is no longer valid.",
        )
    return event, request


def _erp_payment_recovery_status(db: Session, request_id: UUID) -> Literal["approved"]:
    try:
        with capability_client(db) as client:
            observed = client.get_expense_claim_status(str(request_id))
    except ErpCapabilityError as exc:
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.payment_recovery_erp_unavailable",
            message="ERP payment state could not be verified for recovery.",
        ) from exc
    if not isinstance(observed, dict):
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.payment_recovery_ambiguous",
            message="ERP payment state does not permit an unambiguous recovery.",
        )
    raw_status = observed.get("claim_status") or observed.get("status")
    claim_status = str(raw_status or "").strip().lower()
    payment_evidence = (
        observed.get("payment_status"),
        observed.get("payment_intent_id"),
        observed.get("paid_at"),
    )
    if claim_status != "approved" or any(payment_evidence):
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.payment_recovery_ambiguous",
            message="ERP payment state does not permit an unambiguous recovery.",
        )
    return "approved"


def _preview_payment(
    db: Session,
    event_id: UUID,
    *,
    lock: bool,
) -> ExpensePaymentDeliveryRecoveryPreview:
    event, request = _load_recoverable_payment(db, event_id, lock=lock)
    claim_status = _erp_payment_recovery_status(db, request.id)
    approved_at = request.approved_at
    if approved_at is None:
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.payment_recovery_state_invalid",
            message="The expense approval evidence is no longer valid.",
        )
    diagnostic = parse_diagnostic_evidence(
        (event.erp_response or {}).get(DELIVERY_DIAGNOSTIC_KEY)
    )
    if diagnostic is None:
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.payment_recovery_not_available",
            message=(
                "The event is not an eligible permission-denied payment delivery."
            ),
        )
    evidence = {
        "contract_version": PAYMENT_RECOVERY_CONTRACT_VERSION,
        "event_id": str(event.id),
        "event_updated_at": event.updated_at.isoformat(),
        "expense_request_id": str(request.id),
        "expense_updated_at": request.updated_at.isoformat(),
        "approved_at": approved_at.isoformat(),
        "idempotency_key": event.idempotency_key,
        "diagnostic": {
            "code": diagnostic.code,
            "http_status": diagnostic.http_status,
            "operation": diagnostic.operation,
            "request_id": (
                str(diagnostic.request_id)
                if diagnostic.request_id is not None
                else None
            ),
        },
        "erp_claim_status": claim_status,
        "erp_payment_absent": True,
    }
    fingerprint = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ExpensePaymentDeliveryRecoveryPreview(
        dead_event_id=event.id,
        expense_request_id=request.id,
        idempotency_key=event.idempotency_key,
        fingerprint=fingerprint,
        erp_claim_status=claim_status,
    )


def preview_expense_payment_delivery_recovery(
    db: Session,
    query: PreviewExpensePaymentDeliveryRecovery,
) -> ExpensePaymentDeliveryRecoveryPreview:
    """Preview requeue of the same permission-denied payment event and key."""

    return _preview_payment(db, query.dead_event_id, lock=False)
