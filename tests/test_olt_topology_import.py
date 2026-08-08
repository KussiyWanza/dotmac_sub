"""Reading OLT topology from a running config, and importing it.

The grammar cases come from production configs: BOI declares an
``interface gpon`` block with no matching ``board add``, and ``ont add`` lines
carry no F/S/P of their own.
"""

from __future__ import annotations

import uuid

from app.models.network import (
    OltCard,
    OltCardPort,
    OLTDevice,
    OltShelf,
    PonPort,
)
from app.services.network.olt_topology_import import import_topology
from app.services.network.olt_topology_parse import parse_running_config
from app.services.network.pon_port_identity import PonPortIdentity, stored_identity

_CONFIG = """
#
board add 0/1 H803GPFD
board add 0/4 H801MPWC
#
interface gpon 0/1
 port 0 ont-auto-find enable
 port 1 ont-auto-find enable
 port 13 ont-auto-find enable
 ont add 0 0 sn-auth "48575443A31C862D" omci ont-lineprofile-id 40
 ont add 0 1 sn-auth "485754436835E484" omci ont-lineprofile-id 40
 ont add 13 1 sn-auth "48575443617CA06D" omci ont-lineprofile-id 40
 quit
#
interface vlanif 100
 ip address 10.0.0.1 255.255.255.0
 quit
#
"""


def _olt(db, *, name="OLT-TOPO", vendor="Huawei"):
    olt = OLTDevice(name=name, vendor=vendor, model="MA5608T", is_active=True)
    db.add(olt)
    db.flush()
    return olt


def _pon(db, olt, name):
    pon = PonPort(olt_id=olt.id, name=name, is_active=True)
    db.add(pon)
    db.flush()
    return pon


# ── Parsing ─────────────────────────────────────────────────────────────────


def test_frames_and_slots_come_from_the_interface_blocks():
    reading = parse_running_config(_CONFIG)

    assert [(i.frame, i.slot) for i in reading.interfaces] == [(0, 1)]
    assert reading.interfaces[0].ports == (0, 1, 13)


def test_ont_lines_inherit_frame_and_slot_from_their_block():
    """``ont add 13 1 ...`` carries no F/S/P; the block supplies it."""
    reading = parse_running_config(_CONFIG)

    positioned = {(o.serial, o.frame, o.slot, o.port) for o in reading.onts}
    assert ("48575443617CA06D", 0, 1, 13) in positioned
    assert len(reading.onts) == 3


def test_board_type_is_read_but_is_not_required_for_a_frame_slot():
    """BOI declares interface gpon 0/1 with no matching board add line."""
    reading = parse_running_config(
        "board add 0/4 H801MPWC\ninterface gpon 0/1\n ont add 3 1 sn-auth ABC\n quit\n"
    )

    assert [(i.frame, i.slot) for i in reading.interfaces] == [(0, 1)]
    assert reading.board_type_for(0, 1) is None
    assert reading.board_type_for(0, 4) == "H801MPWC"


def test_a_non_gpon_interface_closes_the_block():
    """Otherwise a later stanza's lines would be attributed to the PON slot."""
    reading = parse_running_config(_CONFIG)

    assert reading.board_type_for(0, 1) == "H803GPFD"
    assert all(i.slot == 1 for i in reading.interfaces)


def test_an_empty_config_reads_as_nothing_rather_than_failing():
    reading = parse_running_config("")

    assert reading.interfaces == ()
    assert reading.onts == ()


# ── Importing ───────────────────────────────────────────────────────────────


def test_import_builds_the_chain_and_links_the_pon_row(db_session):
    olt = _olt(db_session)
    pon = _pon(db_session, olt, "0/1/13")

    outcome = import_topology(db_session, olt, parse_running_config(_CONFIG))

    assert (outcome.shelves_created, outcome.cards_created) == (1, 1)
    assert outcome.ports_created == 3
    assert outcome.pon_rows_linked == 1
    assert outcome.identities_established == 1
    assert pon.olt_card_port_id is not None
    assert stored_identity(pon) == PonPortIdentity(frame=0, slot=1, port=13)


def test_import_is_idempotent(db_session):
    olt = _olt(db_session, name="OLT-TOPO-IDEMPOTENT")
    _pon(db_session, olt, "0/1/13")
    reading = parse_running_config(_CONFIG)

    import_topology(db_session, olt, reading)
    second = import_topology(db_session, olt, reading)

    assert (second.shelves_created, second.cards_created, second.ports_created) == (
        0,
        0,
        0,
    )
    assert second.pon_rows_linked == 0
    assert db_session.query(OltShelf).filter(OltShelf.olt_id == olt.id).count() == 1


def test_a_position_with_no_pon_row_is_reported_not_invented(db_session):
    """Creating the missing row would be choosing an identity for it."""
    olt = _olt(db_session, name="OLT-TOPO-UNMATCHED")

    outcome = import_topology(db_session, olt, parse_running_config(_CONFIG))

    assert set(outcome.unmatched_positions) == {"0/1/0", "0/1/1", "0/1/13"}
    assert outcome.pon_rows_linked == 0
    assert db_session.query(PonPort).filter(PonPort.olt_id == olt.id).count() == 0


def test_a_row_whose_name_cannot_be_read_is_left_alone(db_session):
    """Linking it by position would pick an identity the name never stated."""
    olt = _olt(db_session, name="OLT-TOPO-UNREADABLE")
    pon = _pon(db_session, olt, "pon-0/1/13")

    outcome = import_topology(db_session, olt, parse_running_config(_CONFIG))

    assert pon.olt_card_port_id is None
    assert "0/1/13" in outcome.unmatched_positions


def test_an_existing_link_is_reported_not_silently_repointed(db_session):
    olt = _olt(db_session, name="OLT-TOPO-CONFLICT")
    pon = _pon(db_session, olt, "0/1/13")
    shelf = OltShelf(olt_id=olt.id, shelf_number=9, is_active=True)
    db_session.add(shelf)
    db_session.flush()
    card = OltCard(shelf_id=shelf.id, slot_number=9)
    db_session.add(card)
    db_session.flush()
    elsewhere = OltCardPort(card_id=card.id, port_number=9, is_active=True)
    db_session.add(elsewhere)
    db_session.flush()
    pon.olt_card_port_id = elsewhere.id

    outcome = import_topology(db_session, olt, parse_running_config(_CONFIG))

    assert pon.olt_card_port_id == elsewhere.id
    assert len(outcome.conflicts) == 1
    assert outcome.conflicts[0].pon_port_name == "0/1/13"
    assert isinstance(outcome.conflicts, tuple)


def test_outcome_evidence_is_immutable(db_session):
    olt = _olt(db_session, name=f"OLT-TOPO-IMMUTABLE-{uuid.uuid4().hex[:6]}")

    outcome = import_topology(db_session, olt, parse_running_config(_CONFIG))

    assert isinstance(outcome.unmatched_positions, tuple)
    assert isinstance(outcome.conflicts, tuple)


def test_a_prefixed_twin_does_not_block_the_canonical_row(db_session):
    """The twin is not a legitimate claimant, so there is no contest to lose."""
    olt = _olt(db_session, name="OLT-TOPO-TWIN")
    canonical = _pon(db_session, olt, "0/1/13")
    twin = _pon(db_session, olt, "pon-0/1/13")

    outcome = import_topology(db_session, olt, parse_running_config(_CONFIG))

    assert canonical.olt_card_port_id is not None
    assert twin.olt_card_port_id is None
    assert outcome.pon_rows_linked == 1


def test_two_canonical_names_for_one_position_link_neither(db_session):
    """``0/1/13`` and ``0/01/13`` are distinct strings and one identity.

    UNIQUE(olt_id, name) permits both rows because it constrains display text.
    Whichever got the hardware would be arbitrary, and both would then claim one
    structural identity. Choosing a survivor is a reviewed decision, not an
    import's.
    """
    olt = _olt(db_session, name="OLT-TOPO-CONTESTED")
    first = _pon(db_session, olt, "0/1/13")
    padded = _pon(db_session, olt, "0/01/13")

    outcome = import_topology(db_session, olt, parse_running_config(_CONFIG))

    assert first.olt_card_port_id is None
    assert padded.olt_card_port_id is None
    assert outcome.pon_rows_linked == 0
    assert any("more than one PON row" in c.detail for c in outcome.conflicts)


def test_idle_ports_are_seen_because_the_config_enumerates_them(db_session):
    """``port <n> ont-auto-find`` lists the board's whole complement.

    Port 1 has no ONT. Reading ports from ``ont add`` alone -- as an earlier cut
    did -- would have made every idle port invisible and the import a lower
    bound over occupied ports only.
    """
    reading = parse_running_config(_CONFIG)

    assert 1 in reading.interfaces[0].ports
    assert not any(o.port == 1 for o in reading.onts)


def test_an_occupied_port_is_kept_even_without_an_auto_find_line():
    """Belt and braces: a firmware that omits the line must not lose the port."""
    reading = parse_running_config(
        'interface gpon 0/2\n ont add 7 1 sn-auth "ABC"\n quit\n'
    )

    assert reading.interfaces[0].ports == (7,)
