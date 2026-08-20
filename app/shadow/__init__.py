"""Sub Thin Shadow — the typed cohort/cutover contract for a disposable environment.

This package is **governance, not runtime**. Nothing here is imported by Sub's
request path, and nothing here decides anything about Sub. It exists to answer
one question truthfully and mechanically:

    for each reusable module in the cohort, what has *actually* happened to it?

## Why it lives under `app/`

`make check` in this repository points ruff, mypy and bandit at `app/` only.
Placing a typed contract anywhere else would leave the rule it enforces —
"public inputs and outputs are strongly typed, never `Any` or an unshaped dict"
— unenforced by the very gate that is supposed to prove it. Living here means
the contract is checked by the existing gates with no Makefile change.

## What the shadow environment is, and is not

It is a disposable, egress-denied, synthetic-data stack (`/opt/dotmac-sub-thin-shadow`,
compose project `dotmac-sub-thin-shadow`, loopback `127.0.0.1:18001`) in which
module authority may be switched over *synthetic* data.

It is **not** a second live financial authority, and it satisfies **no**
production cutover gate. `AuthorityMode` has no production member and
`CohortManifest.environment` is `Literal["shadow"]`, so neither of those claims
is expressible in this contract rather than merely discouraged by it.

## Reading the states

`source_only → released_uncomposed → installed_shadow → compared → shadow_authority`.

Each step demands the evidence of the one below: a state past `source_only`
requires a digest-pinned release identity, `compared` and beyond require a
satisfied comparison gate carrying its reconciliation hash, and shadow authority
requires that no blocking prerequisite stands and that some Sub writer is named
as displaced. Today every module sits at `source_only` — see `cohort` for why
that is the accurate reading and not an unfinished draft.
"""

from __future__ import annotations

from app.shadow.cohort import (
    COHORT_REVISION,
    NETWORK_SNAPSHOT_REVISION,
    SHADOW_COHORT,
)
from app.shadow.compose_contract import (
    SHADOW_BIND_HOST,
    SHADOW_BIND_PORT,
    SHADOW_PROJECT,
    ShadowComposeFile,
    ShadowNetwork,
    ShadowService,
    ShadowVolume,
    contract_violations,
    parse_compose,
)
from app.shadow.identity import (
    SHA256,
    ArtifactIdentityError,
    Digest,
    DigestError,
    UnpinnedReferenceError,
    pinned_reference,
)
from app.shadow.manifest import (
    BlockingPrerequisite,
    CohortManifest,
    ComparisonGate,
    DisplacedWriter,
    ModuleEntry,
    ReleaseIdentity,
    RetirementRatchet,
)
from app.shadow.vocabulary import (
    ADOPTION_PROGRESSION,
    AdoptionState,
    AuthorityMode,
    PersistencePlane,
    at_or_beyond,
    progression_index,
)

__all__ = [
    "ADOPTION_PROGRESSION",
    "COHORT_REVISION",
    "NETWORK_SNAPSHOT_REVISION",
    "SHA256",
    "SHADOW_BIND_HOST",
    "SHADOW_BIND_PORT",
    "SHADOW_COHORT",
    "SHADOW_PROJECT",
    "AdoptionState",
    "ArtifactIdentityError",
    "AuthorityMode",
    "BlockingPrerequisite",
    "CohortManifest",
    "ComparisonGate",
    "Digest",
    "DigestError",
    "DisplacedWriter",
    "ModuleEntry",
    "PersistencePlane",
    "ReleaseIdentity",
    "RetirementRatchet",
    "ShadowComposeFile",
    "ShadowNetwork",
    "ShadowService",
    "ShadowVolume",
    "UnpinnedReferenceError",
    "at_or_beyond",
    "contract_violations",
    "parse_compose",
    "pinned_reference",
    "progression_index",
]
