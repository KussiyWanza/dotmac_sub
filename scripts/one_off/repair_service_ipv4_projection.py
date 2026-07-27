#!/usr/bin/env python
"""Preview or apply one reviewed exact-service served IPv4 projection repair.

The command changes only ``Subscription.ipv4_address`` through the IPAM owner.
Its durable event projects RADIUS and disconnects only sessions still pinned to
the previous address. The default is dry-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID, uuid4

from app.db import SessionLocal
from app.services.ip_assignment_lifecycle import (
    RepairServiceIPv4ProjectionCommand,
    preview_service_ipv4_projection_repair,
    repair_service_ipv4_projection,
)
from app.services.owner_commands import CommandContext


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription-id", required=True, type=_uuid)
    parser.add_argument("--assignment-id", required=True, type=_uuid)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    return parser


def _required(parser: argparse.ArgumentParser, value: str | None, flag: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        parser.error(f"{flag} is required with --apply")
    return normalized


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    with SessionLocal() as session:
        preview = preview_service_ipv4_projection_repair(
            session,
            subscription_id=args.subscription_id,
            assignment_id=args.assignment_id,
        )
        print(
            json.dumps(
                {
                    "subscription_id": str(preview.subscription_id),
                    "subscriber_id": (
                        str(preview.subscriber_id)
                        if preview.subscriber_id is not None
                        else None
                    ),
                    "assignment_id": (
                        str(preview.assignment_id)
                        if preview.assignment_id is not None
                        else None
                    ),
                    "served_address": preview.served_address,
                    "desired_address": preview.desired_address,
                    "radius_mode": preview.radius_mode,
                    "observed_radius_address": preview.observed_radius_address,
                    "active_session_count": preview.active_session_count,
                    "old_address_session_count": preview.old_address_session_count,
                    "decision": preview.decision.value,
                    "applicable": preview.applicable,
                    "repair_fingerprint": preview.fingerprint,
                },
                indent=2,
                sort_keys=True,
            )
        )
        if not args.apply:
            print("DRY RUN — no changes written.")
            return 0
        if not preview.applicable:
            parser.error(f"current preview is not applicable: {preview.decision.value}")
        fingerprint = _required(parser, args.fingerprint, "--fingerprint")
        if fingerprint != preview.fingerprint:
            parser.error("--fingerprint does not match the current repair preview")
        idempotency_key = _required(parser, args.idempotency_key, "--idempotency-key")
        actor = _required(parser, args.actor, "--actor")
        reason = _required(parser, args.reason, "--reason")
        session.rollback()
        outcome = repair_service_ipv4_projection(
            session,
            RepairServiceIPv4ProjectionCommand(
                context=CommandContext.system(
                    command_id=uuid4(),
                    actor=actor,
                    scope="ip_assignment_projection_repair",
                    reason=reason,
                    idempotency_key=idempotency_key,
                ),
                subscription_id=args.subscription_id,
                assignment_id=args.assignment_id,
                preview_fingerprint=fingerprint,
            ),
        )
        print(
            json.dumps(
                {
                    "subscription_id": str(outcome.subscription_id),
                    "assignment_id": str(outcome.assignment_id),
                    "previous_address": outcome.previous_address,
                    "desired_address": outcome.desired_address,
                    "observed_active_sessions": outcome.observed_active_sessions,
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
