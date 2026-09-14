#!/usr/bin/env python3
"""Inspect or execute one reviewed quarantined Party identity reactivation."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.db import read_only_snapshot_session
from app.models.party import Party, PartyType
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.party_identity_reactivation import (
    COMMAND_SCOPE,
    PartyReactivationDecisionSource,
    ReactivateQuarantinedPartyCommand,
    reactivate_quarantined_party,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--party-id", type=UUID, required=True)
    parser.add_argument(
        "--expected-party-type", choices=tuple(PartyType), type=PartyType
    )
    parser.add_argument("--expected-updated-at", type=datetime.fromisoformat)
    parser.add_argument("--approved-by-user-id", type=UUID)
    parser.add_argument("--reviewed-at", type=datetime.fromisoformat)
    parser.add_argument("--reason")
    parser.add_argument("--command-id", type=UUID)
    return parser


def _execute_requirements(args: argparse.Namespace) -> None:
    missing = tuple(
        name
        for name in (
            "expected_party_type",
            "expected_updated_at",
            "approved_by_user_id",
            "reviewed_at",
            "reason",
            "command_id",
        )
        if getattr(args, name) is None
    )
    if missing:
        _parser().error(
            "--execute requires "
            + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        )


def main() -> int:
    args = _parser().parse_args()
    if args.check:
        with read_only_snapshot_session() as db:
            party = db.scalar(select(Party).where(Party.id == args.party_id))
            payload = (
                {"status": "not_found", "party_id": str(args.party_id)}
                if party is None
                else {
                    "status": "observed",
                    "party_id": str(party.id),
                    "party_type": party.party_type,
                    "party_status": party.status,
                    "updated_at": party.updated_at.isoformat(),
                }
            )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if party is not None else 2

    _execute_requirements(args)
    command_id: UUID = args.command_id
    approved_by: UUID = args.approved_by_user_id
    try:
        with db_session_adapter.owner_command_session() as db:
            outcome = reactivate_quarantined_party(
                db,
                ReactivateQuarantinedPartyCommand(
                    context=CommandContext(
                        command_id=command_id,
                        correlation_id=command_id,
                        actor=f"user:{approved_by}",
                        scope=COMMAND_SCOPE,
                        reason="Reviewed canonical Party identity reactivation",
                        idempotency_key=f"party-reactivation:{command_id}",
                    ),
                    party_id=args.party_id,
                    expected_party_type=args.expected_party_type,
                    expected_updated_at=args.expected_updated_at,
                    reviewed_by_user_id=approved_by,
                    reviewed_at=args.reviewed_at,
                    decision_source=PartyReactivationDecisionSource.administrative_review,
                    review_reason=args.reason,
                ),
            )
        print(
            json.dumps(
                {
                    "status": "replayed" if outcome.replayed else "reactivated",
                    "party_id": str(outcome.party_id),
                    "previous_status": outcome.previous_status.value,
                    "current_status": outcome.current_status.value,
                    "updated_at": outcome.updated_at.isoformat(),
                    "command_id": str(outcome.command_id),
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
                    "message": "Party identity reactivation failed",
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
