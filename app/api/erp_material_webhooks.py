from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.erp_material_webhook import (
    ErpMaterialStatusReceipt,
    ErpMaterialStatusWebhook,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.field import material_requests
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.backoffice_contracts import (
    ERP_MATERIAL_STATUS_WEBHOOK_CAPABILITY,
)
from app.services.integrations.runtime_execution import (
    RuntimeExecutionError,
    build_execution_context,
)
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/webhooks/erp-material", tags=["erp-material-webhook"])
MAX_BODY_BYTES = 128 * 1024


@router.post("/{capability_binding_id}", response_model=ErpMaterialStatusReceipt)
async def receive_erp_material_status(
    capability_binding_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> ErpMaterialStatusReceipt:
    try:
        execution = build_execution_context(
            db, capability_binding_id=capability_binding_id
        )
    except RuntimeExecutionError as exc:
        raise HTTPException(
            status_code=503, detail="ERP material webhook is unavailable"
        ) from exc
    if execution.binding.capability_id != ERP_MATERIAL_STATUS_WEBHOOK_CAPABILITY:
        raise HTTPException(
            status_code=404, detail="ERP material webhook binding not found"
        )
    installation_id = execution.binding.installation_id
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413, detail="ERP material webhook payload is too large"
        )
    presented = str(request.headers.get("X-Dotmac-Signature") or "")
    delivery_id = str(request.headers.get("X-Dotmac-Delivery") or "").strip()
    if not delivery_id:
        raise HTTPException(status_code=400, detail="ERP delivery id is required")
    secret = execution.secret_material["webhook_signing_secret"]
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid ERP webhook signature")
    try:
        payload = ErpMaterialStatusWebhook.model_validate_json(raw)
    except (ValueError, ValidationError):
        raise HTTPException(
            status_code=422, detail="Invalid ERP material status payload"
        ) from None
    receipt, should_process = integration_inbox.receive_and_claim_verified(
        db,
        capability_binding_id=capability_binding_id,
        provider_event_id=delivery_id,
        event_type=ERP_MATERIAL_STATUS_WEBHOOK_CAPABILITY,
        payload=payload.model_dump(mode="json", exclude_none=True),
        headers={"content-type": str(request.headers.get("content-type") or "")},
    )
    if not should_process:
        consequence = receipt.consequence_json or {}
        return ErpMaterialStatusReceipt(
            material_request_id=UUID(str(consequence["material_request_id"])),
            status=str(consequence["status"]),
            replayed=True,
        )
    receipt_id = receipt.id
    command_id = uuid5(
        NAMESPACE_URL,
        f"erp-material-status:{capability_binding_id}:{delivery_id}",
    )
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = material_requests.observe_erp_material_status(
            db,
            material_requests.ObserveErpMaterialStatus(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=f"integration:{installation_id}",
                    scope=ERP_MATERIAL_STATUS_WEBHOOK_CAPABILITY,
                    reason="Observe ERP material request status",
                    idempotency_key=delivery_id,
                ),
                request_id=payload.omni_id,
                provider_request_id=payload.request_number or payload.request_id,
                provider_status=payload.new_status,
                observed_at=payload.updated_at or datetime.now(UTC),
                serial_numbers_by_sequence=tuple(
                    (line.sequence, line.serial_numbers) for line in payload.items
                ),
            ),
        )
    except Exception as exc:
        integration_inbox.fail_claimed_consequence(
            db,
            receipt_id=receipt_id,
            error_code="erp_material_status_consequence_failed",
            error_detail=type(exc).__name__,
        )
        raise
    current = integration_inbox.get_receipt(db, receipt_id=receipt_id)
    integration_inbox.complete_consequence(
        db,
        receipt=current,
        consequence={
            "material_request_id": str(outcome.id),
            "status": outcome.status.value,
        },
    )
    return ErpMaterialStatusReceipt(
        material_request_id=outcome.id, status=outcome.status.value, replayed=False
    )
