from __future__ import annotations

import hashlib
import hmac
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.erp_staff_access_webhook import (
    ERP_STAFF_ACCESS_WEBHOOK_ADAPTER,
    ErpStaffAccessReceipt,
    ErpStaffAccountStatusEvent,
    ErpStaffLeaveRestrictionEvent,
    ErpStaffLeaveRestrictionWebhook,
)
from app.services import erp_staff_access
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.backoffice_contracts import (
    ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY,
)
from app.services.integrations.runtime_execution import (
    RuntimeExecutionError,
    build_execution_context,
)
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/webhooks/erp-staff-access", tags=["erp-staff-access"])
MAX_BODY_BYTES = 128 * 1024


def _audit_rejection(
    db: Session,
    *,
    action: str,
    installation_id: UUID,
    capability_binding_id: UUID,
    status_code: int,
    metadata: dict[str, object] | None = None,
) -> None:
    erp_staff_access.record_staff_access_webhook_rejection(
        db,
        action=action,
        installation_id=installation_id,
        capability_binding_id=capability_binding_id,
        status_code=status_code,
        metadata=metadata or {},
    )


@router.post("/{capability_binding_id}", response_model=ErpStaffAccessReceipt)
async def receive_erp_staff_access(
    capability_binding_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> ErpStaffAccessReceipt:
    try:
        execution = build_execution_context(
            db, capability_binding_id=capability_binding_id
        )
    except RuntimeExecutionError as exc:
        raise HTTPException(
            status_code=503, detail="ERP staff access webhook is unavailable"
        ) from exc
    if execution.binding.capability_id != ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY:
        raise HTTPException(
            status_code=404, detail="ERP staff access webhook binding not found"
        )
    installation_id = execution.binding.installation_id
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413, detail="ERP staff access webhook payload is too large"
        )
    delivery_id = str(request.headers.get("X-Dotmac-Delivery") or "").strip()
    if not delivery_id:
        raise HTTPException(status_code=400, detail="ERP delivery id is required")
    presented = str(request.headers.get("X-Dotmac-Signature") or "")
    secret = execution.secret_material["webhook_signing_secret"]
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented, expected):
        _audit_rejection(
            db,
            action="auth.erp_staff_access_webhook_signature_invalid",
            installation_id=installation_id,
            capability_binding_id=capability_binding_id,
            status_code=401,
        )
        raise HTTPException(status_code=401, detail="Invalid ERP webhook signature")
    try:
        payload = ERP_STAFF_ACCESS_WEBHOOK_ADAPTER.validate_json(raw)
    except (ValueError, ValidationError):
        raise HTTPException(status_code=422, detail="Invalid ERP staff access payload")

    receipt, should_process = integration_inbox.receive_and_claim_verified(
        db,
        capability_binding_id=capability_binding_id,
        provider_event_id=delivery_id,
        event_type=payload.event_type,
        payload=payload.model_dump(mode="json", exclude_none=True),
        headers={"content-type": str(request.headers.get("content-type") or "")},
    )
    if not should_process:
        consequence = receipt.consequence_json or {}
        return ErpStaffAccessReceipt(
            event_id=str(consequence["event_id"]),
            event_type=str(consequence["event_type"]),
            applied=bool(consequence["applied"]),
            replayed=True,
            status=str(consequence["status"]),
        )

    receipt_id = receipt.id
    command_id = uuid5(
        NAMESPACE_URL,
        f"erp-staff-access:{capability_binding_id}:{delivery_id}",
    )
    context = CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"service:{installation_id}",
        scope=ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY,
        reason="Observe ERP staff access status",
        idempotency_key=delivery_id,
    )
    db_session_adapter.release_read_transaction(db)
    try:
        owner_event = payload.to_owner_event(event_id=delivery_id)
        if owner_event is None:
            current = integration_inbox.get_receipt(db, receipt_id=receipt_id)
            consequence = {
                "event_id": delivery_id,
                "event_type": payload.event_type,
                "applied": False,
                "status": "unmapped",
            }
            integration_inbox.complete_consequence(
                db,
                receipt=current,
                consequence=consequence,
            )
            return ErpStaffAccessReceipt(
                **consequence,
                replayed=False,
            )
        if isinstance(payload, ErpStaffLeaveRestrictionWebhook):
            assert isinstance(owner_event, ErpStaffLeaveRestrictionEvent)
            outcome = erp_staff_access.apply_staff_leave_restriction_event(
                db,
                erp_staff_access.ApplyLeaveRestrictionCommand(
                    context=context,
                    event=owner_event,
                    delivery_id=delivery_id,
                ),
            )
        else:
            assert isinstance(owner_event, ErpStaffAccountStatusEvent)
            outcome = erp_staff_access.apply_staff_account_status_event(
                db,
                erp_staff_access.ApplyAccountStatusCommand(
                    context=context,
                    event=owner_event,
                    delivery_id=delivery_id,
                ),
            )
    except DomainError as exc:
        integration_inbox.fail_claimed_consequence(
            db,
            receipt_id=receipt_id,
            error_code=exc.code,
            error_detail=exc.message,
        )
        if exc.code in {
            "auth.erp_staff_access.mapping_not_found",
            "auth.erp_staff_access.mapping_conflict",
        }:
            _audit_rejection(
                db,
                action="auth.erp_staff_access_mapping_failed",
                installation_id=installation_id,
                capability_binding_id=capability_binding_id,
                status_code=409,
                metadata={"error_code": exc.code},
            )
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except Exception as exc:
        integration_inbox.fail_claimed_consequence(
            db,
            receipt_id=receipt_id,
            error_code="erp_staff_access_consequence_failed",
            error_detail=type(exc).__name__,
        )
        raise

    current = integration_inbox.get_receipt(db, receipt_id=receipt_id)
    consequence = {
        "event_id": outcome.event_id,
        "event_type": payload.event_type,
        "applied": outcome.applied,
        "status": outcome.status,
    }
    integration_inbox.complete_consequence(
        db,
        receipt=current,
        consequence=consequence,
    )
    return ErpStaffAccessReceipt(
        event_id=outcome.event_id,
        event_type=payload.event_type,
        applied=outcome.applied,
        replayed=False,
        status=outcome.status,
    )
