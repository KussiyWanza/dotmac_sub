#!/usr/bin/env python3
"""Report aggregate Party-to-support customer lifecycle convergence."""

from __future__ import annotations

import json

from app.db import read_only_snapshot_session
from app.services.customer_lifecycle_audit import build_customer_lifecycle_audit


def main() -> int:
    with read_only_snapshot_session() as db:
        result = build_customer_lifecycle_audit(db)
        db.rollback()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "installed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
