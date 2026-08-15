"""Print the exhaustive read-only staged fiber field-verification worklist."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import read_only_snapshot_session  # noqa: E402
from app.services.network.fiber_topology_field_worklist import (  # noqa: E402
    FiberTopologyFieldWorklistError,
    reconcile_fiber_field_worklist,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List every latest staged fiber source identity in deterministic "
            "field-evidence priority order. This command is read-only: it cannot "
            "create or assign work orders, record observations, infer identity or "
            "endpoints, generate proposals, mutate topology, or claim cutover "
            "readiness."
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
            report = reconcile_fiber_field_worklist(db)
    except FiberTopologyFieldWorklistError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            report.to_dict(),
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
