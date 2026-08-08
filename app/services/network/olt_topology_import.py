"""Import OLT hardware topology from device evidence. Idempotent.

The OLT is authoritative for its own physical topology, so Sub imports what the
device states and derives from it; it does not assert a shelf/card/port layout
of its own. This is the missing producer behind
``network.pon_port_identity``: that owner derives chassis identity from
``PonPort -> OltCardPort -> OltCard.slot -> OltShelf.shelf``, and on production
that chain existed for 23 of 502 rows because the topology was never recorded.

What this establishes, in order:

1. ``OltShelf`` per frame the device declares.
2. ``OltCard`` per slot, carrying the board type where the config states one.
3. ``OltCardPort`` per PON port the device shows in use.
4. The link ``PonPort.olt_card_port_id``, which is what turns an underivable row
   into one with a structural identity.

Deliberate limits, because the alternative to each is a guess:

* Ports are only created where the device shows one. A running config lists a
  port only if an ONT was added to it, so an empty port is real hardware this
  cannot see. Creating the full complement from the board type would be
  inventing rows from a model number.
* A PON row is linked only when its canonical name agrees with a parsed
  position. A row whose name cannot be read is left alone -- linking it by
  position would be choosing an identity for it, which is the defect the
  identity owner exists to prevent.
* An existing link is never silently repointed. Disagreement is reported as a
  conflict for review, because a PON row already bound to different hardware is
  evidence of something worth understanding, not a value to overwrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OltCard,
    OltCardPort,
    OLTDevice,
    OltPortType,
    OltShelf,
    PonPort,
)
from app.services.events import EventType, emit_event
from app.services.network.olt_topology_parse import OltTopologyReading
from app.services.network.pon_port_identity import (
    PonIdentityShape,
    PonPortIdentity,
    materialize_identity,
    read_name,
)

__all__ = ["TopologyConflict", "TopologyImportOutcome", "import_topology"]


@dataclass(frozen=True, slots=True)
class TopologyConflict:
    """A PON row whose existing hardware link disagrees with the device."""

    pon_port_id: str
    pon_port_name: str
    detail: str


@dataclass(frozen=True, slots=True)
class TopologyImportOutcome:
    """What one import established, immutably.

    Tuples rather than lists: this is admitted evidence of what a device stated
    and what was written from it, and it must not be mutable after the fact.
    """

    olt_id: str
    olt_name: str
    shelves_created: int = 0
    cards_created: int = 0
    ports_created: int = 0
    pon_rows_linked: int = 0
    identities_established: int = 0
    unmatched_positions: tuple[str, ...] = ()
    conflicts: tuple[TopologyConflict, ...] = ()


@dataclass
class _Counters:
    shelves: int = 0
    cards: int = 0
    ports: int = 0
    linked: int = 0
    identities: int = 0
    unmatched: list[str] = field(default_factory=list)
    conflicts: list[TopologyConflict] = field(default_factory=list)


def _shelf(db: Session, olt: OLTDevice, frame: int, counters: _Counters) -> OltShelf:
    shelf = db.scalar(
        select(OltShelf).where(
            OltShelf.olt_id == olt.id, OltShelf.shelf_number == frame
        )
    )
    if shelf is None:
        shelf = OltShelf(olt_id=olt.id, shelf_number=frame, is_active=True)
        db.add(shelf)
        db.flush()
        counters.shelves += 1
    return shelf


def _card(
    db: Session,
    shelf: OltShelf,
    slot: int,
    board_type: str | None,
    counters: _Counters,
) -> OltCard:
    card = db.scalar(
        select(OltCard).where(OltCard.shelf_id == shelf.id, OltCard.slot_number == slot)
    )
    if card is None:
        card = OltCard(shelf_id=shelf.id, slot_number=slot, card_type=board_type)
        db.add(card)
        db.flush()
        counters.cards += 1
    elif board_type and not card.card_type:
        # The device knows its board type; fill it in, but never overwrite an
        # operator-recorded value with a parse.
        card.card_type = board_type
    return card


def _card_port(
    db: Session, card: OltCard, port: int, counters: _Counters
) -> OltCardPort:
    card_port = db.scalar(
        select(OltCardPort).where(
            OltCardPort.card_id == card.id, OltCardPort.port_number == port
        )
    )
    if card_port is None:
        card_port = OltCardPort(
            card_id=card.id,
            port_number=port,
            port_type=OltPortType.pon,
            is_active=True,
        )
        db.add(card_port)
        db.flush()
        counters.ports += 1
    return card_port


def import_topology(
    db: Session,
    olt: OLTDevice,
    reading: OltTopologyReading,
) -> TopologyImportOutcome:
    """Establish shelves, cards, ports and PON links from one device reading.

    Idempotent: re-running against the same reading creates nothing and links
    nothing further. The caller owns the transaction -- nothing is committed
    here, so a dry run is simply a caller that rolls back.
    """
    counters = _Counters()

    pon_rows = db.scalars(select(PonPort).where(PonPort.olt_id == olt.id)).all()
    by_identity: dict[tuple[int, int, int], PonPort] = {}
    contested: set[tuple[int, int, int]] = set()
    for row in pon_rows:
        name_reading = read_name(row.name, shape=PonIdentityShape.chassis)
        identity = name_reading.identity
        # Only a canonically named row may be matched to a position. read_name
        # strips the transport prefix, so "pon-0/1/13" also reads as 0/1/13 --
        # matching on that would link a row whose name we have already decided
        # not to trust, and where a canonical twin exists it would be arbitrary
        # which of the two got the hardware.
        if name_reading.prefixed or name_reading.malformed:
            continue
        if not isinstance(identity, PonPortIdentity):
            continue
        position = (identity.frame, identity.slot, identity.port)
        if position in by_identity:
            contested.add(position)
            continue
        by_identity[position] = row

    for interface in reading.interfaces:
        shelf = _shelf(db, olt, interface.frame, counters)
        card = _card(
            db,
            shelf,
            interface.slot,
            reading.board_type_for(interface.frame, interface.slot),
            counters,
        )
        for port in interface.ports:
            card_port = _card_port(db, card, port, counters)
            position = (interface.frame, interface.slot, port)
            if position in contested:
                counters.conflicts.append(
                    TopologyConflict(
                        pon_port_id="",
                        pon_port_name=f"{interface.frame}/{interface.slot}/{port}",
                        detail=(
                            "more than one PON row reads to this position; a "
                            "survivor must be chosen before it can be linked"
                        ),
                    )
                )
                continue
            matched = by_identity.get(position)
            if matched is None:
                counters.unmatched.append(f"{interface.frame}/{interface.slot}/{port}")
                continue
            if matched.olt_card_port_id is not None:
                if matched.olt_card_port_id != card_port.id:
                    counters.conflicts.append(
                        TopologyConflict(
                            pon_port_id=str(matched.id),
                            pon_port_name=matched.name,
                            detail=(
                                "already linked to a different card port; the "
                                "device places it at "
                                f"{interface.frame}/{interface.slot}/{port}"
                            ),
                        )
                    )
                continue
            matched.olt_card_port_id = card_port.id
            counters.linked += 1
            if materialize_identity(db, matched) is not None:
                counters.identities += 1

    if counters.shelves or counters.cards or counters.ports or counters.linked:
        # Learning that an OLT has hardware Sub never recorded is a new fact
        # about the estate, even though the device was always that shape. It is
        # emitted only when something was actually established, so a re-run
        # that changes nothing stays silent.
        emit_event(
            db,
            EventType.olt_topology_imported,
            {
                "olt_id": str(olt.id),
                "olt_name": olt.name,
                "shelves_created": counters.shelves,
                "cards_created": counters.cards,
                "ports_created": counters.ports,
                "pon_rows_linked": counters.linked,
                "identities_established": counters.identities,
            },
        )

    return TopologyImportOutcome(
        olt_id=str(olt.id),
        olt_name=olt.name,
        shelves_created=counters.shelves,
        cards_created=counters.cards,
        ports_created=counters.ports,
        pon_rows_linked=counters.linked,
        identities_established=counters.identities,
        unmatched_positions=tuple(counters.unmatched),
        conflicts=tuple(counters.conflicts),
    )
