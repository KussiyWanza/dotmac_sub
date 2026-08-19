"""Closed vocabularies for the shadow cohort.

Each of these is an enum rather than a validated string because the set of legal
values is the *contract*, not a convention. The important one is
`AuthorityMode`: production authority is absent from it, so a shadow manifest
cannot spell production authority even by mistake. That is a structural
property — deleting a validator cannot restore the ability to say it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class AdoptionState(StrEnum):
    """How far a module has actually travelled — never how far it could.

    The whole progression exists to keep "the source is in the tree" from being
    read as "the module is adopted". Source presence is `SOURCE_ONLY` and
    nothing more.
    """

    #: Code exists on a pinned revision. Nothing is published or installed.
    SOURCE_ONLY = "source_only"
    #: A digest-pinned artifact exists, but no assembly composes it.
    RELEASED_UNCOMPOSED = "released_uncomposed"
    #: Installed into the shadow assembly, migrated, reading synthetic data.
    INSTALLED_SHADOW = "installed_shadow"
    #: Shadow output reconciles against the Sub baseline for a fixture window.
    COMPARED = "compared"
    #: Authoritative over synthetic shadow data only.
    SHADOW_AUTHORITY = "shadow_authority"


class AuthorityMode(StrEnum):
    """What a module is allowed to decide.

    There is deliberately no production member. A shadow environment satisfies
    no production cutover gate, so the vocabulary it is described with must not
    contain a value that would let a later editor record one.
    """

    #: Decides nothing; present for inventory only.
    NONE = "none"
    #: Computes alongside the baseline and writes only comparison output.
    OBSERVER = "observer"
    #: Authoritative over synthetic shadow data, behind a shadow watermark.
    SHADOW_AUTHORITY = "shadow_authority"


class PersistencePlane(StrEnum):
    """Which declared plane a module's tables live in (ADR-0023).

    Declared, never inferred from a missing tenant column.
    """

    #: `tenant_id NOT NULL`, FORCEd RLS.
    TENANT = "tenant"
    #: Control plane: no tenant column, REVOKEd from the tenant app role.
    PLATFORM = "platform"
    #: One behaviour, both declared planes, no FK across them.
    DUAL = "dual"


#: The only legal order of travel. Exported so callers compare by position
#: rather than re-encoding the sequence at each site.
ADOPTION_PROGRESSION: Final[tuple[AdoptionState, ...]] = (
    AdoptionState.SOURCE_ONLY,
    AdoptionState.RELEASED_UNCOMPOSED,
    AdoptionState.INSTALLED_SHADOW,
    AdoptionState.COMPARED,
    AdoptionState.SHADOW_AUTHORITY,
)


def progression_index(state: AdoptionState) -> int:
    """Position of `state` in `ADOPTION_PROGRESSION`."""
    return ADOPTION_PROGRESSION.index(state)


def at_or_beyond(state: AdoptionState, floor: AdoptionState) -> bool:
    """True when `state` has travelled at least as far as `floor`."""
    return progression_index(state) >= progression_index(floor)


__all__ = [
    "ADOPTION_PROGRESSION",
    "AdoptionState",
    "AuthorityMode",
    "PersistencePlane",
    "at_or_beyond",
    "progression_index",
]
