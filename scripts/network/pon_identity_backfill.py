"""Establish stored structural identity on PON ports. Idempotent.

Migration ``487_pon_structural_identity`` adds the columns and the two partial
unique indexes but deliberately writes no data: resolving identity needs the
platform shape and the identity owner's refusals, which is application logic,
not SQL.

This is that step. It asks ``network.pon_port_identity`` to resolve each row
from its sources — hardware topology on a chassis OLT, the port name on a
single-box one — and writes the answer to ``identity_frame``/``identity_slot``/
``identity_port``. Rows it cannot resolve are left untouched, not cleared: a
transient inability to resolve must never erase an identity already proven.

Because the resolution is the owner's and the write is idempotent, running this
twice is a no-op and running it after hardware topology is corrected repairs the
affected rows.

``--dry-run`` reports exactly what would be written and commits nothing. Run it
first: the partial unique indexes are live, so a backfill is also the moment two
rows claiming one identity would collide, and that collision is a finding rather
than an error to retry past.

Exit codes: ``0`` when every row either resolved or was already correct, ``1``
when any row could not be resolved or a collision was detected.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models.network import OLTDevice, PonPort
from app.services.network.pon_port_identity import (
    canonical_name,
    materialize_identity,
    stored_identity,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change and commit nothing",
    )
    parser.add_argument(
        "--olt",
        default=None,
        help="restrict to one OLT id, for a cautious first pass",
    )
    args = parser.parse_args()

    counts: Counter[str] = Counter()
    unresolved: list[str] = []

    db = SessionLocal()
    try:
        stmt = select(PonPort)
        if args.olt:
            stmt = stmt.where(PonPort.olt_id == args.olt)
        ports = db.scalars(stmt).all()

        for pon in ports:
            before = stored_identity(pon)
            identity = materialize_identity(db, pon)

            if identity is None:
                counts["unresolved"] += 1
                olt = db.get(OLTDevice, pon.olt_id)
                unresolved.append(
                    f"  {pon.id}  olt={getattr(olt, 'name', '?')!r}  name={pon.name!r}"
                )
            elif before is None:
                counts["established"] += 1
            elif before == identity:
                counts["already_correct"] += 1
            else:
                counts["corrected"] += 1
                print(f"  CORRECTED {pon.id} {before} -> {canonical_name(identity)}")

        print(f"\npon_ports examined: {len(ports)}")
        for key in ("established", "corrected", "already_correct", "unresolved"):
            print(f"  {key:16s} {counts[key]}")

        if unresolved:
            print("\nunresolved rows (left untouched):")
            for line in unresolved[:50]:
                print(line)
            if len(unresolved) > 50:
                print(f"  ... and {len(unresolved) - 50} more")

        if args.dry_run:
            db.rollback()
            print("\nDRY RUN — nothing committed.")
        else:
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                print(
                    "\nCOLLISION — two rows claim one structural identity. "
                    "That is a finding, not a retryable error: resolve the "
                    "duplicate through the identity owner before backfilling.\n"
                    f"{exc.orig}",
                    file=sys.stderr,
                )
                return 1
            print("\nCommitted.")

        return 1 if counts["unresolved"] else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
