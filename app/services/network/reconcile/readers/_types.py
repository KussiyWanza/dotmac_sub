"""Shared types for the readers subpackage.

``ReadResult`` is parameterised on the observation shape so OLT and ACS
readers each return a typed result without coupling to each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

T = TypeVar("T")

#: Tri-state outcome of a single read attempt. Replaces the previous
#: ``success``/``unreachable`` boolean pair, which let a genuinely bad read
#: (an unparseable or rejected device reply, reachable or not) fall through
#: as if it had simply not been attempted. There is no fourth state: a caller
#: that cannot classify its outcome as one of these three has a bug, not an
#: excuse to default somewhere silently.
#:
#: * ``"present"``  — clean read, the device/record exists. ``observed`` is
#:   populated with real values.
#: * ``"absent"``   — clean read, the device/record does not exist (e.g. the
#:   OLT confirms the serial isn't registered, or the ACS has no document for
#:   it yet). ``observed`` is populated with the surface's "absent" shape —
#:   this is real evidence, not a placeholder for failure.
#: * ``"unavailable"`` — the read could not be trusted: a transport failure
#:   (SSH refused, HTTP 5xx, timeout — see ``transport_unreachable``) or a
#:   reachable-but-unparseable/rejected reply. ``observed`` is ``None``.
#:   Callers must refuse to treat this as either ``"present"`` or ``"absent"``
#:   — see ``reconcile.core``'s pre-plan refusal.
ObservationStatus = Literal["present", "absent", "unavailable"]


@dataclass(frozen=True)
class ReadResult(Generic[T]):
    """Outcome of a single OLT or ACS read pass.

    ``status`` is required — there is deliberately no default, so a caller
    constructing one must make an explicit classification decision rather
    than falling into whatever the field ordering happens to default to.

    ``transport_unreachable`` only carries meaning when ``status ==
    "unavailable"``; it distinguishes "couldn't even contact the surface"
    (SSH refused, HTTP 5xx, network timeout — maps to the existing
    ``*_UNREACHABLE`` failure reasons) from "the surface replied but the
    reply could not be trusted" (a rejected command, a parameter error, an
    unparseable body — maps to ``OLT_OBSERVATION_UNAVAILABLE`` for the OLT
    surface). Both are equally unsafe to treat as an observation of reality;
    the distinction exists only to keep the operator-facing failure message
    accurate.

    ``success``/``unreachable`` remain as derived, read-only properties for
    callers that only need the coarser boolean split.
    """

    status: ObservationStatus
    observed: T | None
    error: str | None
    transport_unreachable: bool = False
    #: Sub-classification of an ``"unavailable"`` OLT read that stems from a
    #: physical-identity problem rather than a transport failure or an
    #: unparseable reply: the OLT reported this ONT's serial registered
    #: somewhere other than the desired fsp/olt_ont_id target
    #: (``"mismatch"``), or a registration was found with no confirmed target
    #: to compare it against (``"unresolved"``). ``None`` for every other
    #: ``"unavailable"`` cause. Lets ``reconcile.core`` report
    #: ``OLT_IDENTITY_MISMATCH``/``OLT_IDENTITY_UNRESOLVED`` instead of the
    #: generic ``OLT_OBSERVATION_UNAVAILABLE``.
    identity_status: Literal["mismatch", "unresolved"] | None = None

    def __post_init__(self) -> None:
        if self.status == "unavailable" and self.observed is not None:
            raise ValueError(
                "an 'unavailable' ReadResult must not carry an observation — "
                "that is exactly the substitution this type exists to forbid"
            )
        if self.status != "unavailable" and self.observed is None:
            raise ValueError(f"a {self.status!r} ReadResult must carry an observation")
        if self.transport_unreachable and self.status != "unavailable":
            raise ValueError(
                "transport_unreachable only applies to an 'unavailable' status"
            )
        if self.identity_status is not None and self.status != "unavailable":
            raise ValueError("identity_status only applies to an 'unavailable' status")
        if self.identity_status is not None and self.transport_unreachable:
            raise ValueError(
                "identity_status and transport_unreachable are mutually exclusive "
                "— a transport failure never carries identity information"
            )

    @property
    def success(self) -> bool:
        """Whether this was a clean, trustworthy read (present or absent)."""
        return self.status != "unavailable"

    @property
    def unreachable(self) -> bool:
        """Whether the surface could not be contacted at all (transport-level)."""
        return self.transport_unreachable
