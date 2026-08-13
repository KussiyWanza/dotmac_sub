#!/usr/bin/env python3
"""Report aggregate-only parity for the kernel audit R1 dual-write."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import text

from app.db import SessionLocal
from app.services.audit import audit_events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero even when the aggregate report detects drift",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        report = audit_events.r1_parity(db)
        db.rollback()

    print(json.dumps(report.as_dict(), sort_keys=True))
    if report.blocking_mismatches and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
