#!/usr/bin/env python3
"""Report aggregate-only parity for the kernel audit R1 dual-write."""

from __future__ import annotations

import argparse
import json

from app.db import read_only_snapshot_session
from app.services.audit import audit_events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero even when the aggregate report detects drift",
    )
    args = parser.parse_args()

    with read_only_snapshot_session() as db:
        report = audit_events.r1_parity(db)

    print(json.dumps(report.as_dict(), sort_keys=True))
    if report.blocking_mismatches and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
