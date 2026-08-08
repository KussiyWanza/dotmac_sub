"""Import OLT hardware topology from archived running configs. Idempotent.

The fleet already archives running configs (``olt_config_backups``), so the
common case needs no device I/O: the configs on disk state which frames, slots
and PON ports exist, and that is exactly what ``pon_port_identity`` needs to
derive chassis identity.

Reads the newest archived config per OLT, parses it with
``network.olt_topology_parse``, and hands the reading to
``network.olt_topology_import``. Nothing is inferred here; this is transport and
reporting around the owner.

Run ``--dry-run`` first. Establishing identity activates the partial unique
indexes for those rows, so an import is also the moment two rows claiming one
position would collide, and that collision is a finding rather than an error to
retry past.

Coverage is bounded by what has been archived. Configs list a PON port only if
an ONT was added to it, so an idle port is real hardware this cannot see, and an
OLT with no archived config contributes nothing. Both are reported rather than
silently skipped: an import that quietly covers half the estate reads like one
that covered all of it.

Exit codes: ``0`` when every OLT processed without conflicts, ``1`` when any
conflict was found or any requested OLT had no usable config.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import OLTDevice
from app.services.network.olt_topology_import import import_topology
from app.services.network.olt_topology_parse import parse_running_config

DEFAULT_ROOT = Path("/root/dotmac_sub/uploads/olt_config_backups")


def _newest_config(root: Path, olt_id: str) -> Path | None:
    directory = root / str(olt_id)
    if not directory.is_dir():
        return None
    configs = sorted(
        (p for p in directory.glob("*.txt") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return configs[0] if configs else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="commit nothing")
    parser.add_argument("--olt", default=None, help="restrict to one OLT id")
    parser.add_argument(
        "--config-root",
        default=str(DEFAULT_ROOT),
        help="directory holding <olt_id>/<config>.txt archives",
    )
    args = parser.parse_args()
    root = Path(args.config_root)

    exit_code = 0
    db = SessionLocal()
    try:
        stmt = select(OLTDevice).where(OLTDevice.is_active.is_(True))
        if args.olt:
            stmt = stmt.where(OLTDevice.id == args.olt)
        olts = db.scalars(stmt).all()

        totals = {"shelves": 0, "cards": 0, "ports": 0, "linked": 0, "identities": 0}
        without_config: list[str] = []

        for olt in olts:
            config = _newest_config(root, str(olt.id))
            if config is None:
                without_config.append(olt.name)
                continue

            reading = parse_running_config(
                config.read_text(encoding="utf-8", errors="replace")
            )
            if not reading.interfaces:
                without_config.append(f"{olt.name} (config states no gpon interfaces)")
                continue

            outcome = import_topology(db, olt, reading)
            totals["shelves"] += outcome.shelves_created
            totals["cards"] += outcome.cards_created
            totals["ports"] += outcome.ports_created
            totals["linked"] += outcome.pon_rows_linked
            totals["identities"] += outcome.identities_established

            print(
                f"{outcome.olt_name:22s} "
                f"shelves+{outcome.shelves_created} cards+{outcome.cards_created} "
                f"ports+{outcome.ports_created} linked={outcome.pon_rows_linked} "
                f"identities={outcome.identities_established}  "
                f"[{config.name}]"
            )
            for position in outcome.unmatched_positions:
                print(f"    no PON row named {position}")
            for conflict in outcome.conflicts:
                exit_code = 1
                print(
                    f"    CONFLICT {conflict.pon_port_name!r} "
                    f"({conflict.pon_port_id}): {conflict.detail}",
                    file=sys.stderr,
                )

        print(
            f"\ntotals: shelves+{totals['shelves']} cards+{totals['cards']} "
            f"ports+{totals['ports']} pon_rows_linked={totals['linked']} "
            f"identities_established={totals['identities']}"
        )
        if without_config:
            exit_code = 1
            print("\nno usable archived config (contributed nothing):")
            for name in without_config:
                print(f"  {name}")

        if args.dry_run:
            db.rollback()
            print("\nDRY RUN — nothing committed.")
        else:
            db.commit()
            print("\nCommitted.")
        return exit_code
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
