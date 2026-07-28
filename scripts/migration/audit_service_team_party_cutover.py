#!/usr/bin/env python3
"""Report aggregate migration-426 service-team identity readiness.

The command emits counts only: no UUID, name, email, binding reason, or CRM
identifier.  PostgreSQL execution is repeatable-read and read-only.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.db_session_adapter import db_session_adapter
from app.services.service_team_party_cutover import (
    audit_service_team_party_cutover,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero when migration 426 is not ready.",
    )
    args = parser.parse_args()
    try:
        with db_session_adapter.read_session() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
            audit = audit_service_team_party_cutover(db)
    except SQLAlchemyError:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "message": "database readiness audit failed",
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(audit.summary(), indent=2, sort_keys=True))
    return 0 if not args.check or audit.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
