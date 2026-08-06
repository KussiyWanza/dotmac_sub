#!/usr/bin/env python3
"""Operator adapter for reviewed ONT automatic-sweep cohort admissions.

The fleet-wide ``network.ont_reconcile`` control is an emergency stop, not
positive authority to walk every device. This adapter makes the eligibility
owner's reviewed, named, expiring admission commands reachable to operators.

Examples::

    python scripts/one_off/ont_reconcile_admission.py list

    python scripts/one_off/ont_reconcile_admission.py admit \
        --serial 4857544328201B9A --cohort-key cohort-1-verified \
        --reason-code initial_verified_rollout \
        --explanation "Canonical PON identity and sentinel review passed." \
        --actor michael@dotmac --reviewer network-lead@dotmac \
        --expires-at 2026-08-12T10:00:00+01:00 \
        --idempotency-key admit-cohort-1-4857544328201B9A

    python scripts/one_off/ont_reconcile_admission.py revoke \
        --admission-id <uuid> --actor michael@dotmac \
        --reason "cohort paused after acceptance signal"
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
    AdmissionSpec,
    admit_reconcile_cohort_member,
    effective_admissions,
    revoke_reconcile_admission,
)
from app.services.owner_commands import CommandContext  # noqa: E402


def parse_expires_at(value: str) -> datetime:
    """Parse one replayable, timezone-aware absolute expiry."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO-8601 timestamp "
            f"(expected e.g. 2026-08-12T10:00:00+01:00)"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} has no timezone offset; pass an absolute instant"
        )
    return parsed


def _resolve_ont(db, serial: str) -> OntUnit:
    ont = db.query(OntUnit).filter(OntUnit.serial_number == serial).one_or_none()
    if ont is None:
        raise SystemExit(f"No ONT with serial {serial!r}")
    return ont


def _cmd_list(_args) -> int:
    with SessionLocal() as db:
        rows = effective_admissions(db)
        if not rows:
            print("No effective automatic-reconcile admissions.")
            return 0
        print(f"Effective admissions: {len(rows)}\n")
        for admission in rows:
            ont = db.get(OntUnit, admission.ont_unit_id)
            print(
                f"  {getattr(ont, 'serial_number', '?'):<20} "
                f"admission={admission.id} cohort={admission.cohort_key} "
                f"expires={admission.expires_at} reviewer={admission.reviewer}"
            )
    return 0


def _cmd_admit(args) -> int:
    with SessionLocal() as db:
        ont = _resolve_ont(db, args.serial)
        spec = AdmissionSpec(
            ont_unit_id=ont.id,
            cohort_key=args.cohort_key,
            reason_code=args.reason_code,
            explanation=args.explanation,
            reviewer=args.reviewer,
            expires_at=args.expires_at,
        )
        context = CommandContext.system(
            actor=args.actor,
            scope=f"ont:{ont.id}",
            reason=args.explanation,
            idempotency_key=args.idempotency_key,
        )
        db.commit()
        try:
            admission = admit_reconcile_cohort_member(db, spec=spec, context=context)
        except DomainError as exc:
            print(f"REFUSED [{exc.code}] {exc}")
            return 2
        print(
            f"admitted: {admission.id} cohort={admission.cohort_key} "
            f"expires={admission.expires_at}"
        )
    return 0


def _cmd_revoke(args) -> int:
    try:
        admission_id = UUID(str(args.admission_id))
    except ValueError:
        print(f"REFUSED [invalid_admission_id] {args.admission_id!r} is not a UUID")
        return 2

    with SessionLocal() as db:
        context = CommandContext.system(
            actor=args.actor,
            scope=f"reconcile-admission:{admission_id}",
            reason=args.reason,
        )
        try:
            admission = revoke_reconcile_admission(
                db, admission_id=admission_id, context=context
            )
        except DomainError as exc:
            print(f"REFUSED [{exc.code}] {exc}")
            return 2
        print(f"revoked: {admission.id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show effective admissions")

    admit = sub.add_parser("admit", help="admit one ONT to a named cohort")
    admit.add_argument("--serial", required=True)
    admit.add_argument("--cohort-key", required=True)
    admit.add_argument("--reason-code", required=True)
    admit.add_argument("--explanation", required=True)
    admit.add_argument("--actor", required=True)
    admit.add_argument("--reviewer", required=True, help="must differ from --actor")
    admit.add_argument(
        "--expires-at",
        required=True,
        type=parse_expires_at,
        metavar="ISO8601",
        help="absolute, timezone-aware authority expiry; identical on replay",
    )
    admit.add_argument("--idempotency-key", required=True)

    revoke = sub.add_parser("revoke", help="remove authority before expiry")
    revoke.add_argument("--admission-id", required=True)
    revoke.add_argument("--actor", required=True)
    revoke.add_argument("--reason", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return {
        "list": _cmd_list,
        "admit": _cmd_admit,
        "revoke": _cmd_revoke,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
