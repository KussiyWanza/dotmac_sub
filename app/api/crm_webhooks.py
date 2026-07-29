"""Verified DotMac CRM event ingress through the canonical Integration Inbox."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.integration_platform import IntegrationInbox
from app.services import quotes_mirror
from app.services.crm_customers import CRMCustomerObservation, observe_customer
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.crm_capability import inbound_secret_material

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/crm", tags=["crm-webhook"])

SIGNATURE_HEADER = "X-Webhook-Signature-256"
EVENT_HEADER = "X-Webhook-Event"
DELIVERY_HEADER = "X-Webhook-Delivery-Id"

TICKET_EVENTS = {"ticket.created", "ticket.resolved", "ticket.escalated"}
CUSTOMER_EVENTS = {"customer.accepted"}
CHAT_EVENTS = {"message.outbound"}
QUOTE_EVENTS = {
    "quote.created",
    "quote.updated",
    "quote.accepted",
    "quote.rejected",
}


class CrmTicketObservationNotReady(RuntimeError):
    """The ticket event is valid but its executable capability is unavailable."""

    def __init__(self, issue_codes: tuple[str, ...]) -> None:
        super().__init__("CRM ticket observation capability is not ready")
        self.issue_codes = issue_codes


def _verify_signature(raw_body: bytes, presented: str | None, secret: str) -> None:
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CRM webhook signature verification is not configured.",
        )
    expected = (
        "sha256="
        + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing CRM webhook signature.",
        )


async def _receive_verified(
    request: Request,
    db: Session,
    *,
    default_event: str,
) -> tuple[str, dict[str, Any], IntegrationInbox, bool]:
    try:
        binding, material = inbound_secret_material(db)
    except Exception as exc:
        logger.error("crm_inbound_capability_unavailable type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CRM inbound integration is not enabled.",
        ) from exc

    raw_body = await request.body()
    _verify_signature(
        raw_body,
        request.headers.get(SIGNATURE_HEADER),
        str(material.get("webhook_signing_secret") or ""),
    )
    try:
        decoded = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from None
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        )
    payload = dict(decoded)
    event_type = str(request.headers.get(EVENT_HEADER) or default_event).strip()
    provider_event_id = str(request.headers.get(DELIVERY_HEADER) or "").strip()
    if not provider_event_id:
        provider_event_id = f"{event_type}:{hashlib.sha256(raw_body).hexdigest()}"
    receipt, should_process = integration_inbox.receive_and_claim_verified(
        db,
        capability_binding_id=binding.id,
        provider_event_id=provider_event_id,
        event_type=event_type,
        payload=payload,
        headers={
            key: value
            for key, value in {
                "content-type": request.headers.get("content-type"),
                "user-agent": request.headers.get("user-agent"),
            }.items()
            if value
        },
    )
    return event_type, payload, receipt, should_process


def _body(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else payload


def _complete(
    db: Session,
    receipt: IntegrationInbox,
    consequence: dict[str, Any],
) -> dict[str, Any]:
    return integration_inbox.complete_consequence(
        db,
        receipt=receipt,
        consequence=consequence,
    )


def _failed(
    db: Session,
    receipt: IntegrationInbox,
    exc: Exception,
    *,
    error_code: str = "crm_consequence_failed",
    error_detail: str | None = None,
) -> None:
    integration_inbox.fail_consequence(
        db,
        receipt=receipt,
        error_code=error_code,
        error_detail=error_detail or type(exc).__name__,
    )


def _existing(receipt: IntegrationInbox, should_process: bool) -> dict[str, Any] | None:
    if should_process:
        return None
    return dict(receipt.consequence_json or {})


@router.post("/customers")
async def receive_crm_customer(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    event_type, payload, receipt, should_process = await _receive_verified(
        request, db, default_event="customer.accepted"
    )
    prior = _existing(receipt, should_process)
    if prior is not None:
        return prior
    try:
        if event_type not in CUSTOMER_EVENTS:
            return _complete(db, receipt, {"status": "ignored", "event": event_type})
        observation = CRMCustomerObservation.from_payload(payload)
        consequence = observe_customer(db, observation).as_consequence()
        return _complete(db, receipt, consequence)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
            integration_inbox.fail_consequence(
                db,
                receipt=receipt,
                error_code="crm_customer_name_rejected",
                error_detail="Customer name is missing or invalid.",
                consequence={
                    "status": "rejected",
                    "error_code": "crm_customer_name_rejected",
                    "name_disposition": "rejected",
                },
            )
        else:
            _failed(db, receipt, exc)
        raise
    except Exception as exc:
        _failed(db, receipt, exc)
        raise


@router.post("")
async def receive_crm_event(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    event_type, payload, receipt, should_process = await _receive_verified(
        request, db, default_event="unknown"
    )
    prior = _existing(receipt, should_process)
    if prior is not None:
        return prior
    try:
        if event_type not in TICKET_EVENTS:
            return _complete(db, receipt, {"status": "ignored", "event": event_type})
        from app.services import control_registry

        if not control_registry.is_enabled(db, "crm.ticket_pull"):
            return _complete(
                db,
                receipt,
                {
                    "status": "ignored",
                    "reason": "ticket_observation_disabled",
                    "event": event_type,
                },
            )
        from app.services.integrations.crm_ticket_readiness import (
            resolve_crm_ticket_pull_readiness,
        )

        readiness = resolve_crm_ticket_pull_readiness(db, control_enabled=True)
        if not readiness.ready:
            raise CrmTicketObservationNotReady(readiness.issue_codes)
        ticket_id = str(payload.get("ticket_id") or "").strip()
        if not ticket_id:
            return _complete(
                db,
                receipt,
                {
                    "status": "ignored",
                    "reason": "ticket_id_missing",
                    "event": event_type,
                },
            )
        from app.services.queue_adapter import enqueue_task
        from app.tasks.crm_ticket_pull import sync_crm_ticket

        enqueue_task(
            sync_crm_ticket,
            args=[ticket_id],
            correlation_id=f"crm_inbox:{receipt.id}",
            source="integration_inbox",
        )
        return _complete(
            db,
            receipt,
            {"status": "queued", "event": event_type, "ticket_id": ticket_id},
        )
    except Exception as exc:
        if isinstance(exc, CrmTicketObservationNotReady):
            integration_inbox.fail_claimed_consequence(
                db,
                receipt_id=receipt.id,
                error_code="crm_ticket_observation_not_ready",
                error_detail=",".join(exc.issue_codes),
            )
        else:
            _failed(db, receipt, exc)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to apply CRM ticket observation.",
        ) from exc


@router.post("/chat")
async def receive_crm_chat_event(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    event_type, payload, receipt, should_process = await _receive_verified(
        request, db, default_event="message.outbound"
    )
    prior = _existing(receipt, should_process)
    if prior is not None:
        return prior
    try:
        if event_type not in CHAT_EVENTS:
            return _complete(db, receipt, {"status": "ignored", "event": event_type})
        body = _body(payload)
        from app.services import push as push_service

        preview = str(body.get("preview") or "").strip() or "You have a new message."
        conversation_id = str(body.get("conversation_id") or "")

        def wake(subscriber_id: str) -> None:
            push_service.send_push(
                db,
                subscriber_id,
                title="New message from support",
                body=preview,
                data={"type": "chat_message", "conversation_id": conversation_id},
            )

        subscriber_id = str(body.get("subscriber_id") or "").strip()
        if subscriber_id:
            wake(subscriber_id)
            return _complete(db, receipt, {"status": "ok", "event": event_type})
        reseller_id = str(body.get("reseller_id") or "").strip()
        if reseller_id:
            from app.services import reseller_portal

            subscriber_ids = reseller_portal.portal_user_subscriber_ids(db, reseller_id)
            for target_id in subscriber_ids:
                wake(target_id)
            return _complete(
                db,
                receipt,
                {
                    "status": "ok" if subscriber_ids else "ignored",
                    "event": event_type,
                },
            )
        return _complete(
            db,
            receipt,
            {"status": "ignored", "reason": "no_target", "event": event_type},
        )
    except Exception as exc:
        _failed(db, receipt, exc)
        raise


async def _receive_mirror_event(
    request: Request,
    db: Session,
    *,
    allowed_events: set[str],
    default_event: str,
    consequence_owner: Callable[[Session, str, dict[str, Any]], dict[str, Any]],
    control_key: str | None = None,
) -> dict[str, Any]:
    event_type, payload, receipt, should_process = await _receive_verified(
        request, db, default_event=default_event
    )
    prior = _existing(receipt, should_process)
    if prior is not None:
        return prior
    try:
        if event_type not in allowed_events:
            return _complete(db, receipt, {"status": "ignored", "event": event_type})
        if control_key:
            from app.services import control_registry

            if not control_registry.is_enabled(db, control_key):
                return _complete(
                    db,
                    receipt,
                    {
                        "status": "ignored",
                        "reason": "observation_disabled",
                        "event": event_type,
                    },
                )
        consequence = consequence_owner(db, event_type, _body(payload))
        return _complete(db, receipt, consequence)
    except Exception as exc:
        _failed(db, receipt, exc)
        raise


@router.post("/quotes")
async def receive_crm_quote_event(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return await _receive_mirror_event(
        request,
        db,
        allowed_events=QUOTE_EVENTS,
        default_event="quote.updated",
        consequence_owner=quotes_mirror.apply_webhook,
    )
