"""Print exhaustive read-only staged-path connectivity coverage evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import read_only_snapshot_session  # noqa: E402
from app.services.network.fiber_topology_connectivity_coverage import (  # noqa: E402
    FiberTopologyConnectivityCoverageError,
    reconcile_fiber_connectivity_coverage,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile the complete latest staged fiber-path cohort to exact "
            "reviewed batch, decision, mutation, and canonical provenance evidence. "
            "This command cannot infer endpoints, propose or execute decisions, "
            "approve change requests, mutate topology, or authorize cutover."
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
            report = reconcile_fiber_connectivity_coverage(db)
    except FiberTopologyConnectivityCoverageError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            report.to_dict(),
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if report.ready_for_connectivity_cutover_review else 2


if __name__ == "__main__":
    raise SystemExit(main())
