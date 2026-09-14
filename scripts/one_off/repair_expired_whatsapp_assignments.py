"""Report or release historical expired WhatsApp active assignments.

Dry-run is the default. Use ``--apply`` only after reviewing the counts and
with explicit production approval.
"""

from __future__ import annotations

import argparse
import json

from app.services import team_inbox_maintenance
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Release stale assignments. Omitted means read-only dry-run.",
    )
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    with db_session_adapter.owner_command_session() as session:
        result = team_inbox_maintenance.repair_expired_whatsapp_assignments(
            session,
            team_inbox_maintenance.RepairExpiredWhatsAppAssignmentsCommand(
                context=CommandContext.system(
                    actor="operator:expired-whatsapp-assignment-repair",
                    scope="team-inbox:maintenance",
                    reason=(
                        "apply reviewed expired WhatsApp assignment repair"
                        if args.apply
                        else "preview expired WhatsApp assignment repair"
                    ),
                ),
                dry_run=not args.apply,
                limit=args.limit,
            ),
        )
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry-run",
                "examined": result.examined,
                "stale_assignments_found": result.stale_assignments_found,
                "stale_queues_found": result.stale_queues_found,
                "assignments_released": result.assignments_released,
                "queues_cancelled": result.queues_cancelled,
                "already_correct": result.already_correct,
                "conflicts": result.conflicts,
                "errors": result.errors,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
