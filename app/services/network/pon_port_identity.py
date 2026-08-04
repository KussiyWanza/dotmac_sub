"""Sole owner of PON port identity.

``PonPort.name`` has been carrying two jobs — display text and identity — and
the database only enforces ``UNIQUE(olt_id, name)``, which is uniqueness of a
display string. Two writers were free to invent a name when none was supplied,
and the identity of a customer-bearing port ended up depending on which code
path created the row.

The identity of a PON port is structural: ``(olt_id, frame, slot, port)``. It
is not a string, and it is not ``port_number`` on its own — a bare port number
names nothing without the frame and slot it sits in, so this module refuses to
build an identity from one. Where the structural parts are unknown the answer
is a refusal, never a generated placeholder: a fabricated name is
indistinguishable from a real one the moment it is written.

Identity is derived from hardware topology, not from the contaminated field::

    PonPort -> OltCardPort.port_number -> port
            -> OltCard.slot_number     -> slot
            -> OltShelf.shelf_number   -> frame

Each link is NOT NULL and uniquely constrained, so the chain is authoritative
where it exists. ``PonPort.olt_card_port_id`` is nullable, so it does not
always exist; see ``classify``.

Scope note. This module is containment: it owns identity, canonical rendering,
and the refusals. It deliberately does **not** repair data, and there is no
database constraint on ``(olt_id, frame, slot, port)`` yet — both belong to a
separate reviewed migration slice. Defensive prefix stripping elsewhere in the
codebase stays until that slice has verified the data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self

from sqlalchemy.orm import Session

from app.models.network import OltCard, OltCardPort, OltShelf, PonPort

__all__ = [
    "PonIdentityRefusal",
    "PonPortIdentity",
    "PonPortIdentityError",
    "PonPortNameReading",
    "assert_assignable",
    "canonical_name",
    "classify",
    "derive_identity",
    "read_name",
]

#: A canonical PON name is exactly ``frame/slot/port``. Anything else is either
#: a vendor-prefixed transport string or malformed.
_FSP = re.compile(r"^(\d+)/(\d+)/(\d+)$")

#: Vendor CLI output prefixes a PON interface with ``pon-``/``PON-``. That form
#: is acceptable **at transport ingress only**, where it must be converted
#: immediately; it is never an identity and never a stored name.
_TRANSPORT_PREFIX = re.compile(r"^pon-", re.IGNORECASE)


class PonIdentityRefusal:
    """Stable refusal codes, so callers branch on a code rather than a message."""

    unknown_structure = "pon_identity_structure_unknown"
    port_number_only = "pon_identity_port_number_insufficient"
    name_not_canonical = "pon_identity_name_not_canonical"
    ambiguous = "pon_identity_ambiguous"


class PonPortIdentityError(ValueError):
    """A PON port cannot be identified, so no decision may be taken about it."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PonPortIdentity:
    """Validated structural identity of one PON port.

    Extracted from the strict ``OntFsp`` value object that already existed in
    ``ont_authorization_contracts``, which validated the same three numbers for
    the authorization path only. Identity is a property of the port, not of one
    caller, so it lives here and that contract now consumes this.
    """

    frame: int
    slot: int
    port: int

    @classmethod
    def parse(cls, value: str) -> Self:
        """Build an identity from a canonical ``frame/slot/port`` string.

        Refuses a vendor-prefixed string. Ingress adapters strip the prefix and
        convert immediately; by the time a value reaches identity it is either
        canonical or wrong.
        """
        raw = (value or "").strip()
        m = _FSP.match(raw)
        if m is None:
            raise PonPortIdentityError(
                f"{raw!r} is not a canonical frame/slot/port identity.",
                code=PonIdentityRefusal.name_not_canonical,
            )
        return cls(frame=int(m[1]), slot=int(m[2]), port=int(m[3]))

    @property
    def value(self) -> str:
        return f"{self.frame}/{self.slot}/{self.port}"

    def __str__(self) -> str:
        return self.value


def canonical_name(identity: PonPortIdentity) -> str:
    """The one rendering of a PON port name.

    Always ``frame/slot/port``. ``name`` is a projection of identity, not an
    independently writable field.
    """
    return identity.value


@dataclass(frozen=True, slots=True)
class PonPortNameReading:
    """What a stored ``name`` claims, and how much cleaning that claim needed."""

    identity: PonPortIdentity | None
    prefixed: bool
    malformed: bool


def read_name(name: str | None) -> PonPortNameReading:
    """Interpret a stored name without trusting it.

    Reports the prefix separately from the parse so callers can tell a
    vendor-prefixed row (repairable by renaming) from a malformed one.
    """
    raw = (name or "").strip()
    prefixed = bool(_TRANSPORT_PREFIX.match(raw))
    stripped = _TRANSPORT_PREFIX.sub("", raw)
    m = _FSP.match(stripped)
    if m is None:
        return PonPortNameReading(identity=None, prefixed=prefixed, malformed=True)
    return PonPortNameReading(
        identity=PonPortIdentity(frame=int(m[1]), slot=int(m[2]), port=int(m[3])),
        prefixed=prefixed,
        malformed=False,
    )


def derive_from_card_port(db: Session, card_port: OltCardPort) -> PonPortIdentity:
    """Derive identity from the hardware chain. Fails closed.

    A card port alone gives only the port number. Without the card's slot and
    the shelf's frame there is no identity to state, and inventing one is the
    defect this owner exists to stop.
    """
    card = db.get(OltCard, card_port.card_id)
    shelf = db.get(OltShelf, card.shelf_id) if card is not None else None
    if card is None or shelf is None:
        raise PonPortIdentityError(
            "Cannot derive a PON identity: the card port has no shelf/slot "
            "context, and a port number alone does not identify a port.",
            code=PonIdentityRefusal.port_number_only,
        )
    return PonPortIdentity(
        frame=int(shelf.shelf_number),
        slot=int(card.slot_number),
        port=int(card_port.port_number),
    )


def derive_identity(db: Session, pon_port: PonPort) -> PonPortIdentity | None:
    """Structural identity of a stored PON port, or ``None`` when unknowable.

    ``None`` is not a failure to be papered over — it means this row never
    recorded the hardware link that would name it, and no amount of string
    cleanup can recover that. Callers that must decide something about the port
    use :func:`assert_assignable` instead of guessing.
    """
    if pon_port.olt_card_port_id is None:
        return None
    card_port = db.get(OltCardPort, pon_port.olt_card_port_id)
    if card_port is None:
        return None
    try:
        return derive_from_card_port(db, card_port)
    except PonPortIdentityError:
        return None


def classify(db: Session, pon_port: PonPort) -> str:
    """Where one stored row sits relative to canonical identity.

    ``canonical`` — derivable and the name agrees.
    ``derivable_name_prefixed`` / ``derivable_name_malformed`` /
    ``derivable_name_disagrees`` — hardware knows, the name lies.
    ``underivable`` — no hardware link; the name is the only claim, and it is
    unverifiable.
    """
    derived = derive_identity(db, pon_port)
    reading = read_name(pon_port.name)
    if derived is None:
        return "underivable"
    if reading.malformed:
        return "derivable_name_malformed"
    if reading.identity is not None and reading.identity != derived:
        return "derivable_name_disagrees"
    if reading.prefixed:
        return "derivable_name_prefixed"
    return "canonical"


def assert_assignable(db: Session, pon_port: PonPort) -> None:
    """Refuse to assign or provision against an unidentifiable PON row.

    Refuses a prefixed, malformed, or ambiguous row. A row whose name is
    canonical but whose hardware link is missing is **not** refused here: its
    name states a well-formed identity, and refusing it would stop most
    provisioning on the estate for a defect that predates the caller. That
    broader set is excluded from bounded reconciliation instead, which is a
    reversible operational gate rather than a hard failure in a customer
    workflow.
    """
    reading = read_name(pon_port.name)
    if reading.prefixed or reading.malformed:
        raise PonPortIdentityError(
            f"PON port {pon_port.name!r} does not carry a canonical "
            "frame/slot/port identity; it must be repaired through the "
            "identity owner before it can be assigned.",
            code=PonIdentityRefusal.name_not_canonical,
        )
    identity = reading.identity
    if identity is None:  # pragma: no cover - implied by the checks above
        raise PonPortIdentityError(
            f"PON port {pon_port.name!r} has no identity.",
            code=PonIdentityRefusal.unknown_structure,
        )
    siblings = (
        db.query(PonPort)
        .filter(
            PonPort.olt_id == pon_port.olt_id,
            PonPort.id != pon_port.id,
        )
        .all()
    )
    for other in siblings:
        other_reading = read_name(other.name)
        if other_reading.identity == identity:
            raise PonPortIdentityError(
                f"PON identity {identity} is ambiguous on this OLT: rows "
                f"{pon_port.id} and {other.id} both claim it.",
                code=PonIdentityRefusal.ambiguous,
            )
