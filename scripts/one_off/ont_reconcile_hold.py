#!/usr/bin/env python3
"""Operator adapter for per-ONT reconciliation holds.

Thin by design: it parses arguments, builds a typed command context, and calls
``network.ont_reconcile_eligibility``. Every decision -- whether a hold may be
placed, whether a reviewer is acceptable, whether a release is legal -- belongs
to the owner, not here.

This exists because the rollout commands were otherwise unreachable outside
tests: an owner nobody can invoke is an owner that will not be used, and the
fleet-wide hold would stay on by default.

Usage::

    # who is held right now
    python scripts/one_off/ont_reconcile_hold.py list

    # holds past their review date (these stay ACTIVE -- they alert, not expire)
    python scripts/one_off/ont_reconcile_hold.py overdue

    # place a reviewed hold
    python scripts/one_off/ont_reconcile_hold.py place \\
        --serial 4857544328201B9A \\
        --reason-code wan_intent_adjudication \\
        --explanation "Unverified WAN intent; management drift not adjudicated." \\
        --actor michael@dotmac --reviewer ops@dotmac \\
        --review-due-at 2026-08-11T10:00:00+00:00 \\
        --idempotency-key hold-cohort-001

    # release it
    python scripts/one_off/ont_reconcile_hold.py release \\
        --hold-id <uuid> --actor michael@dotmac \\
        --reason "adjudicated; safe to converge"

``--reviewer`` must differ from ``--actor``: suppressing convergence on a
customer device is a two-person decision. ``--idempotency-key`` is mandatory so
a retried invocation cannot create a second decision.

``--review-due-at`` is an absolute, timezone-aware instant, and that is the
whole point. The owner treats a replay as a replay only when the ENTIRE command
matches, review date included. An earlier version of this adapter took a
relative ``--review-in-days`` and resolved it against ``now()`` on every
invocation, so re-running a byte-identical command produced a review date that
differed by microseconds -- and mandatory idempotency could therefore never
replay through its own supported CLI. It refused instead, which was safe but
made the documented retry story a fiction. A naive timestamp is rejected rather
than assumed to be UTC: silently reinterpreting an operator's local time would
move the review date by hours.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import SessionLocal  # noqa: E402
from app.models.network import OntUnit  # noqa: E402
from app.services.domain_errors import DomainError  # noqa: E402
from app.services.network.ont_reconcile_eligibility import (  # noqa: E402
    HoldSpec,
    held_ont_ids,
    overdue_holds,
    place_reconcile_hold,
    reconcile_eligibility,
    release_reconcile_hold,
)
from app.services.owner_commands import CommandContext  # noqa: E402


def parse_review_due_at(value: str) -> datetime:
    """Parse an absolute, timezone-aware review date at the CLI boundary.

    Converting here rather than deeper means a bad value is reported next to
    the flag that carried it. Naive input is refused: the owner would coerce it
    to UTC, silently shifting an operator's local time by their offset.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO-8601 timestamp "
            f"(expected e.g. 2026-08-11T10:00:00+00:00)"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} has no timezone offset; pass an absolute instant "
            f"(e.g. 2026-08-11T10:00:00+00:00 or ...Z)"
        )
    return parsed


def _resolve_ont(db, serial: str) -> OntUnit:
    ont = db.query(OntUnit).filter(OntUnit.serial_number == serial).one_or_none()
    if ont is None:
        raise SystemExit(f"No ONT with serial {serial!r}")
    return ont


def _cmd_list(args) -> int:
    with SessionLocal() as db:
        held = held_ont_ids(db)
        if not held:
            print("No active reconciliation holds.")
            return 0
        print(f"Active holds: {len(held)}\n")
        for ont_id in held:
            ont = db.get(OntUnit, ont_id)
            verdict = reconcile_eligibility(db, ont_id)
            print(
                f"  {getattr(ont, 'serial_number', '?'):<20} "
                f"hold={verdict.hold_id} reason={verdict.reason_code} "
                f"review_due={verdict.review_due_at} "
                f"{'OVERDUE' if verdict.overdue else ''}"
            )
    return 0


def _cmd_overdue(args) -> int:
    with SessionLocal() as db:
        rows = overdue_holds(db)
        if not rows:
            print("No overdue holds.")
            return 0
        print(
            f"OVERDUE holds (still active -- they alert, they do not expire): {len(rows)}\n"
        )
        for hold in rows:
            ont = db.get(OntUnit, hold.ont_unit_id)
            print(
                f"  {getattr(ont, 'serial_number', '?'):<20} hold={hold.id} "
                f"due={hold.review_due_at} actor={hold.actor} "
                f"reviewer={hold.reviewer} reason={hold.reason_code}"
            )
    return 1  # non-zero so a cron/CI caller notices


def _cmd_place(args) -> int:
    with SessionLocal() as db:
        ont = _resolve_ont(db, args.serial)
        spec = HoldSpec(
            ont_unit_id=ont.id,
            reason_code=args.reason_code,
            explanation=args.explanation,
            reviewer=args.reviewer,
            review_due_at=args.review_due_at,
        )
        context = CommandContext.system(
            actor=args.actor,
            scope=f"ont:{ont.id}",
            reason=args.explanation,
            idempotency_key=args.idempotency_key,
        )
        db.commit()  # the owner requires a transaction-free session at entry
        try:
            hold = place_reconcile_hold(db, spec=spec, context=context)
        except DomainError as exc:
            print(f"REFUSED [{exc.code}] {exc}")
            return 2
        db.commit()
        print(f"hold placed: {hold.id} (review due {hold.review_due_at})")
    return 0


def _cmd_release(args) -> int:
    # Parse at the boundary: the owner's typed command takes a UUID, and
    # handing it a string would fail deep inside rather than here where the
    # operator can see it.
    try:
        hold_id = UUID(str(args.hold_id))
    except ValueError:
        print(f"REFUSED [invalid_hold_id] {args.hold_id!r} is not a UUID")
        return 2

    with SessionLocal() as db:
        context = CommandContext.system(
            actor=args.actor, scope=f"hold:{hold_id}", reason=args.reason
        )
        db.commit()
        try:
            hold = release_reconcile_hold(db, hold_id=hold_id, context=context)
        except DomainError as exc:
            print(f"REFUSED [{exc.code}] {exc}")
            return 2
        db.commit()
        print(f"hold released: {hold.id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the operator parser.

    Separate from ``main`` so the argument contract -- absolute review date,
    mandatory idempotency key, no relative flag -- can be asserted without
    reaching a database.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show active holds")
    sub.add_parser("overdue", help="show holds past their review date")

    place = sub.add_parser("place", help="place a reviewed hold")
    place.add_argument("--serial", required=True)
    place.add_argument("--reason-code", required=True)
    place.add_argument("--explanation", required=True)
    place.add_argument("--actor", required=True)
    place.add_argument("--reviewer", required=True, help="must differ from --actor")
    place.add_argument(
        "--review-due-at",
        required=True,
        type=parse_review_due_at,
        metavar="ISO8601",
        help=(
            "absolute, timezone-aware review date (e.g. 2026-08-11T10:00:00+00:00). "
            "Must be identical on a retry for the idempotency key to replay."
        ),
    )
    place.add_argument(
        "--idempotency-key",
        required=True,
        help="mandatory: a retry must not create a second decision",
    )

    release = sub.add_parser("release", help="release a hold")
    release.add_argument("--hold-id", required=True)
    release.add_argument("--actor", required=True)
    release.add_argument("--reason", required=True)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return {
        "list": _cmd_list,
        "overdue": _cmd_overdue,
        "place": _cmd_place,
        "release": _cmd_release,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
