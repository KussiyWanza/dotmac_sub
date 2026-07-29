"""Dry-run-first repair for the July 20 CRM name-overwrite incident."""

from __future__ import annotations

import argparse
import json
import sys

from app.db import SessionLocal
from app.services.crm_customer_name_repair import (
    WINDOW_END,
    WINDOW_START,
    apply_name_remediation_plan,
    build_name_remediation_plan,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-target",
        default="",
        help="Required label for apply mode; identifies the approved deployment target.",
    )
    parser.add_argument(
        "--digest",
        default="",
        help="Required exact manifest digest for apply mode.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the approved repair. Default: dry-run only.",
    )
    parser.add_argument(
        "--actor-id",
        default=None,
        help="Optional actor id stamped on the correction audit events.",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        plan = build_name_remediation_plan(
            session,
            deployment_target=args.deployment_target or "dry-run",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        print(json.dumps(plan.manifest, indent=2, sort_keys=True))
        if not args.apply:
            print(
                "DRY-RUN only. Re-run with --apply, --digest, and --deployment-target."
            )
            return 0
        if not args.digest.strip():
            print("REFUSING: --digest is required in apply mode.")
            return 2
        if not args.deployment_target.strip():
            print("REFUSING: --deployment-target is required in apply mode.")
            return 2
        result = apply_name_remediation_plan(
            session,
            plan,
            expected_digest=args.digest.strip(),
            deployment_target=args.deployment_target.strip(),
            actor_id=args.actor_id,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
