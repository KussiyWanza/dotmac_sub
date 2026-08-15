#!/usr/bin/env python3
"""Read-only shadow-drift gate for the composable service-team facts.

Reports the five legacy-scalar shadow counters from
``service_team_composition.inspect_shadow_drift``. Exit code 0 means every
counter is zero (the legacy scalar columns may contract); exit code 2 means at
least one team or membership still lacks its composed fact.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy.exc import SQLAlchemyError

from app.db import read_only_snapshot_session
from app.services.domain_errors import DomainError
from app.services.service_team_composition import inspect_shadow_drift


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Read only; exit 2 while any shadow-drift counter is nonzero. "
            "This is the only mode; the flag exists for runbook symmetry."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _parser().parse_args(argv)
    try:
        with read_only_snapshot_session() as db:
            drift = inspect_shadow_drift(db)
        print(
            json.dumps(
                {**asdict(drift), "blocker_count": drift.blocker_count},
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if drift.blocker_count == 0 else 2
    except (DomainError, SQLAlchemyError) as exc:
        code = exc.code if isinstance(exc, DomainError) else "database_error"
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": code,
                    "message": "service-team shadow-drift inspection failed",
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
