#!/usr/bin/env python3
"""Preview or repair dropped support-comment StoredFile references."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from uuid import UUID, uuid4

from app.db import SessionLocal
from app.services.owner_commands import CommandContext
from app.services.support import (
    TicketCommentAttachmentRepairCommand,
    repair_ticket_comment_attachment_references,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ticket-id",
        action="append",
        required=True,
        type=UUID,
        help="Exact affected Ticket UUID; repeat for up to 100 Tickets",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply exact unambiguous repairs; default is read-only preview",
    )
    parser.add_argument("--actor", help="Operator identity required with --apply")
    parser.add_argument("--reason", help="Reviewed reason required with --apply")
    parser.add_argument(
        "--idempotency-key",
        help="Stable operator key required with --apply",
    )
    args = parser.parse_args()
    if args.apply and not all((args.actor, args.reason, args.idempotency_key)):
        parser.error("--apply requires --actor, --reason, and --idempotency-key")

    command_id = uuid4()
    context = CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=args.actor or "ticket-attachment-repair-preview",
        scope="support.ticket:comment_attachment_reference_repair",
        reason=args.reason or "preview dropped comment attachment references",
        idempotency_key=args.idempotency_key,
    )
    with SessionLocal() as db:
        outcome = repair_ticket_comment_attachment_references(
            db,
            TicketCommentAttachmentRepairCommand(
                context=context,
                ticket_ids=tuple(args.ticket_id),
                apply=args.apply,
            ),
        )
    print(json.dumps(asdict(outcome), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
