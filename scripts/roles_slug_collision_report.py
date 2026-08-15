#!/usr/bin/env python3
"""Report which role names would contend for one kernel slug.

Run this against a production-derived snapshot BEFORE relying on the kernel
role identity for anything. `auth.rbac_catalog` fails closed on a collision —
`uq_roles_tenant_slug` rejects the second role — so an unreviewed collision
turns an ordinary role edit into a `catalog_conflict` at the worst moment.

Read-only, deterministic, and safe to run repeatedly: the output is sorted JSON,
so two runs over one population are byte-identical and two snapshots diff
cleanly.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select

from app.db import read_only_snapshot_session
from app.models.rbac import Role
from app.services.rbac_catalog import role_slug_collision_report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero even when the report finds collisions",
    )
    args = parser.parse_args(argv)

    with read_only_snapshot_session() as db:
        rows: tuple[tuple[UUID, str], ...] = tuple(
            (role_id, name)
            for role_id, name in db.execute(select(Role.id, Role.name)).all()
        )

    report = role_slug_collision_report(rows)
    print(json.dumps(report.as_dict(), sort_keys=True))
    if report.blocking and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
