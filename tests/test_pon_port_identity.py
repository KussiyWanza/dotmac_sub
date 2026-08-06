"""PON port identity: structural, derived from hardware, and fail-closed.

Every refusal here stops an automatic process from acting on a port nobody can
name, so each is pinned individually rather than inferred from a happy path.
"""

from __future__ import annotations

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
    PonPortIdentity,
    PonPortIdentityError,
    assert_assignable,
    canonical_name,
    classify,
    derive_from_card_port,
    derive_identity,
    read_name,
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
