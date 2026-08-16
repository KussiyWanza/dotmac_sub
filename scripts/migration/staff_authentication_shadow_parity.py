#!/usr/bin/env python3
"""Report whether staff authentication can be resolved through Party identically.

Shadow evidence for the staff Party read cutover. Read-only, aggregate and
PII-free: the output carries counts and stable reason codes only, so it can be
run against a production-derived restore and pasted into a review unredacted.

Exits non-zero while any blocking cohort remains, so it can gate the cutover
directly. `--report-only` surveys without gating.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from app.db import read_only_snapshot_session
from app.services.staff_authentication_shadow import (
    staff_authentication_parity_report,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero even while blocking cohorts remain",
    )
    args = parser.parse_args(argv)

    with read_only_snapshot_session() as db:
        report = staff_authentication_parity_report(db)

    print(json.dumps(report.as_dict(), sort_keys=True))
    if not report.is_read_cutover_safe and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
