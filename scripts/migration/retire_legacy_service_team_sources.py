#!/usr/bin/env python3
"""Audit or explicitly retire the narrow pre-426 service-team sources."""

from __future__ import annotations

import argparse
import json
from uuid import uuid4

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from app.db import read_only_snapshot_session
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.service_team_source_retirement import (
    RetireLegacyServiceTeamSources,
    audit_legacy_service_team_sources,
    retire_legacy_service_team_sources,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Read only; exit 2 until the five pointers and sources are ready.",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Retire only the reviewed legacy sources and invalid identity rows.",
    )
    parser.add_argument(
        "--actor",
        help="Required with --execute, for example service:release-operator.",
    )
    parser.add_argument(
        "--reason",
        help="Required with --execute; recorded in command evidence.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.execute and (not args.actor or not args.reason):
        _parser().error("--execute requires --actor and --reason")
    try:
        if args.check:
            with read_only_snapshot_session() as db:
                bind = db.get_bind()
                # This gate exists only to protect migration 426's one-time
                # cutover. Once the composable schema from 440 is present the
                # cutover has happened, and ordinary later identity drift must
                # never block a deploy behind a destructive retirement tool.
                if inspect(bind).has_table("service_team_capability_definitions"):
                    print(
                        json.dumps(
                            {"ready": True, "skipped": "cutover_complete"},
                            indent=2,
                            sort_keys=True,
                        )
                    )
                    return 0
                audit = audit_legacy_service_team_sources(db)
            print(json.dumps(audit.summary(), indent=2, sort_keys=True))
            return 0 if audit.ready else 2

        command_id = uuid4()
        with db_session_adapter.owner_command_session() as db:
            outcome = retire_legacy_service_team_sources(
                db,
                RetireLegacyServiceTeamSources(
                    context=CommandContext(
                        command_id=command_id,
                        correlation_id=command_id,
                        actor=str(args.actor),
                        scope="operations:service_team:retire",
                        reason=str(args.reason),
                        idempotency_key=f"service-team-source-retirement:{command_id}",
                    )
                ),
            )
        print(
            json.dumps(
                {
                    "status": "replayed" if outcome.replayed else "retired",
                    "retired_source_count": outcome.retired_source_count,
                    "cleared_manager_pointer_count": (
                        outcome.cleared_manager_pointer_count
                    ),
                    "removed_migration_blocking_membership_count": (
                        outcome.removed_migration_blocking_membership_count
                    ),
                    "reactivated_native_membership_count": (
                        outcome.reactivated_native_membership_count
                    ),
                    "abandoned_legacy_member_entry_count": (
                        outcome.abandoned_legacy_member_entry_count
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (DomainError, SQLAlchemyError) as exc:
        code = exc.code if isinstance(exc, DomainError) else "database_error"
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": code,
                    "message": "service-team source-retirement operation failed",
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
