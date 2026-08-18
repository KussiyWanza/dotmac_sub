"""Bounded retention owner for detailed field-technician GPS history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.field_location import FieldTechLocationPing
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

LOCATION_HISTORY_RETENTION_DAYS = 30
MAX_PRUNE_BATCH_SIZE = 10_000
_OWNER = "operations.field_location_retention"
_CONCERN = "detailed field-location history retention"


class FieldLocationRetentionError(DomainError):
    """Stable rejection from the field-location retention boundary."""


@dataclass(frozen=True, slots=True)
class PruneFieldLocationHistoryCommand:
    context: CommandContext
    as_of: datetime
    batch_size: int = MAX_PRUNE_BATCH_SIZE


@dataclass(frozen=True, slots=True)
class PruneFieldLocationHistoryOutcome:
    command_id: UUID
    cutoff: datetime
    deleted_count: int
    batch_limit_reached: bool


_DEFINITION = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="prune_field_location_history",
)


def _error(suffix: str, message: str, **details: object) -> FieldLocationRetentionError:
    return FieldLocationRetentionError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def prune_field_location_history(
    db: Session,
    command: PruneFieldLocationHistoryCommand,
) -> PruneFieldLocationHistoryOutcome:
    """Delete one locked batch of GPS pings beyond the approved retention."""

    def _operation() -> PruneFieldLocationHistoryOutcome:
        if command.batch_size < 1 or command.batch_size > MAX_PRUNE_BATCH_SIZE:
            raise _error(
                "invalid_batch_size",
                f"batch_size must be between 1 and {MAX_PRUNE_BATCH_SIZE}",
                batch_size=command.batch_size,
            )
        as_of = command.as_of
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)
        else:
            as_of = as_of.astimezone(UTC)
        cutoff = as_of - timedelta(days=LOCATION_HISTORY_RETENTION_DAYS)
        ping_ids = tuple(
            db.scalars(
                select(FieldTechLocationPing.id)
                .where(FieldTechLocationPing.received_at < cutoff)
                .order_by(
                    FieldTechLocationPing.received_at.asc(),
                    FieldTechLocationPing.id.asc(),
                )
                .limit(command.batch_size)
                .with_for_update(skip_locked=True)
            ).all()
        )
        if ping_ids:
            db.execute(
                delete(FieldTechLocationPing).where(
                    FieldTechLocationPing.id.in_(ping_ids)
                )
            )
            audit_service.audit_events.stage(
                db,
                AuditEventCreate(
                    actor_type=AuditActorType.service,
                    actor_id=command.context.actor,
                    action="field_location_history_pruned",
                    entity_type="field_location_retention",
                    entity_id=cutoff.date().isoformat(),
                    status_code=200,
                    is_success=True,
                    metadata_={
                        "retention_days": LOCATION_HISTORY_RETENTION_DAYS,
                        "cutoff": cutoff.isoformat(),
                        "deleted_count": len(ping_ids),
                        "command_id": str(command.context.command_id),
                    },
                ),
            )
            emit_event(
                db,
                EventType.field_location_history_pruned,
                {
                    "retention_days": LOCATION_HISTORY_RETENTION_DAYS,
                    "cutoff": cutoff.isoformat(),
                    "deleted_count": len(ping_ids),
                },
                actor=command.context.actor,
            )
        db.flush()
        return PruneFieldLocationHistoryOutcome(
            command_id=command.context.command_id,
            cutoff=cutoff,
            deleted_count=len(ping_ids),
            batch_limit_reached=len(ping_ids) == command.batch_size,
        )

    return execute_owner_command(
        db,
        definition=_DEFINITION,
        context=command.context,
        operation=_operation,
    )
