"""Signed fiber.dotmac.ng inquiry ingress for Team Inbox."""

from __future__ import annotations

import hashlib
import hmac
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.fiber_inquiry import FiberInquiryReceipt, FiberInquiryRequest
from app.services import team_inbox_fiber_receive, team_inbox_observations
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.connectors.fiber_inquiry_http import (
    FIBER_INQUIRY_CAPABILITY,
)
from app.services.integrations.runtime_execution import (
    RuntimeExecutionError,
    build_execution_context,
)

router = APIRouter(prefix="/webhooks/fiber-inquiry", tags=["fiber-inquiry-webhook"])
logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024


@router.post("/{capability_binding_id}", response_model=FiberInquiryReceipt)
async def receive_fiber_inquiry(
    capability_binding_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> FiberInquiryReceipt:
    try:
        context = build_execution_context(
            db,
            capability_binding_id=capability_binding_id,
        )
    except RuntimeExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fiber inquiry integration is unavailable",
        ) from exc
    if context.binding.capability_id != FIBER_INQUIRY_CAPABILITY:
        raise HTTPException(status_code=404, detail="Fiber inquiry binding not found")

    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413, detail="Fiber inquiry payload is too large"
        )
    signature_header = str(context.config["signature_header"])
    delivery_header = str(context.config["delivery_id_header"])
    signature_prefix = str(context.config["signature_prefix"])
    site_id = str(context.config["site_id"])
    secret = context.secret_material["webhook_signing_secret"]
    installation_id = context.binding.installation_id
    expected = (
        signature_prefix
        + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )
    presented = request.headers.get(signature_header)
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    delivery_id = str(request.headers.get(delivery_header) or "").strip()
    if not delivery_id:
        raise HTTPException(status_code=400, detail="Webhook delivery id is required")
    try:
        payload = FiberInquiryRequest.model_validate_json(raw_body)
    except (ValueError, ValidationError):
        raise HTTPException(
            status_code=422, detail="Invalid fiber inquiry payload"
        ) from None

    try:
        receipt, should_process = integration_inbox.receive_and_claim_verified(
            db,
            capability_binding_id=capability_binding_id,
            provider_event_id=delivery_id,
            event_type=FIBER_INQUIRY_CAPABILITY,
            payload=payload.model_dump(mode="json", exclude_none=True),
            headers={"content-type": str(request.headers.get("content-type") or "")},
        )
    except integration_inbox.ProviderEventIdentityCollision as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "fiber_inquiry_receipt_conflict", "message": str(exc)},
        ) from exc
    except integration_inbox.InboxError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "fiber_inquiry_receipt_conflict", "message": str(exc)},
        ) from exc
    if not should_process:
        consequence = receipt.consequence_json or {}
        try:
            return FiberInquiryReceipt(
                observation_id=UUID(str(consequence["observation_id"])),
                conversation_id=UUID(str(consequence["conversation_id"])),
                message_id=UUID(str(consequence["message_id"])),
                replayed=True,
                resolution_status=str(
                    consequence.get("resolution_status") or "unmatched"
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Fiber inquiry receipt has incomplete consequence evidence",
            ) from exc
    receipt_id = receipt.id
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = team_inbox_fiber_receive.receive_fiber_inquiry_committed(
            db,
            team_inbox_fiber_receive.FiberInquiryIngressCommand(
                delivery_id=delivery_id,
                site_id=site_id,
                payload=payload,
                actor=f"integration:{installation_id}",
                integration_inbox_id=receipt_id,
            ),
        )
    except team_inbox_observations.TeamInboxObservationError as exc:
        integration_inbox.fail_claimed_consequence(
            db,
            receipt_id=receipt_id,
            error_code=exc.code,
            error_detail=str(exc),
        )
        status_code = (
            status.HTTP_409_CONFLICT
            if exc.code.endswith("provider_event_identity_collision")
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception(
            "fiber_inquiry_processing_failed type=%s delivery_id=%s",
            type(exc).__name__,
            delivery_id,
        )
        integration_inbox.fail_claimed_consequence(
            db,
            receipt_id=receipt_id,
            error_code="fiber_inquiry_processing_failed",
            error_detail=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Fiber inquiry processing failed",
        ) from exc
    current_receipt = integration_inbox.get_receipt(db, receipt_id=receipt_id)
    integration_inbox.complete_consequence(
        db,
        receipt=current_receipt,
        consequence={
            "observation_id": str(outcome.observation_id),
            "conversation_id": str(outcome.conversation_id),
            "message_id": str(outcome.message_id),
            "resolution_status": outcome.resolution_status,
        },
    )
    return FiberInquiryReceipt(
        observation_id=outcome.observation_id,
        conversation_id=outcome.conversation_id,
        message_id=outcome.message_id,
        replayed=outcome.replayed,
        resolution_status=outcome.resolution_status,
    )
