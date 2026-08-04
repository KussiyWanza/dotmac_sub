"""Census of PON port identity, before anything is repaired.

``PonPort.name`` is doing two jobs — display text and identity — and the only
uniqueness the database enforces is ``UNIQUE(olt_id, name)``, i.e. uniqueness of
the display string. The structural identity that actually names a port,
``(olt_id, frame, slot, port)``, is stored nowhere.

It is, however, **derivable** from hardware topology::

    PonPort -> OltCardPort.port_number  -> port
            -> OltCard.slot_number      -> slot
            -> OltShelf.shelf_number    -> frame

That chain is authoritative: each link is NOT NULL and uniquely constrained.
But ``PonPort.olt_card_port_id`` is nullable, so it only exists for some rows.
Where it is missing, the sole identity source is ``name`` — the contaminated
field — and no amount of string cleanup can recover a structural identity that
was never recorded.

That distinction is the point of this census. "Prefixed" and "malformed" are
surface symptoms of the same root defect and are cheap to fix. **Underivable**
is a different condition: it cannot be repaired by renaming, and it is what has
to gate bounded reconciliation.

Read-only by construction: a REPEATABLE READ, READ ONLY transaction, no device
I/O, and the session is rolled back rather than committed. Safe on production.

Exit codes: ``0`` when every PON row has an unambiguous derivable identity;
``1`` when any row is underivable or ambiguous — those are the rows that must
be excluded from bounded reconciliation until repaired.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from dataclasses import field as dc_field

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.fiber_access_attachment import FiberAccessAttachmentDecision
from app.models.fiber_physical import FiberConnectorPort
from app.models.network import (
    ForwardingObservation,
    OltCard,
    OltCardPort,
    OltShelf,
    OntAssignment,
    OntSignalObservation,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
)
from app.models.ont_assignment_identity import OntAssignmentIdentityDecision
from app.models.ont_topology_observation import OntTopologyObservationEvidence

EXIT_CLEAN = 0
EXIT_DIRTY = 1

#: Every table that points at a PON port. "No active assignment" does not prove
#: "no references", so a repair worklist that consults only OntAssignment will
#: silently strand rows.
REFERENCES: tuple[tuple[str, object, object], ...] = (
    ("ont_unit", OntUnit, OntUnit.pon_port_id),
    ("ont_assignment", OntAssignment, OntAssignment.pon_port_id),
    ("splitter_link", PonPortSplitterLink, PonPortSplitterLink.pon_port_id),
    ("fiber_connector_port", FiberConnectorPort, FiberConnectorPort.pon_port_id),
    (
        "fiber_attachment_decision",
        FiberAccessAttachmentDecision,
        FiberAccessAttachmentDecision.pon_port_id,
    ),
    ("signal_observation", OntSignalObservation, OntSignalObservation.pon_port_id),
    (
        "forwarding_observation",
        ForwardingObservation,
        ForwardingObservation.pon_port_id,
    ),
    (
        "topology_evidence_observed",
        OntTopologyObservationEvidence,
        OntTopologyObservationEvidence.observed_pon_port_id,
    ),
    (
        "topology_evidence_canonical",
        OntTopologyObservationEvidence,
        OntTopologyObservationEvidence.canonical_pon_port_id,
    ),
    (
        "identity_decision",
        OntAssignmentIdentityDecision,
        OntAssignmentIdentityDecision.target_pon_port_id,
    ),
)

_FSP = re.compile(r"^(\d+)/(\d+)/(\d+)$")
_PREFIX = re.compile(r"^pon-", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class NameReading:
    """What ``PonPort.name`` claims, and how much cleaning that claim needed."""

    canonical: tuple[int, int, int] | None
    prefixed: bool
    malformed: bool


def read_name(name: str | None) -> NameReading:
    raw = (name or "").strip()
    prefixed = bool(_PREFIX.match(raw))
    stripped = _PREFIX.sub("", raw)
    m = _FSP.match(stripped)
    if m is None:
        return NameReading(canonical=None, prefixed=prefixed, malformed=True)
    return NameReading(
        canonical=(int(m[1]), int(m[2]), int(m[3])),
        prefixed=prefixed,
        malformed=False,
    )


@dataclass
class Row:
    pon_port_id: str
    olt_id: str
    name: str
    derived: tuple[int, int, int] | None
    name_reading: NameReading
    references: dict[str, int] = dc_field(default_factory=dict)

    @property
    def total_references(self) -> int:
        return sum(self.references.values())

    @property
    def derivable(self) -> bool:
        return self.derived is not None

    @property
    def agrees(self) -> bool | None:
        """Whether hardware and name tell the same story. ``None`` if unknowable."""
        if self.derived is None or self.name_reading.canonical is None:
            return None
        return self.derived == self.name_reading.canonical

    def classify(self) -> str:
        if not self.derivable:
            return "underivable"
        if self.name_reading.malformed:
            return "derivable_name_malformed"
        if self.agrees is False:
            return "derivable_name_disagrees"
        if self.name_reading.prefixed:
            return "derivable_name_prefixed"
        return "canonical"


def _read_only(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def collect(db: Session) -> list[Row]:
    # Structural identity, straight from the hardware chain. An outer join so a
    # row with no card-port link still appears -- those are the ones that matter.
    stmt = (
        select(
            PonPort.id,
            PonPort.olt_id,
            PonPort.name,
            OltShelf.shelf_number,
            OltCard.slot_number,
            OltCardPort.port_number,
        )
        .select_from(PonPort)
        .outerjoin(OltCardPort, OltCardPort.id == PonPort.olt_card_port_id)
        .outerjoin(OltCard, OltCard.id == OltCardPort.card_id)
        .outerjoin(OltShelf, OltShelf.id == OltCard.shelf_id)
        .order_by(PonPort.olt_id, PonPort.name)
    )

    rows: list[Row] = []
    for pid, olt_id, name, frame, slot, port in db.execute(stmt).all():
        derived = (
            (int(frame), int(slot), int(port))
            if frame is not None and slot is not None and port is not None
            else None
        )
        rows.append(
            Row(
                pon_port_id=str(pid),
                olt_id=str(olt_id),
                name=name or "",
                derived=derived,
                name_reading=read_name(name),
            )
        )

    ids = [r.pon_port_id for r in rows]
    by_id = {r.pon_port_id: r for r in rows}
    for label, model, column in REFERENCES:
        counts = db.execute(
            select(column, func.count())
            .select_from(model)
            .where(column.in_(ids))
            .group_by(column)
        ).all()
        for ref_id, n in counts:
            by_id[str(ref_id)].references[label] = n
    return rows


def main() -> int:
    with SessionLocal() as db:
        _read_only(db)
        rows = collect(db)
        db.rollback()

    # Ambiguity is a property of the set, not of a row: two ports on one OLT
    # resolving to the same structural identity cannot both be canonical, and a
    # merge has to choose between them.
    seen: dict[tuple[str, tuple[int, int, int]], list[Row]] = defaultdict(list)
    for r in rows:
        if r.derived is not None:
            seen[(r.olt_id, r.derived)].append(r)
    ambiguous = {k: v for k, v in seen.items() if len(v) > 1}
    ambiguous_ids = {r.pon_port_id for group in ambiguous.values() for r in group}

    classes = Counter(r.classify() for r in rows)
    blocked = [r for r in rows if not r.derivable or r.pon_port_id in ambiguous_ids]

    report = {
        "total_pon_ports": len(rows),
        "classification": dict(classes.most_common()),
        "ambiguous_structural_identities": len(ambiguous),
        "rows_in_an_ambiguous_group": len(ambiguous_ids),
        "blocked_rows": len(blocked),
        "references_on_blocked_rows": dict(
            sum((Counter(r.references) for r in blocked), Counter()).most_common()
        ),
        "blocked_rows_with_no_references": sum(
            1 for r in blocked if r.total_references == 0
        ),
        "reference_totals": dict(
            sum((Counter(r.references) for r in rows), Counter()).most_common()
        ),
        "prefixed_total": sum(1 for r in rows if r.name_reading.prefixed),
        "prefixed_and_underivable": sum(
            1 for r in rows if r.name_reading.prefixed and not r.derivable
        ),
        "name_disagrees_with_hardware": sum(1 for r in rows if r.agrees is False),
    }

    print(json.dumps(report, indent=2, sort_keys=True))

    if blocked:
        print(
            "\nBlocked from bounded reconciliation "
            "(underivable or ambiguous identity):",
            file=sys.stderr,
        )
        for r in sorted(blocked, key=lambda r: (r.olt_id, r.name)):
            why = "underivable" if not r.derivable else "ambiguous"
            refs = ", ".join(f"{k}={v}" for k, v in sorted(r.references.items())) or "-"
            print(
                f"  {r.pon_port_id}  olt={r.olt_id}  name={r.name!r:<24} "
                f"{why:<12} refs: {refs}",
                file=sys.stderr,
            )
        return EXIT_DIRTY
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
