"""Print the complete exact staged-geometry field-verification map overlay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import read_only_snapshot_session  # noqa: E402
from app.services.network.fiber_topology_field_map import (  # noqa: E402
    FiberTopologyFieldMapError,
    project_fiber_field_verification_map,
)
from app.services.network.fiber_topology_field_worklist import (  # noqa: E402
    FiberTopologyFieldWorklistError,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Project every latest staged fiber source feature as exact GeoJSON "
            "with owner-produced field-evidence priority. This command is "
            "read-only: it cannot repair or snap geometry, infer identity or "
            "endpoints, create jobs or observations, mutate topology, or claim "
            "cutover readiness."
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
            report = project_fiber_field_verification_map(db)
    except (FiberTopologyFieldMapError, FiberTopologyFieldWorklistError) as exc:
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
