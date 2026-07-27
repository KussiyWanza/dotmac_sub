"""`events.owner_outputs` — the guaranteed owner-output protocol (ADR 0007 §2).

Producer side: :func:`stage_owner_output` stages a versioned owner-output
event inside the producing owner's active ``execute_owner_command``
transaction, through the existing transactional outbox (``EventStore``). The
authoritative transition and its output therefore commit atomically —
ADR 0007 invariant 15. Calling it outside an owner command fails closed.

Consumer side: :func:`consume_owner_output` runs the consumer's effect and
commits its :class:`OwnerOutputReceipt` in the same transaction — ADR 0007
invariant 16. The unique ``(consumer, event_id)`` receipt makes redelivery
harmless: a replay returns the recorded outcome without re-running the
effect. A retryable failure simply propagates (the outbox redelivers); an
explicitly terminal failure is recorded with reviewable evidence via
:func:`record_terminal_failure` and is never converted into a success log.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.owner_output import OwnerOutputReceipt, ReceiptOutcome
from app.services.domain_errors import DomainError
from app.services.events.dispatcher import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext, owner_command_active

OWNER = "events.owner_outputs"

ResultT = TypeVar("ResultT")


class OwnerOutputError(DomainError):
    """Fail-closed owner-output protocol error."""


def _error(suffix: str, message: str, **details: object) -> OwnerOutputError:
    return OwnerOutputError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


@dataclass(frozen=True)
class OwnerOutputEnvelope:
    """Versioned identity every owner output carries (ADR 0007 §6)."""

    event_type: EventType
    producer_owner: str
    source_kind: str
    source_id: UUID
    schema_version: int = 1
    occurred_at: datetime | None = None


def stage_owner_output(
    db: Session,
    envelope: OwnerOutputEnvelope,
    payload: dict[str, Any],
    *,
    context: CommandContext,
    account_id: UUID | None = None,
    subscription_id: UUID | None = None,
    invoice_id: UUID | None = None,
) -> UUID:
    """Stage one versioned owner output inside the active owner command.

    Returns the event id. The event row commits atomically with the owner's
    state change; delivery happens after commit through the durable
    dispatcher. The envelope fields ride in the payload so every consumer can
    validate producer identity and causality before acting.
    """

    if not owner_command_active(db):
        raise _error(
            "output_requires_owner_command",
            "Owner outputs are staged only inside an active owner command.",
            event_type=envelope.event_type.value,
        )
    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "An owner output requires the command's business idempotency key.",
            event_type=envelope.event_type.value,
        )

    occurred_at = envelope.occurred_at or datetime.now(UTC)
    event = emit_event(
        db,
        envelope.event_type,
        {
            **payload,
            "envelope": {
                "schema_version": envelope.schema_version,
                "producer_owner": envelope.producer_owner,
                "source_kind": envelope.source_kind,
                "source_id": str(envelope.source_id),
                "command_id": str(context.command_id),
                "correlation_id": str(context.correlation_id),
                "causation_id": (
                    str(context.causation_id) if context.causation_id else None
                ),
                "idempotency_key": context.idempotency_key,
                "occurred_at": occurred_at.isoformat(),
            },
        },
        actor=context.actor,
        account_id=account_id,
        subscription_id=subscription_id,
        invoice_id=invoice_id,
    )
    return event.event_id


def existing_receipt(
    db: Session, *, consumer: str, event_id: UUID
) -> OwnerOutputReceipt | None:
    """Return the committed receipt for ``(consumer, event_id)``, if any."""

    return db.execute(
        select(OwnerOutputReceipt).where(
            OwnerOutputReceipt.consumer == consumer,
            OwnerOutputReceipt.event_id == event_id,
        )
    ).scalar_one_or_none()


def consume_owner_output(
    db: Session,
    *,
    consumer: str,
    event_id: UUID,
    event_type: str,
    producer_owner: str,
    context: CommandContext,
    operation: Callable[[], ResultT],
    schema_version: int = 1,
) -> tuple[ResultT | None, OwnerOutputReceipt]:
    """Apply one consumer effect exactly once for one owner output.

    Runs inside the consumer's active owner command so the receipt and the
    business effect commit atomically. A replay — redelivery of an event this
    consumer already receipted — returns ``(None, receipt)`` without running
    the effect again. A raised exception leaves no receipt: the delivery stays
    durably pending/retrying in the outbox, never silently completed.
    """

    if not owner_command_active(db):
        raise _error(
            "receipt_requires_owner_command",
            "Consumer receipts are committed only inside an owner command.",
            consumer=consumer,
        )
    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "A consumer effect requires its business idempotency key.",
            consumer=consumer,
        )

    receipt = existing_receipt(db, consumer=consumer, event_id=event_id)
    if receipt is not None:
        return None, receipt

    result = operation()

    receipt = OwnerOutputReceipt(
        consumer=consumer,
        event_id=event_id,
        event_type=event_type,
        producer_owner=producer_owner,
        schema_version=schema_version,
        outcome=ReceiptOutcome.succeeded,
        effect_idempotency_key=context.idempotency_key,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
        causation_id=context.causation_id,
    )
    db.add(receipt)
    db.flush()
    return result, receipt


def record_terminal_failure(
    db: Session,
    *,
    consumer: str,
    event_id: UUID,
    event_type: str,
    producer_owner: str,
    context: CommandContext,
    failure_reason: str,
    schema_version: int = 1,
) -> OwnerOutputReceipt:
    """Record an explicitly reviewed terminal failure for one delivery.

    Terminal means a human or policy decided retrying cannot succeed. The
    evidence is durable and queryable; it is not a success and it is not a
    silent abandonment.
    """

    if not owner_command_active(db):
        raise _error(
            "receipt_requires_owner_command",
            "Terminal failures are recorded only inside an owner command.",
            consumer=consumer,
        )
    if not failure_reason.strip():
        raise _error(
            "missing_failure_reason",
            "A terminal failure requires reviewable failure evidence.",
            consumer=consumer,
        )

    existing = existing_receipt(db, consumer=consumer, event_id=event_id)
    if existing is not None:
        raise _error(
            "receipt_already_recorded",
            "This consumer already committed an outcome for the event.",
            consumer=consumer,
            event_id=str(event_id),
            outcome=existing.outcome.value,
        )

    receipt = OwnerOutputReceipt(
        consumer=consumer,
        event_id=event_id,
        event_type=event_type,
        producer_owner=producer_owner,
        schema_version=schema_version,
        outcome=ReceiptOutcome.terminal_failure,
        effect_idempotency_key=context.idempotency_key or str(uuid4()),
        failure_reason=failure_reason,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
        causation_id=context.causation_id,
    )
    db.add(receipt)
    db.flush()
    return receipt


__all__ = [
    "OwnerOutputEnvelope",
    "OwnerOutputError",
    "ReceiptOutcome",
    "consume_owner_output",
    "existing_receipt",
    "record_terminal_failure",
    "stage_owner_output",
]
