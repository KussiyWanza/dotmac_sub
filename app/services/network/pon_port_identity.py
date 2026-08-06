"""Sole owner of PON port identity.

``PonPort.name`` has been carrying two jobs — display text and identity — and
the database only enforces ``UNIQUE(olt_id, name)``, which is uniqueness of a
display string. Two writers were free to invent a name when none was supplied,
and the identity of a customer-bearing port ended up depending on which code
path created the row.

The identity of a PON port is structural, but **which structure depends on the
platform** — see :class:`PonIdentityShape`.

On a chassis OLT it is ``(olt_id, frame, slot, port)``. It is not a string, and
it is not ``port_number`` on its own — a bare port number names nothing without
the frame and slot it sits in. There, identity is derived from hardware
topology rather than the contaminated field::

    PonPort -> OltCardPort.port_number -> port
            -> OltCard.slot_number     -> slot
            -> OltShelf.shelf_number   -> frame

Each link is NOT NULL and uniquely constrained, so the chain is authoritative
where it exists. ``PonPort.olt_card_port_id`` is nullable, so it does not
always exist; see ``classify``.

On a single-box OLT there is no chassis to walk. The port number *is* the whole
identity, ``(olt_id, port)``, and ``pon<n>`` is its canonical rendering — not a
prefix to strip. Treating ``frame/slot/port`` as universal was a chassis model
imposed on hardware that has none, and it made 121 correctly named UF-OLT ports
carrying 678 live assignments permanently unassignable.

Where the structural parts are unknown the answer is a refusal, never a
generated placeholder: a fabricated name is indistinguishable from a real one
the moment it is written.

Identity is now **stored**, not re-parsed from the name on every read. Migration
``487_pon_structural_identity`` adds ``identity_frame``/``identity_slot``/
``identity_port`` and constrains them with two partial unique indexes, one per
shape — because a single universal constraint could never cover both. Those
columns are owned here: no UI or import path may write them.
:func:`materialize_identity` establishes them and is idempotent;
:func:`derive_identity` prefers what is recorded and falls back to deriving it
only while a row is still un-backfilled.

Scope note. Repair of rows that have no derivable identity at all is still out
of scope: on production 194 Huawei rows carry no hardware link and no usable
name, and they are left with NULL identity columns, matched by neither partial
index, until the topology import or their deletion. Defensive prefix stripping
elsewhere in the codebase stays until that is done.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Self
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.network import OltCard, OltCardPort, OLTDevice, OltShelf, PonPort

__all__ = [
    "PonIdentity",
    "PonIdentityConflict",
    "PonIdentityShape",
    "PonIdentityRefusal",
    "PonPortIdentity",
    "PonPortIdentityError",
    "PonPortNameReading",
    "SingleBoxPonIdentity",
    "assert_assignable",
    "canonical_name",
    "classify",
    "derive_identity",
    "materialize_identity",
    "read_name",
    "shape_for_vendor",
    "stored_identity",
]

#: A canonical PON name is exactly ``frame/slot/port``. Anything else is either
#: a vendor-prefixed transport string or malformed.
_FSP = re.compile(r"^(\d+)/(\d+)/(\d+)$")

#: Vendor CLI output prefixes a PON interface with ``pon-``/``PON-``. That form
#: is acceptable **at transport ingress only**, where it must be converted
#: immediately; it is never an identity and never a stored name.
_TRANSPORT_PREFIX = re.compile(r"^pon-", re.IGNORECASE)

#: A single-box OLT names its ports ``pon<n>``; the bare number is accepted at
#: ingress and rendered canonically as ``pon<n>``.
_SINGLE_BOX_PORT = re.compile(r"^(?:pon)?(\d+)$", re.IGNORECASE)


class PonIdentityRefusal:
    """Stable refusal codes, so callers branch on a code rather than a message."""

    unknown_structure = "pon_identity_structure_unknown"
    port_number_only = "pon_identity_port_number_insufficient"
    name_not_canonical = "pon_identity_name_not_canonical"
    ambiguous = "pon_identity_ambiguous"


@dataclass(frozen=True, slots=True)
class PonIdentityConflict:
    """The rows that both claim one structural identity.

    Immutable evidence carried on the refusal so a caller — or a later merge —
    can act on the conflict without parsing a message string. ``claimants`` is
    a tuple rather than a list so the admitted evidence cannot be mutated after
    the refusal is raised.
    """

    identity: PonIdentity
    claimants: tuple[UUID, ...]


class PonPortIdentityError(ValueError):
    """A PON port cannot be identified, so no decision may be taken about it."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        conflict: PonIdentityConflict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        #: Structured provenance for ``ambiguous`` refusals; ``None`` otherwise.
        self.conflict = conflict


class PonIdentityShape(str, Enum):
    """How a platform names a PON port, because not every OLT has a chassis.

    ``chassis`` — the port sits in a slot in a frame, so identity is
    ``frame/slot/port``. Huawei MA5600/MA5608T/MA5800, ZTE, Nokia.

    ``single_box`` — the OLT *is* the box. There is no frame and no slot; the
    port number is the whole identity. Ubiquiti UF-OLT.

    Defining identity as ``frame/slot/port`` for everything is a chassis model
    imposed on hardware that has no chassis. On the UF-OLT estate that made
    every correctly named port unidentifiable: 121 rows carrying 678 live
    assignments were refused, permanently, for being named ``pon1``..``pon8`` —
    which is the only faithful name that hardware admits.
    """

    chassis = "chassis"
    single_box = "single_box"


#: Vendors whose OLTs have no chassis. Matched against ``OLTDevice.vendor``
#: case-insensitively by substring, mirroring ``get_olt_adapter``.
_SINGLE_BOX_VENDORS = frozenset({"ubiquiti", "ubnt"})


def shape_for_vendor(vendor: str | None) -> PonIdentityShape:
    """Identity shape for a vendor string. Chassis is the conservative default.

    An unknown vendor is treated as a chassis platform: that is the existing
    behaviour for every OLT in the fleet bar one, and it fails toward refusing
    a malformed name rather than silently accepting a bare port number.
    """
    lowered = (vendor or "").strip().casefold()
    if any(token in lowered for token in _SINGLE_BOX_VENDORS):
        return PonIdentityShape.single_box
    return PonIdentityShape.chassis


@dataclass(frozen=True, slots=True)
class PonPortIdentity:
    """Validated structural identity of one PON port in a chassis.

    Extracted from the strict ``OntFsp`` value object that already existed in
    ``ont_authorization_contracts``, which validated the same three numbers for
    the authorization path only. Identity is a property of the port, not of one
    caller, so it lives here.
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


@dataclass(frozen=True, slots=True)
class SingleBoxPonIdentity:
    """Validated identity of one PON port on an OLT with no chassis.

    The port number is the entire identity. Rendering it as ``pon<n>`` is not a
    vendor prefix to be stripped — on this platform it *is* the canonical name,
    and the established estate naming (``pon1``..``pon8``) already matches.
    """

    port: int

    @classmethod
    def parse(cls, value: str) -> Self:
        raw = (value or "").strip()
        m = _SINGLE_BOX_PORT.match(raw)
        if m is None:
            raise PonPortIdentityError(
                f"{raw!r} is not a canonical single-box PON identity.",
                code=PonIdentityRefusal.name_not_canonical,
            )
        return cls(port=int(m[1]))

    @property
    def value(self) -> str:
        return f"pon{self.port}"

    def __str__(self) -> str:
        return self.value


#: A PON port is identified by whichever shape its platform admits.
PonIdentity = PonPortIdentity | SingleBoxPonIdentity


def canonical_name(identity: PonIdentity) -> str:
    """The one rendering of a PON port name, for that port's platform.

    ``frame/slot/port`` on a chassis, ``pon<n>`` on a single-box OLT. ``name``
    is a projection of identity, not an independently writable field.
    """
    return identity.value


@dataclass(frozen=True, slots=True)
class PonPortNameReading:
    """What a stored ``name`` claims, and how much cleaning that claim needed."""

    identity: PonIdentity | None
    prefixed: bool
    malformed: bool


def read_name(
    name: str | None,
    *,
    shape: PonIdentityShape = PonIdentityShape.chassis,
) -> PonPortNameReading:
    """Interpret a stored name without trusting it, for a platform's shape.

    Reports the prefix separately from the parse so callers can tell a
    vendor-prefixed row (repairable by renaming) from a malformed one.

    ``shape`` defaults to ``chassis`` so existing chassis-only callers are
    unchanged. On a ``single_box`` platform a bare ``pon<n>`` is canonical and
    is **not** treated as a prefixed chassis name — the ``pon`` there is the
    name, not a transport prefix wrapped around an ``f/s/p``.
    """
    raw = (name or "").strip()

    if shape is PonIdentityShape.single_box:
        try:
            return PonPortNameReading(
                identity=SingleBoxPonIdentity.parse(raw),
                prefixed=False,
                malformed=False,
            )
        except PonPortIdentityError:
            return PonPortNameReading(identity=None, prefixed=False, malformed=True)

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


def shape_for_pon_port(db: Session, pon_port: PonPort) -> PonIdentityShape:
    """Identity shape of the platform this row belongs to."""
    olt = db.get(OLTDevice, pon_port.olt_id) if pon_port.olt_id is not None else None
    return shape_for_vendor(getattr(olt, "vendor", None))


def stored_identity(pon_port: PonPort) -> PonIdentity | None:
    """Identity as recorded on the row, or ``None`` when not yet established.

    These columns are the point of the whole exercise: identity that the
    database can constrain, rather than a claim parsed out of a display string.
    ``identity_port`` non-NULL is the flag; ``identity_frame`` NULL alongside it
    is the positive statement that the platform has no frame.
    """
    port = pon_port.identity_port
    if port is None:
        return None
    frame = pon_port.identity_frame
    slot = pon_port.identity_slot
    if frame is None:
        return SingleBoxPonIdentity(port=int(port))
    if slot is None:  # pragma: no cover - a frame without a slot is not writable
        return None
    return PonPortIdentity(frame=int(frame), slot=int(slot), port=int(port))


def materialize_identity(db: Session, pon_port: PonPort) -> PonIdentity | None:
    """Resolve identity and persist it onto the row. Idempotent.

    Backfill and repair entry point. Resolution stays exactly as
    :func:`derive_identity` defines it — this only writes the answer down, so
    running it twice is a no-op and running it after the source of truth changes
    corrects the row.

    Returns the identity written, or ``None`` when it could not be established;
    in that case the columns are left untouched rather than cleared, because a
    transient inability to resolve must not erase an identity already proven.
    """
    identity = _resolve_from_sources(db, pon_port)
    if identity is None:
        return None
    if isinstance(identity, SingleBoxPonIdentity):
        pon_port.identity_frame = None
        pon_port.identity_slot = None
        pon_port.identity_port = identity.port
    else:
        pon_port.identity_frame = identity.frame
        pon_port.identity_slot = identity.slot
        pon_port.identity_port = identity.port
    return identity


def derive_identity(db: Session, pon_port: PonPort) -> PonIdentity | None:
    """Structural identity of a stored PON port, or ``None`` when unknowable.

    ``None`` is not a failure to be papered over — it means this row never
    recorded the hardware link that would name it, and no amount of string
    cleanup can recover that. Callers that must decide something about the port
    use :func:`assert_assignable` instead of guessing.

    Prefers identity already written to the row: once established it is the
    authority, and re-deriving it from ``name`` on every read would leave the
    display string in charge of identity, which is the defect this owner exists
    to end.

    On a single-box platform there is no hardware chain to walk, and none is
    missing: the port number *is* the structural identity, so it is read from
    the name. Requiring a card-port link there would report every correctly
    named UF-OLT port as underivable forever.
    """
    recorded = stored_identity(pon_port)
    if recorded is not None:
        return recorded
    return _resolve_from_sources(db, pon_port)


def _resolve_from_sources(db: Session, pon_port: PonPort) -> PonIdentity | None:
    """Resolve identity from hardware or name, ignoring what is on the row.

    Kept separate from :func:`derive_identity` so repair is possible: that
    function prefers the stored columns, so re-deriving through it would make
    an already-written identity permanently self-confirming and
    :func:`materialize_identity` could never correct drift.
    """
    if shape_for_pon_port(db, pon_port) is PonIdentityShape.single_box:
        return read_name(pon_port.name, shape=PonIdentityShape.single_box).identity
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
    shape = shape_for_pon_port(db, pon_port)
    derived = derive_identity(db, pon_port)
    reading = read_name(pon_port.name, shape=shape)
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

    Two independent conditions narrow what may contest an identity, because
    each alone left production largely unassignable:

    * Only **active** siblings compete. Inactive rows are preserved history and
      must not block a live workflow.
    * Only **canonically named** siblings compete. A prefixed or malformed row
      is already unassignable in its own right and is pending repair, so
      treating its shadow claim as a contest refused the correct row on its
      behalf -- ``read_name`` strips the transport prefix, so ``pon-0/1/13``
      reports identity ``0/1/13`` and shadow-claims its canonical twin.

    Measured against production: the guard refused 459 of 502 PON ports, 212
    carrying live customers. Of the 144 contested pairs, 142 of the prefixed
    twins were *active*, so restricting to active rows alone recovered 2 of
    them; both conditions together recover all 144 (assignable 43 -> 187).
    """
    shape = shape_for_pon_port(db, pon_port)
    reading = read_name(pon_port.name, shape=shape)
    if reading.prefixed or reading.malformed:
        expected = (
            "frame/slot/port" if shape is PonIdentityShape.chassis else "pon<n> port"
        )
        raise PonPortIdentityError(
            f"PON port {pon_port.name!r} does not carry a canonical "
            f"{expected} identity; it must be repaired through the "
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
            PonPort.is_active.is_(True),
        )
        .all()
    )
    for other in siblings:
        other_reading = read_name(other.name, shape=shape)
        # A sibling whose own name is prefixed or malformed is already refused
        # in its own right and is pending repair. Letting it claim an identity
        # would let a row nobody chose veto the canonical row that names the
        # same port -- which refused assignment on every port that still has a
        # `pon-` twin, including ports carrying live customers. Only a
        # canonically named sibling can contest an identity, and
        # UNIQUE(olt_id, name) already makes two of those impossible.
        if other_reading.prefixed or other_reading.malformed:
            continue
        if other_reading.identity == identity:
            raise PonPortIdentityError(
                f"PON identity {identity} is ambiguous on this OLT: rows "
                f"{pon_port.id} and {other.id} both claim it.",
                code=PonIdentityRefusal.ambiguous,
                conflict=PonIdentityConflict(
                    identity=identity,
                    claimants=(pon_port.id, other.id),
                ),
            )
