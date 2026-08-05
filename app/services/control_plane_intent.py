"""Shared semantic contract for network control-plane intent delivery.

Vendor adapters keep their native persistence states. This module gives those
states one lifecycle for orchestration, reporting, and safety checks without
making one vendor model authoritative for another.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class ControlPlanePhase(str, enum.Enum):
    desired = "desired"
    planned = "planned"
    queued = "queued"
    applying = "applying"
    readback_pending = "readback_pending"
    verified = "verified"
    drifted = "drifted"
    failed = "failed"


class ControlPlaneContractError(ValueError):
    """Base error for invalid lifecycle and revision operations."""


class ControlPlaneTransitionError(ControlPlaneContractError):
    pass


class ControlPlaneHeadConflict(ControlPlaneContractError):
    pass


#: The bounded scalar contract for a substituted desired value.
#:
#: A default that a composition layer can substitute is always a scalar: a
#: string, an integer, a boolean, or nothing. Accepting ``Any`` here would let
#: a provider hand over a structure this module cannot compare, and the
#: comparison is the whole ruling.
DesiredScalar = str | int | bool | None


class DesiredValueAuthority(enum.StrEnum):
    """Who, if anyone, authorises executing a substituted default.

    This is execution authority, deliberately separate from review progress.
    Only :attr:`declared_default` grants execution; nothing about how far a
    review has got can confer it.
    """

    #: A named owner explicitly declared this default. Executable.
    declared_default = "declared_default"
    #: No owner authorises it. Never executable; the provider refuses.
    inadmissible = "inadmissible"
    #: A different named owner rules on this value and already fails closed.
    #: This provider must not add a competing guard — two owners refusing the
    #: same value independently is how refusals start disagreeing.
    delegated = "delegated"
    #: Executes today with no declaration behind it. A recorded authority debt,
    #: not a permission: providers must hold these on a shrink-only baseline so
    #: the set can be paid down but never grown.
    undeclared = "undeclared"


class DesiredValueAdjudication(enum.StrEnum):
    """How far the review of a default has got. Never grants execution."""

    approved = "approved"
    undecided = "undecided"
    refused = "refused"


class DesiredValueProvenance(enum.StrEnum):
    """How a desired value acquired its concrete representation.

    ``unknown`` is deliberately distinct from a concrete false/zero/empty
    value.  A provider may keep such a representation in a typed state object,
    but neither planning nor applying may treat it as executable intent.
    """

    explicit = "explicit"
    declared_default = "declared_default"
    unknown = "unknown"


def has_executable_desired_provenance(
    provenance: DesiredValueProvenance,
) -> bool:
    """Whether provenance is sufficient to execute the represented value."""
    return isinstance(provenance, DesiredValueProvenance) and provenance in {
        DesiredValueProvenance.explicit,
        DesiredValueProvenance.declared_default,
    }


@dataclass(frozen=True, slots=True)
class DesiredValueDeclaration:
    """One provider's declaration about one substituted default."""

    field: str
    sentinel: DesiredScalar
    authority: DesiredValueAuthority
    adjudication: DesiredValueAdjudication
    #: The owner behind ``declared_default`` or ``delegated``. Required for
    #: both, forbidden otherwise: an authority with no name is not an authority.
    declared_by: str | None = None

    def __post_init__(self) -> None:
        named = {
            DesiredValueAuthority.declared_default,
            DesiredValueAuthority.delegated,
        }
        if self.authority in named and not (self.declared_by or "").strip():
            raise ControlPlaneContractError(
                f"{self.field!r} claims {self.authority.value} without naming an owner"
            )
        if self.authority not in named and self.declared_by:
            raise ControlPlaneContractError(
                f"{self.field!r} names an owner but claims no delegated authority"
            )
        if (
            self.authority is DesiredValueAuthority.declared_default
            and self.adjudication is not DesiredValueAdjudication.approved
        ):
            raise ControlPlaneContractError(
                f"{self.field!r} is executable but its review is "
                f"{self.adjudication.value}; only an approved default executes"
            )
        if (
            self.adjudication is DesiredValueAdjudication.refused
            and self.authority is DesiredValueAuthority.declared_default
        ):
            raise ControlPlaneContractError(
                f"{self.field!r} was refused and cannot also be a declared default"
            )


def is_executable_desired_value(
    value: DesiredScalar,
    *,
    declaration: DesiredValueDeclaration,
) -> bool:
    """Apply the control-plane admissibility rule to one desired value.

    The rule: *missing or provenance-unknown desired state must remain typed as
    unknown and cannot become an executable device value unless a named owner
    explicitly declares that default.*

    A provider registers, per field, the concrete value its composition layers
    substitute when the source is absent, and who authorises executing it. This
    function decides; the provider's delivery path enforces. Vendor adapters
    therefore share one rule without this module learning any vendor's field
    names.

    A value that is not the sentinel is always executable — it is real intent,
    whatever the declaration says about the default.

    Type identity is part of the comparison: ``False == 0`` in Python, so a
    boolean is never matched against a numeric sentinel and vice versa.
    """
    if isinstance(value, bool) is not isinstance(declaration.sentinel, bool):
        return True
    if value != declaration.sentinel:
        return True
    # The value *is* the substituted default. Only an authority admits it.
    if declaration.authority is DesiredValueAuthority.declared_default:
        return True
    if declaration.authority is DesiredValueAuthority.delegated:
        # Ruled on elsewhere; refusing here too would double-guard.
        return True
    if declaration.authority is DesiredValueAuthority.undeclared:
        # Honest about the debt: this executes, and the provider's shrink-only
        # baseline is what stops the set from growing.
        return True
    return False


@dataclass(frozen=True)
class ControlPlaneTarget:
    """Canonical identity for one desired-state revision."""

    provider: str
    target_type: str
    target_id: str
    desired_revision: int

    def __post_init__(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("target_type", self.target_type),
            ("target_id", self.target_id),
        ):
            cleaned = str(value).strip()
            if not cleaned:
                raise ControlPlaneContractError(f"{name} is required")
            if name in {"provider", "target_type"}:
                cleaned = cleaned.lower()
            object.__setattr__(self, name, cleaned)
        if self.desired_revision < 1:
            raise ControlPlaneContractError("desired_revision must be positive")

    @property
    def correlation_key(self) -> str:
        return (
            f"{self.provider}:{self.target_type}:{self.target_id}:"
            f"revision:{self.desired_revision}"
        )

    def as_payload(self) -> dict[str, str | int]:
        return {
            "provider": self.provider,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "desired_revision": self.desired_revision,
        }


_ALLOWED_TRANSITIONS: dict[ControlPlanePhase, frozenset[ControlPlanePhase]] = {
    ControlPlanePhase.desired: frozenset(
        {
            ControlPlanePhase.planned,
            ControlPlanePhase.queued,
            ControlPlanePhase.applying,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.planned: frozenset(
        {ControlPlanePhase.queued, ControlPlanePhase.failed}
    ),
    ControlPlanePhase.queued: frozenset(
        {
            ControlPlanePhase.applying,
            ControlPlanePhase.readback_pending,
            ControlPlanePhase.verified,
            ControlPlanePhase.drifted,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.applying: frozenset(
        {
            ControlPlanePhase.readback_pending,
            ControlPlanePhase.verified,
            ControlPlanePhase.drifted,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.readback_pending: frozenset(
        {
            ControlPlanePhase.applying,
            ControlPlanePhase.verified,
            ControlPlanePhase.drifted,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.verified: frozenset(
        {ControlPlanePhase.desired, ControlPlanePhase.drifted}
    ),
    ControlPlanePhase.drifted: frozenset(
        {
            ControlPlanePhase.desired,
            ControlPlanePhase.planned,
            ControlPlanePhase.queued,
            ControlPlanePhase.applying,
            ControlPlanePhase.readback_pending,
            ControlPlanePhase.verified,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.failed: frozenset(
        {
            ControlPlanePhase.desired,
            ControlPlanePhase.planned,
            ControlPlanePhase.queued,
            ControlPlanePhase.applying,
            ControlPlanePhase.readback_pending,
        }
    ),
}


def assert_phase_transition(
    current: ControlPlanePhase, destination: ControlPlanePhase
) -> None:
    """Reject an impossible semantic transition; idempotent writes are allowed."""
    if current == destination:
        return
    if destination not in _ALLOWED_TRANSITIONS[current]:
        raise ControlPlaneTransitionError(
            f"Cannot transition control-plane intent from '{current.value}' "
            f"to '{destination.value}'"
        )


def assert_intent_head(*, expected_revision: int, current_revision: int) -> None:
    """Prevent a queued operation from writing a superseded intent revision."""
    if expected_revision < 1:
        raise ControlPlaneContractError("expected_revision must be positive")
    if expected_revision != current_revision:
        raise ControlPlaneHeadConflict(
            f"Intent revision {expected_revision} is stale; current revision is "
            f"{current_revision}"
        )


def _status_value(status: Any) -> str:
    value = getattr(status, "value", status)
    return str(value).strip().lower()


def phase_for_network_operation(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "pending": ControlPlanePhase.queued,
            "running": ControlPlanePhase.applying,
            "waiting": ControlPlanePhase.readback_pending,
            "succeeded": ControlPlanePhase.verified,
            "warning": ControlPlanePhase.drifted,
            "failed": ControlPlanePhase.failed,
            "canceled": ControlPlanePhase.failed,
        },
        source="NetworkOperation",
    )


def phase_for_uisp_intent(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "staged": ControlPlanePhase.desired,
            "applying": ControlPlanePhase.applying,
            "pending_readback": ControlPlanePhase.readback_pending,
            "pending_observation": ControlPlanePhase.readback_pending,
            "verified": ControlPlanePhase.verified,
            "drifted": ControlPlanePhase.drifted,
            "manual_required": ControlPlanePhase.drifted,
            "failed": ControlPlanePhase.failed,
            "decommissioned": ControlPlanePhase.verified,
        },
        source="UISP intent",
    )


def phase_for_huawei_sync(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "synced": ControlPlanePhase.verified,
            "reconciling": ControlPlanePhase.applying,
            "out_of_sync": ControlPlanePhase.drifted,
        },
        source="Huawei reconcile",
    )


def phase_for_router_push(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "pending": ControlPlanePhase.queued,
            "running": ControlPlanePhase.applying,
            "pending_readback": ControlPlanePhase.readback_pending,
            "completed": ControlPlanePhase.verified,
            "partial_failure": ControlPlanePhase.drifted,
            "failed": ControlPlanePhase.failed,
            "rolled_back": ControlPlanePhase.failed,
        },
        source="RouterOS push",
    )


def phase_for_router_push_result(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "pending": ControlPlanePhase.queued,
            "running": ControlPlanePhase.applying,
            "pending_readback": ControlPlanePhase.readback_pending,
            "success": ControlPlanePhase.verified,
            "failed": ControlPlanePhase.failed,
            "skipped": ControlPlanePhase.failed,
        },
        source="RouterOS push result",
    )


def phase_for_provisioning_run(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "pending": ControlPlanePhase.queued,
            "running": ControlPlanePhase.applying,
            "success": ControlPlanePhase.verified,
            "failed": ControlPlanePhase.failed,
        },
        source="ProvisioningRun",
    )


def _project(
    status: Any,
    mapping: dict[str, ControlPlanePhase],
    *,
    source: str,
) -> ControlPlanePhase:
    value = _status_value(status)
    try:
        return mapping[value]
    except KeyError as exc:
        raise ControlPlaneContractError(f"Unknown {source} status '{value}'") from exc
