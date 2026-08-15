#!/usr/bin/env python3
"""Report PII-free Organization/Reseller/Vendor Party convergence counts."""

from __future__ import annotations

import json

from app.db import read_only_snapshot_session
from app.services.party_organization_audit import (
    build_party_organization_profile_audit,
)


def main() -> int:
    with read_only_snapshot_session() as db:
        result = build_party_organization_profile_audit(db)
        db.rollback()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "installed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
