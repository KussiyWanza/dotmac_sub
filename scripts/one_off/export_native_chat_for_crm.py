"""Export Selfcare-only native chat history for the reviewed CRM importer.

The output contains private support content. Write it only to an operator-owned
temporary path, transfer it over the approved SSH channel, and remove it after
the import and verification complete. The command prints counts and a digest,
never message bodies or customer identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxMessage,
    InboxMessageDirection,
)

SCHEMA = "dotmac_sub.native_chat_history.v1"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def build_export(db: Session) -> dict[str, Any]:
    conversations = (
        db.query(InboxConversation)
        .filter(InboxConversation.channel_type == InboxChannelType.chat_widget.value)
        .filter(InboxConversation.is_active.is_(True))
        .order_by(InboxConversation.created_at.asc(), InboxConversation.id.asc())
        .all()
    )
    rows: list[dict[str, Any]] = []
    for conversation in conversations:
        messages = (
            db.query(InboxMessage)
            .filter(InboxMessage.conversation_id == conversation.id)
            .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
            .order_by(InboxMessage.created_at.asc(), InboxMessage.id.asc())
            .all()
        )
        if not messages:
            continue
        if conversation.subscriber_id is None:
            raise RuntimeError(
                "Populated native chat has no subscriber identity; import aborted."
            )
        rows.append(
            {
                "source_conversation_id": str(conversation.id),
                "source_subscriber_id": str(conversation.subscriber_id),
                "subject": conversation.subject,
                "created_at": _iso(conversation.created_at),
                "first_message_at": _iso(conversation.first_message_at),
                "last_message_at": _iso(conversation.last_message_at),
                "metadata": {
                    key: value
                    for key, value in dict(conversation.metadata_ or {}).items()
                    if key
                    in {
                        "surface",
                        "ticket_id",
                        "project_id",
                        "source",
                    }
                },
                "messages": [
                    {
                        "source_message_id": str(message.id),
                        "client_message_id": str(
                            (message.metadata_ or {}).get("client_message_id") or ""
                        )
                        or None,
                        "body": message.body or "",
                        "received_at": _iso(message.received_at)
                        or _iso(message.created_at),
                        "created_at": _iso(message.created_at),
                    }
                    for message in messages
                ],
            }
        )
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema": SCHEMA,
        "exported_at": datetime.now(UTC).isoformat(),
        "content_sha256": hashlib.sha256(canonical).hexdigest(),
        "conversation_count": len(rows),
        "message_count": sum(len(row["messages"]) for row in rows),
        "conversations": rows,
    }


def write_private_export(output: Path, payload: dict[str, Any]) -> None:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing export: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        payload = build_export(db)
    write_private_export(args.output, payload)
    print(
        json.dumps(
            {
                "status": "exported",
                "conversation_count": payload["conversation_count"],
                "message_count": payload["message_count"],
                "content_sha256": payload["content_sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
