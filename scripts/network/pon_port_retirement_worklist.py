"""Which PON rows describe hardware that does not exist, and what points at them.

Read-only. Produces the worklist that must exist before any row is retired; it
decides nothing and writes nothing.

Sub holds more PON rows than the devices have ports. Measured on production:
381 Huawei rows against 142 ports the OLTs actually declare, so 284 rows name a
frame/slot/port the hardware does not have -- Jabi carries 64 rows for a single
16-port board, many naming an empty slot 0/2.

A row is only called *unbacked* when the device evidence is good enough to say
so. That means the OLT has an archived running config, that config declares at
least one ``interface gpon`` block, and the row's own name is canonical enough
to state a position. Anything short of that is reported as *unknown* rather than
counted against the row: absence of evidence is not evidence the port is absent,
and a retirement list that quietly folds the two together is how real hardware
gets deleted.

References are counted across **every** table that points at a PON port, not
just assignments. "No active assignment" does not prove "no references" -- a row
can carry signal history, topology evidence, fibre attachments or a reviewed
identity decision long after its last customer left.

The output splits three ways:

* ``retirable`` -- unbacked by the device and referenced by nothing.
* ``review`` -- unbacked but something still points at it. Each is listed with
  what points at it, because these are individual decisions.
* ``unknown`` -- no usable device evidence. Not retirable on this evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.network import OLTDevice, PonPort
from app.services.network.olt_topology_parse import parse_running_config
from app.services.network.pon_port_identity import (
    PonIdentityShape,
    PonPortIdentity,
    read_name,
    shape_for_vendor,
)
from scripts.network.pon_port_identity_census import REFERENCES

DEFAULT_ROOT = Path("/root/dotmac_sub/uploads/olt_config_backups")


@dataclass(frozen=True, slots=True)
class RowVerdict:
    pon_port_id: str
    olt_name: str
    name: str
    verdict: str
    references: tuple[str, ...]


def _reference_counts(db: Session, pon_port_id: object) -> tuple[str, ...]:
    found: list[str] = []
    for label, model, column in REFERENCES:
        count = db.scalar(
            select(func.count()).select_from(model).where(column == pon_port_id)
        )
        if count:
            found.append(f"{label}={count}")
    return tuple(found)


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
    parser.add_argument("--config-root", default=str(DEFAULT_ROOT))
    parser.add_argument(
        "--show", type=int, default=25, help="rows to list per category"
    )
    args = parser.parse_args()
    root = Path(args.config_root)

    db = SessionLocal()
    try:
        verdicts: list[RowVerdict] = []
        counts: Counter[str] = Counter()

        for olt in db.scalars(select(OLTDevice)).all():
            rows = db.scalars(select(PonPort).where(PonPort.olt_id == olt.id)).all()
            if not rows:
                continue

            shape = shape_for_vendor(olt.vendor)
            if shape is PonIdentityShape.single_box:
                # A single-box OLT has no running config here and needs none:
                # the port number is its whole identity and every row is already
                # established. Counting them as "unknown" would pad the worklist
                # with rows that have nothing to decide.
                counts["single_box_skipped"] += len(rows)
                continue
            config = _newest_config(root, str(olt.id))
            reading = (
                parse_running_config(
                    config.read_text(encoding="utf-8", errors="replace")
                )
                if config is not None
                else None
            )
            # Only a config that actually declares PON interfaces is evidence
            # about which ports exist. An empty parse means we learned nothing.
            declared: set[tuple[int, int, int]] | None = (
                {(i.frame, i.slot, p) for i in reading.interfaces for p in i.ports}
                if reading is not None and reading.interfaces
                else None
            )

            for row in rows:
                identity = read_name(row.name, shape=shape).identity
                if declared is None or not isinstance(identity, PonPortIdentity):
                    verdict = "unknown"
                elif (identity.frame, identity.slot, identity.port) in declared:
                    verdict = "backed"
                else:
                    verdict = "unbacked"

                if verdict == "backed":
                    counts["backed"] += 1
                    continue

                references = _reference_counts(db, row.id)
                if verdict == "unknown":
                    counts["unknown"] += 1
                elif references:
                    verdict = "review"
                    counts["review"] += 1
                else:
                    verdict = "retirable"
                    counts["retirable"] += 1

                verdicts.append(
                    RowVerdict(
                        pon_port_id=str(row.id),
                        olt_name=olt.name,
                        name=row.name,
                        verdict=verdict,
                        references=references,
                    )
                )

        total = sum(counts.values())
        print(f"pon_ports examined: {total}")
        for key in ("backed", "retirable", "review", "unknown", "single_box_skipped"):
            print(f"  {key:10s} {counts[key]}")

        for category in ("review", "retirable", "unknown"):
            listed = [v for v in verdicts if v.verdict == category]
            if not listed:
                continue
            print(f"\n{category} ({len(listed)}):")
            for verdict in listed[: args.show]:
                refs = (
                    f"  refs: {', '.join(verdict.references)}"
                    if verdict.references
                    else ""
                )
                print(
                    f"  {verdict.pon_port_id}  {verdict.olt_name:22s} {verdict.name!r}{refs}"
                )
            if len(listed) > args.show:
                print(f"  ... and {len(listed) - args.show} more")

        print(
            "\nNothing was written. `retirable` means unbacked by device evidence "
            "and referenced by nothing; `review` and `unknown` are decisions, not "
            "a batch."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
