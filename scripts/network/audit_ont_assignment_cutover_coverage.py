"""Print read-only ONT cleanup lineage coverage and readiness evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import read_only_snapshot_session  # noqa: E402
from app.services.network.ont_assignment_cutover_coverage import (  # noqa: E402
    OntAssignmentCutoverCoverageError,
    reconcile_ont_assignment_cutover_coverage,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile current ONT cleanup findings to immutable reviewed and "
            "verified lineage. This read-only command cannot execute repairs or "
            "authorize, create, validate, or enable constraints."
        )
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of indented JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with read_only_snapshot_session() as db:
            report = reconcile_ont_assignment_cutover_coverage(db)
    except OntAssignmentCutoverCoverageError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            report.to_dict(),
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if report.ready_for_constraint_authorization_review else 2


if __name__ == "__main__":
    raise SystemExit(main())
