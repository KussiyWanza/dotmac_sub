"""PON port identity: structural, derived from hardware, and fail-closed.

Every refusal here stops an automatic process from acting on a port nobody can
name, so each is pinned individually rather than inferred from a happy path.
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from app.models.network import (
    OltCard,
    OltCardPort,
    OLTDevice,
    OltShelf,
    PonPort,
)
from app.services.network.pon_port_identity import (
    PonIdentityRefusal,
    PonIdentityShape,
    PonPortIdentity,
    PonPortIdentityError,
    SingleBoxPonIdentity,
    assert_assignable,
    canonical_name,
    classify,
    derive_from_card_port,
    derive_identity,
    materialize_identity,
    read_name,
    shape_for_vendor,
    stored_identity,
)


def _hardware(db, *, frame=0, slot=2, port=3, olt_name="OLT-PON"):
    olt = OLTDevice(name=olt_name, is_active=True)
    db.add(olt)
    db.flush()
    shelf = OltShelf(olt_id=olt.id, shelf_number=frame)
    db.add(shelf)
    db.flush()
    card = OltCard(shelf_id=shelf.id, slot_number=slot)
    db.add(card)
    db.flush()
    card_port = OltCardPort(card_id=card.id, port_number=port)
    db.add(card_port)
    db.flush()
    return olt, card_port


def _pon(db, olt, *, name, card_port=None, is_active=True):
    pon = PonPort(
        olt_id=olt.id,
        name=name,
        olt_card_port_id=card_port.id if card_port is not None else None,
        is_active=is_active,
    )
    db.add(pon)
    db.flush()
    return pon


# ── The value object ────────────────────────────────────────────────────────


def test_a_canonical_string_parses_to_structural_identity():
    identity = PonPortIdentity.parse("0/2/3")

    assert (identity.frame, identity.slot, identity.port) == (0, 2, 3)
    assert canonical_name(identity) == "0/2/3"


@pytest.mark.parametrize("raw", ["pon-0/2/3", "PON-0/2/3"])
def test_a_vendor_prefixed_string_is_not_an_identity(raw):
    """Prefixed forms are transport, converted at ingress -- never identity."""
    with pytest.raises(PonPortIdentityError) as excinfo:
        PonPortIdentity.parse(raw)

    assert excinfo.value.code == PonIdentityRefusal.name_not_canonical


@pytest.mark.parametrize("raw", ["", "pon1", "0/2", "0/2/3/4", "a/b/c", "3"])
def test_a_non_canonical_string_is_refused(raw):
    with pytest.raises(PonPortIdentityError):
        PonPortIdentity.parse(raw)


def test_a_bare_port_number_is_not_an_identity():
    """The defect in one line: `3` names nothing without its frame and slot."""
    with pytest.raises(PonPortIdentityError):
        PonPortIdentity.parse("3")


# ── Reading a stored name without trusting it ───────────────────────────────


def test_reading_a_name_reports_the_prefix_separately_from_the_parse():
    reading = read_name("pon-0/2/3")

    assert reading.prefixed is True
    assert reading.malformed is False
    assert reading.identity == PonPortIdentity(0, 2, 3)


def test_a_malformed_name_yields_no_identity():
    reading = read_name("pon1")

    assert reading.prefixed is False
    assert reading.malformed is True
    assert reading.identity is None


# ── Derivation from hardware ────────────────────────────────────────────────


def test_identity_is_derived_from_the_hardware_chain(db_session):
    olt, card_port = _hardware(db_session, frame=0, slot=2, port=3)
    pon = _pon(db_session, olt, name="0/2/3", card_port=card_port)

    assert derive_identity(db_session, pon) == PonPortIdentity(0, 2, 3)
    assert classify(db_session, pon) == "canonical"


def test_a_row_with_no_card_port_link_has_no_derivable_identity(db_session):
    """The 479-row condition: the name is the only claim, and it is unverifiable."""
    olt, _ = _hardware(db_session)
    pon = _pon(db_session, olt, name="0/2/3", card_port=None)

    assert derive_identity(db_session, pon) is None
    assert classify(db_session, pon) == "underivable"


def test_a_card_port_without_shelf_context_fails_closed(db_session):
    """No fallback to the port number -- that is what wrote `pon-3`."""
    olt = OLTDevice(name="OLT-ORPHAN", is_active=True)
    db_session.add(olt)
    db_session.flush()
    card_port = OltCardPort(card_id=uuid4(), port_number=3)
    db_session.add(card_port)

    with pytest.raises(PonPortIdentityError) as excinfo:
        derive_from_card_port(db_session, card_port)

    assert excinfo.value.code == PonIdentityRefusal.port_number_only


def test_hardware_wins_when_the_name_disagrees(db_session):
    olt, card_port = _hardware(db_session, frame=0, slot=2, port=3)
    pon = _pon(db_session, olt, name="0/9/9", card_port=card_port)

    assert derive_identity(db_session, pon) == PonPortIdentity(0, 2, 3)
    assert classify(db_session, pon) == "derivable_name_disagrees"


def test_a_prefixed_name_on_a_derivable_row_is_reported_as_such(db_session):
    olt, card_port = _hardware(db_session, frame=0, slot=2, port=3)
    pon = _pon(db_session, olt, name="pon-0/2/3", card_port=card_port)

    assert classify(db_session, pon) == "derivable_name_prefixed"


# ── The assignment guard ────────────────────────────────────────────────────


def test_a_canonical_row_may_be_assigned(db_session):
    olt, card_port = _hardware(db_session)
    pon = _pon(db_session, olt, name="0/2/3", card_port=card_port)

    # Returns without raising; the refusal is the failure mode.
    assert assert_assignable(db_session, pon) is None


@pytest.mark.parametrize("name", ["pon-0/2/3", "pon1", "board-2"])
def test_a_prefixed_or_malformed_row_refuses_assignment(db_session, name):
    olt, card_port = _hardware(db_session)
    pon = _pon(db_session, olt, name=name, card_port=card_port)

    with pytest.raises(PonPortIdentityError) as excinfo:
        assert_assignable(db_session, pon)

    assert excinfo.value.code == PonIdentityRefusal.name_not_canonical


def test_two_rows_claiming_one_identity_refuse_assignment(db_session):
    """Ambiguity is a property of the set, and a merge must choose a survivor."""
    olt, card_port = _hardware(db_session)
    first = _pon(db_session, olt, name="0/2/3", card_port=card_port)
    _pon(db_session, olt, name="0/2/3 ", card_port=None)

    with pytest.raises(PonPortIdentityError) as excinfo:
        assert_assignable(db_session, first)

    assert excinfo.value.code == PonIdentityRefusal.ambiguous


def test_an_inactive_duplicate_does_not_refuse_assignment(db_session):
    """Retired history cannot compete with active identity."""
    olt, card_port = _hardware(db_session)
    active = _pon(db_session, olt, name="0/2/3", card_port=card_port)
    _pon(db_session, olt, name="pon-0/2/3", card_port=None, is_active=False)

    assert assert_assignable(db_session, active) is None


def test_ambiguity_carries_typed_immutable_evidence(db_session):
    """A refusal states which rows contest the identity, not just that they do."""
    olt, card_port = _hardware(db_session)
    first = _pon(db_session, olt, name="0/2/3", card_port=card_port)
    second = _pon(db_session, olt, name="0/2/3 ", card_port=None)

    with pytest.raises(PonPortIdentityError) as excinfo:
        assert_assignable(db_session, first)

    conflict = excinfo.value.conflict
    assert conflict is not None
    assert conflict.identity == PonPortIdentity(frame=0, slot=2, port=3)
    assert set(conflict.claimants) == {first.id, second.id}
    assert isinstance(conflict.claimants, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        conflict.identity = PonPortIdentity(frame=9, slot=9, port=9)  # type: ignore[misc]


@pytest.mark.parametrize("twin_name", ["pon-0/2/3", "PON-0/2/3", "pon3", "board-2"])
def test_a_row_pending_repair_cannot_veto_the_canonical_row(db_session, twin_name):
    """The production regression this guard caused.

    A prefixed or malformed sibling is already unassignable in its own right.
    Treating its shadow claim as a contest refused the *correct* row on its
    behalf: 459 of 502 production PON ports were refused, 212 carrying live
    customers, purely because a ``pon-`` twin still existed beside them.
    """
    olt, card_port = _hardware(db_session)
    canonical = _pon(db_session, olt, name="0/2/3", card_port=card_port)
    _pon(db_session, olt, name=twin_name, card_port=None)

    # Returns without raising; the refusal is the failure mode.
    assert assert_assignable(db_session, canonical) is None


def test_the_row_pending_repair_is_still_refused_itself(db_session):
    """Unblocking the canonical twin must not unblock the bad row too."""
    olt, card_port = _hardware(db_session)
    _pon(db_session, olt, name="0/2/3", card_port=card_port)
    twin = _pon(db_session, olt, name="pon-0/2/3", card_port=None)

    with pytest.raises(PonPortIdentityError) as excinfo:
        assert_assignable(db_session, twin)

    assert excinfo.value.code == PonIdentityRefusal.name_not_canonical


def test_a_canonical_name_without_a_hardware_link_is_still_assignable(db_session):
    """Deliberately narrower than the reconciliation gate.

    95% of production PON rows are underivable. Refusing them here would stop
    most provisioning on the estate for a defect that predates the caller, so
    the broader set is excluded from bounded reconciliation -- a reversible
    operational gate -- rather than failing a customer workflow.
    """
    olt, _ = _hardware(db_session)
    pon = _pon(db_session, olt, name="0/2/3", card_port=None)

    assert classify(db_session, pon) == "underivable"
    assert assert_assignable(db_session, pon) is None


# ── The board/port split used when writing ONT inventory ────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("0/1/13", ("0/1", "13")),
        # Splitting on "/" alone accepted this as three parts and returned
        # board "pon-0/1" -- a board that names nothing -- instead of failing
        # closed or reading the real identity.
        ("pon-0/1/13", ("0/1", "13")),
        ("pon1", (None, None)),
        ("board-2", (None, None)),
        ("0/1", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_board_port_split_never_emits_a_board_that_names_nothing(name, expected):
    from app.services.network.ont_assignment_commands import _fsp_parts

    assert _fsp_parts(name, vendor="Huawei") == expected


# ── Single-box platforms have no chassis ────────────────────────────────────


def _single_box_olt(db, *, name="UF-OLT-1"):
    olt = OLTDevice(name=name, vendor="ubiquiti", model="UF-OLT", is_active=True)
    db.add(olt)
    db.flush()
    return olt


def test_shape_is_resolved_from_the_vendor():
    assert shape_for_vendor("ubiquiti") is PonIdentityShape.single_box
    assert shape_for_vendor("Ubiquiti") is PonIdentityShape.single_box
    assert shape_for_vendor("Huawei") is PonIdentityShape.chassis
    # Unknown vendors stay chassis: conservative, and fails toward refusing.
    assert shape_for_vendor(None) is PonIdentityShape.chassis
    assert shape_for_vendor("acme") is PonIdentityShape.chassis


@pytest.mark.parametrize(
    ("raw", "port"), [("pon1", 1), ("PON8", 8), ("3", 3), ("pon12", 12)]
)
def test_a_single_box_port_name_is_a_canonical_identity(raw, port):
    identity = SingleBoxPonIdentity.parse(raw)

    assert identity.port == port
    assert canonical_name(identity) == f"pon{port}"


@pytest.mark.parametrize("raw", ["", "0/1/13", "pon-0/1/13", "board-2", "ponX"])
def test_a_non_port_name_is_not_a_single_box_identity(raw):
    with pytest.raises(PonPortIdentityError):
        SingleBoxPonIdentity.parse(raw)


def test_a_single_box_port_is_assignable_without_any_hardware_link(db_session):
    """The 678-customer regression.

    A UF-OLT has no frame and no slot, so requiring a card-port chain -- or an
    ``f/s/p`` name -- refused every correctly named port on the platform.
    """
    olt = _single_box_olt(db_session)
    pon = _pon(db_session, olt, name="pon5", card_port=None)

    assert derive_identity(db_session, pon) == SingleBoxPonIdentity(port=5)
    assert classify(db_session, pon) == "canonical"
    assert assert_assignable(db_session, pon) is None


def test_a_single_box_row_with_a_chassis_name_is_refused(db_session):
    """Shape cuts both ways: f/s/p names nothing on a box with no slots."""
    olt = _single_box_olt(db_session, name="UF-OLT-2")
    pon = _pon(db_session, olt, name="0/1/13", card_port=None)

    with pytest.raises(PonPortIdentityError) as excinfo:
        assert_assignable(db_session, pon)

    assert excinfo.value.code == PonIdentityRefusal.name_not_canonical
    assert "pon<n>" in str(excinfo.value)


def test_two_single_box_rows_claiming_one_port_are_ambiguous(db_session):
    olt = _single_box_olt(db_session, name="UF-OLT-3")
    first = _pon(db_session, olt, name="pon5", card_port=None)
    _pon(db_session, olt, name="5", card_port=None)

    with pytest.raises(PonPortIdentityError) as excinfo:
        assert_assignable(db_session, first)

    assert excinfo.value.code == PonIdentityRefusal.ambiguous
    assert excinfo.value.conflict.identity == SingleBoxPonIdentity(port=5)


def test_chassis_platforms_are_unaffected_by_the_variant(db_session):
    """A Huawei row still requires f/s/p and still refuses a bare port number."""
    olt, card_port = _hardware(db_session, olt_name="OLT-CHASSIS-REGRESSION")
    good = _pon(db_session, olt, name="0/2/3", card_port=card_port)
    bare = _pon(db_session, olt, name="pon5", card_port=None)

    assert assert_assignable(db_session, good) is None
    with pytest.raises(PonPortIdentityError):
        assert_assignable(db_session, bare)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # There is no board on a single-box OLT; say so rather than invent one.
        ("pon5", (None, "5")),
        ("PON8", (None, "8")),
        ("3", (None, "3")),
        # A chassis name names nothing on a box with no slots.
        ("0/1/13", (None, None)),
        ("board-2", (None, None)),
    ],
)
def test_board_port_split_on_a_single_box_keeps_the_port(name, expected):
    """Reading a ``pon<n>`` name as a chassis name discarded the port number.

    The type checker caught the union-attr error on ``identity.frame``; it could
    not see this. Every UF-OLT assignment and move would have written
    ``board=None, port=None`` and silently lost which port the ONT is on.
    """
    from app.services.network.ont_assignment_commands import _fsp_parts

    assert _fsp_parts(name, vendor="ubiquiti") == expected


# ── Identity stored on the row, not parsed from the name ────────────────────


def test_materialize_writes_chassis_identity_and_is_idempotent(db_session):
    olt, card_port = _hardware(db_session, frame=0, slot=2, port=3)
    pon = _pon(db_session, olt, name="0/2/3", card_port=card_port)

    first = materialize_identity(db_session, pon)

    assert first == PonPortIdentity(frame=0, slot=2, port=3)
    assert (pon.identity_frame, pon.identity_slot, pon.identity_port) == (0, 2, 3)
    assert materialize_identity(db_session, pon) == first


def test_materialize_writes_single_box_identity_with_no_frame(db_session):
    """NULL frame is the positive statement that this platform has none."""
    olt = _single_box_olt(db_session, name="UF-OLT-MAT")
    pon = _pon(db_session, olt, name="pon5", card_port=None)

    identity = materialize_identity(db_session, pon)

    assert identity == SingleBoxPonIdentity(port=5)
    assert pon.identity_frame is None
    assert pon.identity_slot is None
    assert pon.identity_port == 5


def test_materialize_leaves_an_unresolvable_row_untouched(db_session):
    """A transient inability to resolve must never erase a proven identity."""
    olt, _ = _hardware(db_session, olt_name="OLT-UNRESOLVABLE")
    pon = _pon(db_session, olt, name="board-2", card_port=None)
    pon.identity_frame, pon.identity_slot, pon.identity_port = 0, 9, 9

    assert materialize_identity(db_session, pon) is None
    assert (pon.identity_frame, pon.identity_slot, pon.identity_port) == (0, 9, 9)


def test_stored_identity_wins_over_the_name(db_session):
    """Once established, the columns are the authority — not the display text.

    Re-deriving from ``name`` on every read would leave the display string in
    charge of identity, which is the defect this owner exists to end.
    """
    olt, _ = _hardware(db_session, olt_name="OLT-STORED-WINS")
    pon = _pon(db_session, olt, name="0/9/9", card_port=None)
    pon.identity_frame, pon.identity_slot, pon.identity_port = 0, 2, 3

    assert stored_identity(pon) == PonPortIdentity(frame=0, slot=2, port=3)
    assert derive_identity(db_session, pon) == PonPortIdentity(frame=0, slot=2, port=3)


def test_a_row_without_stored_identity_still_derives(db_session):
    """Backfill is not a precondition for the owner to work."""
    olt, card_port = _hardware(db_session, olt_name="OLT-NOT-BACKFILLED")
    pon = _pon(db_session, olt, name="0/2/3", card_port=card_port)

    assert pon.identity_port is None
    assert derive_identity(db_session, pon) == PonPortIdentity(frame=0, slot=2, port=3)


@pytest.mark.parametrize("raw", ["GPON 0/2/3", "gpon  0/2/3", "pon-0/2/3"])
def test_every_vendor_display_form_reads_to_the_same_identity(raw):
    """Production carries both renderings; a reader that knows one is not enough."""
    reading = read_name(raw)

    assert reading.identity == PonPortIdentity(frame=0, slot=2, port=3)
    assert reading.prefixed is True
    assert reading.malformed is False
