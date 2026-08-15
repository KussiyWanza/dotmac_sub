#!/usr/bin/env python3
"""Report PII-free Person-principal and organization-context convergence."""

from __future__ import annotations

import json

from app.db import read_only_snapshot_session
from app.services.party_principal_audit import build_party_principal_context_audit


def main() -> int:
    with read_only_snapshot_session() as db:
        result = build_party_principal_context_audit(db)
        db.rollback()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "installed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
