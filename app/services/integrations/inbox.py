"""Canonical verified inbound receipt and consequence lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationInbox,
)
from app.services.integrations.delivery import payload_digest
from app.services.integrations.installations import quarantine_installation


class InboxError(ValueError):
    """Raised when an inbound receipt violates identity or lifecycle rules."""


class ProviderEventIdentityCollision(InboxError):
    """A provider reused one event identity for different payload bytes."""


CommandResultT = TypeVar("CommandResultT")


def execute_command(
    db: Session,
    command: Callable[[], CommandResultT],
) -> CommandResultT:
    """Complete one inbox-owned unit of work."""

    try:
        result = command()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def get_receipt(db: Session, *, receipt_id: UUID) -> IntegrationInbox:
    receipt = db.get(IntegrationInbox, receipt_id)
    if receipt is None:
        raise InboxError("integration inbox receipt not found")
    return receipt


def list_receipts(
    db: Session,
    *,
    state: str | None = None,
    capability_binding_id: UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[IntegrationInbox]:
    query = select(IntegrationInbox)
    if state:
        normalized_state = state.strip().lower()
        if normalized_state not in {
            "verified",
            "processing",
            "processed",
            "retryable",
            "dead_letter",
        }:
            raise InboxError("invalid integration inbox state")
        query = query.where(IntegrationInbox.state == normalized_state)
    if capability_binding_id:
        query = query.where(
            IntegrationInbox.capability_binding_id == capability_binding_id
        )
    return list(
        db.scalars(
            query.order_by(IntegrationInbox.received_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )


def receive_verified(
    db: Session,
    *,
    capability_binding_id: UUID,
    provider_event_id: str,
    event_type: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> tuple[IntegrationInbox, bool]:
    binding = db.get(IntegrationCapabilityBinding, capability_binding_id)
    if binding is None:
        raise InboxError("capability binding not found")
    normalized_event_id = provider_event_id.strip()
    if not normalized_event_id:
        raise InboxError("provider event id is required")
    digest = payload_digest(payload)
    existing = (
        db.query(IntegrationInbox)
        .filter(
            IntegrationInbox.capability_binding_id == binding.id,
            IntegrationInbox.provider_event_id == normalized_event_id,
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.payload_digest != digest:
            quarantine_installation(
                db,
                installation_id=binding.installation_id,
                reason="provider_event_identity_collision",
                actor="integration.inbox",
            )
            raise ProviderEventIdentityCollision("provider event identity collision")
        return existing, False
    receipt = IntegrationInbox(
        installation_id=binding.installation_id,
        capability_binding_id=binding.id,
        provider_event_id=normalized_event_id,
        event_type=event_type.strip() or "unknown",
        payload_digest=digest,
        headers_json={
            str(key).lower(): str(value) for key, value in (headers or {}).items()
        },
        payload_json=payload,
        state="verified",
        attempt_count=0,
        consequence_json={},
    )
    db.add(receipt)
    db.flush()
    return receipt, True


def receive_and_claim_verified(
    db: Session,
    *,
    capability_binding_id: UUID,
    provider_event_id: str,
    event_type: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> tuple[IntegrationInbox, bool]:
    """Persist a verified fact before any domain consequence runs."""

    try:
        receipt, _created = receive_verified(
            db,
            capability_binding_id=capability_binding_id,
            provider_event_id=provider_event_id,
            event_type=event_type,
            payload=payload,
            headers=headers,
        )
        should_process = claim_for_processing(receipt)
    except ProviderEventIdentityCollision:
        # Quarantine is the authoritative security consequence of an identity
        # collision and must survive the fail-closed rejection.
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise
    db.commit()
    return receipt, should_process


def claim_for_processing(receipt: IntegrationInbox) -> bool:
    if receipt.state == "processed":
        return False
    if (
        receipt.state == "retryable"
        and receipt.error_code == "crm_customer_name_rejected"
    ):
        return False
    if receipt.state == "dead_letter":
        raise InboxError("dead-letter receipt requires authorized replay")
    receipt.state = "processing"
    receipt.attempt_count += 1
    receipt.error_code = None
    receipt.error_detail = None
    return True


def mark_processed(
    receipt: IntegrationInbox, *, consequence: dict[str, Any]
) -> IntegrationInbox:
    receipt.state = "processed"
    receipt.consequence_json = consequence
    receipt.processed_at = datetime.now(UTC)
    receipt.error_code = None
    receipt.error_detail = None
    return receipt


def mark_failed(
    receipt: IntegrationInbox,
    *,
    error_code: str,
    error_detail: str | None = None,
    max_attempts: int = 10,
) -> IntegrationInbox:
    receipt.error_code = error_code[:120]
    receipt.error_detail = (error_detail or "")[:2000] or None
    receipt.state = (
        "dead_letter" if receipt.attempt_count >= max(1, max_attempts) else "retryable"
    )
    return receipt


def complete_consequence(
    db: Session,
    *,
    receipt: IntegrationInbox,
    consequence: dict[str, Any],
) -> dict[str, Any]:
    """Commit one domain consequence with its canonical inbox evidence."""

    return execute_command(
        db,
        lambda: mark_processed(receipt, consequence=consequence).consequence_json,
    )


def fail_consequence(
    db: Session,
    *,
    receipt: IntegrationInbox,
    error_code: str,
    error_detail: str | None = None,
    consequence: dict[str, Any] | None = None,
    max_attempts: int = 10,
) -> None:
    """Discard partial consequence writes, then record retry evidence."""

    receipt_id = receipt.id
    db.rollback()

    def operation() -> None:
        current = get_receipt(db, receipt_id=receipt_id)
        mark_failed(
            current,
            error_code=error_code,
            error_detail=error_detail,
            max_attempts=max_attempts,
        )
        if consequence is not None:
            current.consequence_json = consequence

    execute_command(db, operation)


def fail_claimed_consequence(
    db: Session,
    *,
    receipt_id: UUID,
    error_code: str,
    error_detail: str | None = None,
    max_attempts: int = 10,
) -> None:
    """Record failure after a separate consequence owner already rolled back."""

    def operation() -> None:
        receipt = get_receipt(db, receipt_id=receipt_id)
        mark_failed(
            receipt,
            error_code=error_code,
            error_detail=error_detail,
            max_attempts=max_attempts,
        )

    execute_command(db, operation)


def replay_receipt(db: Session, *, receipt_id: UUID) -> IntegrationInbox:
    receipt = get_receipt(db, receipt_id=receipt_id)
    if receipt.state not in {"retryable", "dead_letter"}:
        raise InboxError("integration inbox receipt is not replayable")
    receipt.state = "verified"
    receipt.error_code = None
    receipt.error_detail = None
    db.flush()
    return receipt
