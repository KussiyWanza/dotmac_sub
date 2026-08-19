"""The typed cohort/cutover contract for the Sub Thin Shadow environment.

Closed models, closed vocabularies, no `Any`, no unshaped dictionary. Every
model refuses unknown keys (`extra="forbid"`) so an unrecognised field is read as
a typo rather than silently accepted as an extension — the failure mode this
guards against is a hand-edited manifest sprouting
`shadow_authority_override: true` and nothing noticing.

The rules encoded here are all one rule seen from different sides: **a module may
only claim what has actually happened to it.** Source presence is not a release,
a release is not an installation, an installation is not a comparison, and a
comparison is not authority. Each step up demands the evidence of the step below.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from app.shadow.identity import Digest, pinned_reference
from app.shadow.vocabulary import (
    AdoptionState,
    AuthorityMode,
    PersistencePlane,
    at_or_beyond,
)

#: A full 40-hex commit. Abbreviated revisions are refused: an abbreviation that
#: is unique today can become ambiguous as the repository grows, and a manifest
#: is read long after it is written.
GitRevision = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]

#: Non-blank prose. Used where the contract needs a human statement — a gate, a
#: rollback condition — and an empty string would satisfy the type while
#: recording nothing.
Statement = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]

#: A lowercase kebab-case code: a stable identifier, not a display label.
Code = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$")]


class ReleaseIdentity(BaseModel):
    """A published artifact, addressed by content.

    Both fields are kept because a reference is what a deployment *uses* and a
    digest is what it *is*; storing only the reference would leave nothing to
    compare a re-resolved reference against.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_ref: Statement
    digest: Digest

    @model_validator(mode="after")
    def _reference_must_pin_this_digest(self) -> ReleaseIdentity:
        pinned_reference(self.artifact_ref, expected=self.digest)
        return self


class BlockingPrerequisite(BaseModel):
    """Something that must happen elsewhere before this module may progress.

    Recorded as a typed pair rather than free prose so the same hold can be
    matched across modules (four of this cohort share `vendor-cp-platform-adoption`)
    and so a hold cannot be "resolved" by rewording it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: Code
    statement: Statement


class ComparisonGate(BaseModel):
    """What shadow output must equal before the module may be believed.

    `satisfied` is not a wish: it demands a reconciliation hash, because a gate
    that is satisfied without evidence is a comment.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: Statement
    reconciliation_hash: str | None = None
    satisfied: bool = False

    @model_validator(mode="after")
    def _satisfied_needs_evidence(self) -> ComparisonGate:
        if self.satisfied and not self.reconciliation_hash:
            raise ValueError(
                "a satisfied comparison gate must carry the reconciliation hash "
                "that satisfied it; without one the claim has no evidence"
            )
        if self.reconciliation_hash is not None:
            Digest.parse(self.reconciliation_hash)
        return self


class RetirementRatchet(BaseModel):
    """A two-directional count of legacy writers still standing (ADR-0018).

    `remaining` must equal `ceiling`. That makes the ratchet fail in *both*
    directions: a new legacy writer appearing pushes `remaining` above the
    ceiling, and retiring one pushes it below — which also fails, until the
    author lowers the ceiling in the same commit. A one-directional ratchet
    silently tolerates progress it never records, so the count drifts away from
    the reality it is supposed to pin.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    remaining: int
    ceiling: int

    @model_validator(mode="after")
    def _ratchet_is_two_directional(self) -> RetirementRatchet:
        if self.remaining < 0 or self.ceiling < 0:
            raise ValueError("a retirement ratchet cannot be negative")
        if self.remaining > self.ceiling:
            raise ValueError(
                f"{self.remaining} legacy writers remain but the ratchet ceiling "
                f"is {self.ceiling} — a new displaced writer appeared and was "
                "not reviewed"
            )
        if self.remaining < self.ceiling:
            raise ValueError(
                f"{self.remaining} legacy writers remain against a ceiling of "
                f"{self.ceiling} — lower the ceiling in the same commit that "
                "retires a writer, so the count cannot drift from reality"
            )
        return self


class DisplacedWriter(BaseModel):
    """A Sub symbol a module would take over from, and its retirement ratchet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sub_writer: Statement
    ratchet: RetirementRatchet


class ModuleEntry(BaseModel):
    """One independently versioned owner, and exactly what it may claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module: Code
    package: Code
    contract_version: Statement
    source_revision: GitRevision
    persistence_plane: PersistencePlane
    adoption_state: AdoptionState
    authority_mode: AuthorityMode
    release: ReleaseIdentity | None
    blocking_prerequisite: BlockingPrerequisite | None
    comparison_gate: ComparisonGate
    rollback_condition: Statement
    displaced_writers: tuple[DisplacedWriter, ...]

    @model_validator(mode="after")
    def _claim_must_match_evidence(self) -> ModuleEntry:
        # Checked first, and separately, because "the source is in the tree" is
        # the single most likely thing to be mistaken for adoption.
        if (
            at_or_beyond(self.adoption_state, AdoptionState.RELEASED_UNCOMPOSED)
            and self.release is None
        ):
            raise ValueError(
                f"{self.module} claims {self.adoption_state.value} with no "
                "release identity — source presence is not a release, and an "
                "unreleased module cannot be installed, compared or believed"
            )

        if (
            at_or_beyond(self.adoption_state, AdoptionState.COMPARED)
            and not self.comparison_gate.satisfied
        ):
            raise ValueError(
                f"{self.module} claims {self.adoption_state.value} while its "
                "comparison gate is unsatisfied"
            )

        # Authority mode and adoption state are two spellings of one fact, so
        # they must agree in both directions.
        holds_authority = self.authority_mode is AuthorityMode.SHADOW_AUTHORITY
        is_authoritative = self.adoption_state is AdoptionState.SHADOW_AUTHORITY
        if holds_authority is not is_authoritative:
            raise ValueError(
                f"{self.module} records adoption_state="
                f"{self.adoption_state.value} with authority_mode="
                f"{self.authority_mode.value}; shadow authority is either both "
                "or neither"
            )

        if is_authoritative and self.blocking_prerequisite is not None:
            raise ValueError(
                f"{self.module} claims shadow authority while blocking "
                f"prerequisite {self.blocking_prerequisite.code!r} stands"
            )
        return self


class CohortManifest(BaseModel):
    """The closed, versioned cohort. Shadow-scoped by its type.

    `environment` is `Literal["shadow"]`: there is no production manifest of this
    shape, and the type — not a validator — is what makes one unrepresentable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: Statement
    environment: Literal["shadow"]
    modules: tuple[ModuleEntry, ...]

    @model_validator(mode="after")
    def _identities_are_unique_and_authority_displaces_something(
        self,
    ) -> CohortManifest:
        seen_modules: set[str] = set()
        seen_packages: set[str] = set()
        seen_digests: dict[str, str] = {}

        for entry in self.modules:
            if entry.module in seen_modules:
                raise ValueError(
                    f"duplicate module {entry.module!r}: a module appears once, "
                    "so it cannot carry two different pins"
                )
            seen_modules.add(entry.module)

            if entry.package in seen_packages:
                raise ValueError(f"duplicate package {entry.package!r}")
            seen_packages.add(entry.package)

            if entry.release is not None:
                digest = str(entry.release.digest)
                if digest in seen_digests:
                    raise ValueError(
                        f"duplicate release digest {digest} shared by "
                        f"{seen_digests[digest]!r} and {entry.module!r}: two "
                        "distinct modules cannot be the same published bytes"
                    )
                seen_digests[digest] = entry.module

            # Authority with nothing displaced means the old writer is still
            # live, which is a parallel decision path rather than a cutover.
            if entry.authority_mode is AuthorityMode.SHADOW_AUTHORITY and not (
                entry.displaced_writers
            ):
                raise ValueError(
                    f"{entry.module} holds shadow authority but displaces no "
                    "Sub writer — name the writer it takes over from, or it is "
                    "a second writer rather than a cutover"
                )
        return self

    def by_module(self, module: str) -> ModuleEntry:
        """The entry for `module`, or `KeyError`."""
        for entry in self.modules:
            if entry.module == module:
                return entry
        raise KeyError(module)


__all__ = [
    "BlockingPrerequisite",
    "Code",
    "CohortManifest",
    "ComparisonGate",
    "DisplacedWriter",
    "GitRevision",
    "ModuleEntry",
    "ReleaseIdentity",
    "RetirementRatchet",
    "Statement",
]
