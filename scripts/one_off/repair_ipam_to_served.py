#!/usr/bin/env python
"""Audit and safely link legacy IPv4 assignments to exact services.

The default is a read-only full-fleet report. Applying requires the exact
preview fingerprint and repairs only high-confidence missing
``IPAssignment.subscription_id`` links. It never changes an address, served IP,
RADIUS row, or session.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import uuid4

from app.db import SessionLocal
from app.services.ip_assignment_lifecycle import (
    IPAssignmentOwnershipDecision,
    ReconcileIPAssignmentOwnershipCommand,
    preview_ip_assignment_service_ownership,
    reconcile_ip_assignment_service_ownership,
)
from app.services.owner_commands import CommandContext


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--fingerprint",
        help="Exact SHA-256 printed by the reviewed dry run.",
    )
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument(
        "--limit",
        type=int,
        help="Bound the repairable cohort before producing/applying its fingerprint.",
    )
    return parser


def _require_apply_argument(
    parser: argparse.ArgumentParser,
    value: str | None,
    flag: str,
) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        parser.error(f"{flag} is required with --apply")
    return normalized


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    with SessionLocal() as session:
        fleet = preview_ip_assignment_service_ownership(session)
        repairable_ids = fleet.repairable_assignment_ids
        if args.limit is not None:
            repairable_ids = repairable_ids[: args.limit]
        selected = preview_ip_assignment_service_ownership(
            session,
            assignment_ids=repairable_ids,
        )
        output = {
            "fleet_fingerprint": fleet.fingerprint,
            "fleet_counts": {
                decision.value: count for decision, count in fleet.counts.items()
            },
            "repairable_count": len(repairable_ids),
            "repair_fingerprint": selected.fingerprint,
            "repair_assignment_ids": [str(value) for value in repairable_ids],
        }
        print(json.dumps(output, indent=2, sort_keys=True))

        blockers = [
            {
                "assignment_id": str(item.assignment_id),
                "decision": item.decision.value,
            }
            for item in fleet.items
            if item.decision
            not in {
                IPAssignmentOwnershipDecision.exact,
                IPAssignmentOwnershipDecision.repairable_missing_service_link,
            }
        ]
        if blockers:
            print(json.dumps({"blocker_sample": blockers[:25]}, indent=2))

        if not args.apply:
            print("DRY RUN — no changes written.")
            return 0

        fingerprint = _require_apply_argument(parser, args.fingerprint, "--fingerprint")
        if fingerprint != selected.fingerprint:
            parser.error(
                "--fingerprint does not match the current selected repair cohort"
            )
        idempotency_key = _require_apply_argument(
            parser, args.idempotency_key, "--idempotency-key"
        )
        actor = _require_apply_argument(parser, args.actor, "--actor")
        reason = _require_apply_argument(parser, args.reason, "--reason")
        # End the read-only preview transaction before entering the public
        # owner-managed command boundary.
        session.rollback()
        context = CommandContext.system(
            command_id=uuid4(),
            actor=actor,
            scope="ipam_service_ownership_reconciliation",
            reason=reason,
            idempotency_key=idempotency_key,
        )
        outcome = reconcile_ip_assignment_service_ownership(
            session,
            ReconcileIPAssignmentOwnershipCommand(
                context=context,
                preview_fingerprint=fingerprint,
                assignment_ids=repairable_ids,
            ),
        )
        print(
            json.dumps(
                {
                    "linked_count": outcome.linked_count,
                    "preview_fingerprint": outcome.preview_fingerprint,
                    "replayed": outcome.replayed,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
