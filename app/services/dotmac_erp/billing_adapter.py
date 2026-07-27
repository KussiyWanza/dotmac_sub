"""`integration.dotmac_erp_billing_adapter` (ADR 0007 Phase 7, section 11).

A transport, not a decision system. It maps committed Sub owner outputs into
versioned, idempotent ERP payloads and keeps durable delivery and
acknowledgement evidence. It owns no billing transition:

- staging happens as a flush-only participant inside the owner or consumer
  command that committed the source fact, so the export row and the fact
  commit atomically;
- delivery and acknowledgement are their own owner commands with durable
  attempts — ERP downtime leaves exports pending and never rolls back Sub
  cash, documents, entitlement, or access;
- a rejection is durable reviewable evidence; chart-of-account and TaxCode
  mapping stay in ERP, which fails closed on anything missing or ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.erp_billing_export import (
    ErpBillingExport,
    ErpBillingFlow,
    ErpExportStatus,
)
from app.services.domain_errors import DomainError
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)

OWNER = "integration.dotmac_erp_billing_adapter"

_DELIVERY_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="durable ERP delivery and acknowledgement evidence",
    name="record_erp_delivery_outcome",
)

# Fields every payload must carry; an incomplete payload is never exported
# for ERP to guess at.
_REQUIRED_PAYLOAD_FIELDS = ("document_kind", "account_id", "currency", "lines")


class ErpBillingAdapterError(DomainError):
    """Fail-closed ERP billing adapter error."""


def _error(suffix: str, message: str, **details: object) -> ErpBillingAdapterError:
    return ErpBillingAdapterError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


@dataclass(frozen=True)
class StageErpExportCommand:
    """Typed request to stage one versioned ERP payload."""

    flow: ErpBillingFlow
    source_kind: str
    source_id: UUID
    payload: dict[str, Any]
    payload_version: int = 1
    source_event_id: UUID | None = None


def stage_export(
    db: Session,
    command: StageErpExportCommand,
    *,
    context: CommandContext,
) -> ErpBillingExport:
    """Stage one export inside the committing owner's transaction.

    Flush-only participant: the export row commits atomically with the source
    fact. One row per business idempotency key; a replay returns the original.
    """

    if not owner_command_active(db):
        raise _error(
            "export_requires_owner_command",
            "ERP exports are staged only inside an active owner command.",
        )
    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "An ERP export requires the command's business idempotency key.",
        )
    missing = [
        field
        for field in _REQUIRED_PAYLOAD_FIELDS
        if field not in command.payload or command.payload[field] in (None, "")
    ]
    if missing:
        raise _error(
            "incomplete_export_payload",
            "An ERP payload must carry its complete required fields.",
            missing_fields=missing,
            flow=command.flow.value,
        )

    existing = db.execute(
        select(ErpBillingExport).where(
            ErpBillingExport.idempotency_key == context.idempotency_key
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    export = ErpBillingExport(
        flow=command.flow,
        source_kind=command.source_kind,
        source_id=command.source_id,
        source_event_id=command.source_event_id,
        payload_version=command.payload_version,
        payload=command.payload,
        idempotency_key=context.idempotency_key,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )
    db.add(export)
    db.flush()
    return export


def pending_exports(db: Session, *, limit: int = 100) -> list[ErpBillingExport]:
    """Read-only: the bounded batch of undelivered exports, oldest first."""

    return list(
        db.execute(
            select(ErpBillingExport)
            .where(ErpBillingExport.status == ErpExportStatus.pending)
            .order_by(ErpBillingExport.created_at)
            .limit(limit)
        ).scalars()
    )


def record_delivery_outcome(
    db: Session,
    *,
    export_id: UUID,
    outcome: ErpExportStatus,
    context: CommandContext,
    occurred_at: datetime,
    erp_reference: str | None = None,
    error: str | None = None,
) -> ErpExportStatus:
    """Record one durable delivery/acknowledgement/rejection outcome.

    ERP acceptance requires ERP's own reference so the source and ERP
    identities are structurally linked. A rejection requires reviewable error
    evidence. Replaying a recorded terminal outcome is a no-op; conflicting
    outcomes fail closed.
    """

    if occurred_at.tzinfo is None:
        raise _error(
            "invalid_outcome_instant",
            "Delivery outcomes require a timezone-aware instant.",
        )
    if outcome is ErpExportStatus.pending:
        raise _error(
            "invalid_outcome",
            "Pending is the initial state, not a recordable outcome.",
        )
    return execute_owner_command(
        db,
        definition=_DELIVERY_COMMAND,
        context=context,
        operation=lambda: _record_outcome(
            db,
            export_id=export_id,
            outcome=outcome,
            occurred_at=occurred_at,
            erp_reference=erp_reference,
            error=error,
        ),
    )


def _record_outcome(
    db: Session,
    *,
    export_id: UUID,
    outcome: ErpExportStatus,
    occurred_at: datetime,
    erp_reference: str | None,
    error: str | None,
) -> ErpExportStatus:
    export = lock_for_update(db, ErpBillingExport, export_id)
    if export is None:
        raise _error(
            "export_not_found",
            "Delivery outcome requires an existing export.",
            export_id=str(export_id),
        )

    if export.status is outcome:
        return export.status
    if export.status in (ErpExportStatus.acknowledged, ErpExportStatus.rejected):
        raise _error(
            "conflicting_export_outcome",
            "A terminal export outcome cannot be replaced.",
            recorded=export.status.value,
            attempted=outcome.value,
        )

    if outcome is ErpExportStatus.delivered:
        export.attempts += 1
        export.delivered_at = occurred_at
    elif outcome is ErpExportStatus.acknowledged:
        if not erp_reference or not erp_reference.strip():
            raise _error(
                "missing_erp_reference",
                "ERP acknowledgement requires ERP's document reference.",
            )
        export.acknowledged_at = occurred_at
        export.erp_reference = erp_reference
    else:  # rejected
        if not error or not error.strip():
            raise _error(
                "missing_rejection_evidence",
                "An ERP rejection requires reviewable error evidence.",
            )
        export.last_error = error

    export.status = outcome
    db.flush()
    return export.status


__all__ = [
    "ErpBillingAdapterError",
    "ErpBillingFlow",
    "ErpExportStatus",
    "StageErpExportCommand",
    "pending_exports",
    "record_delivery_outcome",
    "stage_export",
]
