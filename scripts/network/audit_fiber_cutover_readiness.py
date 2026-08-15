"""Print the complete read-only numeric fiber cutover-readiness report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import read_only_snapshot_session  # noqa: E402
from app.services.network.fiber_topology_cutover_readiness import (  # noqa: E402
    FiberTopologyCutoverReadinessError,
    reconcile_fiber_cutover_readiness,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the one versioned numeric policy against the complete "
            "global latest-source and active-fiber cohort. The report is read-only "
            "evidence for independent cutover review; it cannot authorize or "
            "perform a production cutover."
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
            report = reconcile_fiber_cutover_readiness(db)
    except FiberTopologyCutoverReadinessError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            report.to_dict(),
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if report.ready_for_cutover_review else 2


if __name__ == "__main__":
    raise SystemExit(main())
