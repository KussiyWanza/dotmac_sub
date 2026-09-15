"""Authoritative verification of exact synthetic Team Inbox health probes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.team_inbox import InboxMessage
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.team_inbox_smtp_inbound import SMTP_PROBE_HEADER_VALUE

OWNER = "communications.team_inbox_health"
SMTP_PROBE_VERIFIED_KEY = "smtp_probe_verified"
SMTP_PROBE_ID_KEY = "smtp_probe_id"
_VERIFY_PROBE = OwnerCommandDefinition(
    owner=OWNER,
    concern="verified SMTP probe delivery projection",
    name="verify_team_inbox_smtp_probe_delivery",
)


def verify_smtp_probe_delivery(
    db: Session,
    *,
    probe_id: str,
) -> dict[str, str] | None:
    """Verify and mark only the exact provider-stable ID from the runtime."""

    def operation() -> dict[str, str] | None:
        rows = (
            db.query(InboxMessage)
            .filter(InboxMessage.metadata_[SMTP_PROBE_ID_KEY].as_string() == probe_id)
            .with_for_update()
            .limit(2)
            .all()
        )
        row = rows[0] if len(rows) == 1 else None
        metadata = dict(row.metadata_ or {}) if row is not None else {}
        if row is None or metadata.get("smtp_probe") != SMTP_PROBE_HEADER_VALUE:
            return None
        if metadata.get(SMTP_PROBE_VERIFIED_KEY) is not True:
            metadata[SMTP_PROBE_VERIFIED_KEY] = True
            metadata["smtp_probe_verified_at"] = datetime.now(UTC).isoformat()
            row.metadata_ = metadata
            db.flush()
        return {
            "message_id": str(row.id),
            "conversation_id": str(row.conversation_id),
            "external_message_id": str(row.external_message_id or ""),
        }

    return execute_owner_command(
        db,
        definition=_VERIFY_PROBE,
        context=CommandContext.system(
            actor="system:team-inbox-smtp-probe",
            scope="team-inbox:health",
            reason="verify exact synthetic SMTP delivery",
            idempotency_key=probe_id,
        ),
        operation=operation,
    )
