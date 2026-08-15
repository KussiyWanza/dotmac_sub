"""Print the exhaustive, read-only ONT assignment constraint cutover audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import read_only_snapshot_session  # noqa: E402
from app.services.network.ont_assignment_cutover import (  # noqa: E402
    audit_ont_assignment_cutover,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit every active ONT assignment against the future constraint "
            "cutover gates. This command never repairs data or enables constraints."
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
    with read_only_snapshot_session() as db:
        report = audit_ont_assignment_cutover(db)
    print(
        json.dumps(
            report.to_dict(),
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if report.ready_for_constraints else 2


if __name__ == "__main__":
    raise SystemExit(main())
