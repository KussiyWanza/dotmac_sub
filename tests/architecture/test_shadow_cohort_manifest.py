"""The shadow cohort manifest may not lie, and may not describe production.

`app/shadow/` is a *governance* artifact, not runtime code. It records what the
Sub Thin Shadow environment is entitled to claim about each reusable module in
the cohort, and it exists because the cheapest possible mistake in this
programme is to read "the source is in the tree" as "the module is adopted".

Every test here is paired with a sensitivity proof. A guard that only ever sees
conforming input passes for the wrong reason: it would keep passing if the
validator it is meant to exercise were deleted. So each rejection test is
followed by an acceptance test proving the same code path admits the legal
value, and the "honest state" sweeps assert against a *constructed* violation
rather than only over today's data.
"""

from __future__ import annotations

import typing
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.shadow import (
    ADOPTION_PROGRESSION,
    SHADOW_COHORT,
    AdoptionState,
    AuthorityMode,
    BlockingPrerequisite,
    CohortManifest,
    ComparisonGate,
    Digest,
    DisplacedWriter,
    ModuleEntry,
    PersistencePlane,
    ReleaseIdentity,
    RetirementRatchet,
)

# The 25 independently versioned owners this shadow environment represents.
# Spelled out here rather than derived from the manifest: a test that reads its
# expectation out of the object under test cannot notice a dropped module.
EXPECTED_OWNERS = frozenset(
    {
        "billing",
        "durable-timers",
        "collections",
        "orders",
        "subscriptions",
        "inbox",
        "sales",
        "surveys",
        "projects",
        "work-orders",
        "positioning",
        "web-analytics",
        "analytics",
        "campaigns",
        "assets",
        "fiber-plant",
        "inventory",
        "ipam",
        "network-access",
        "network-assurance",
        "network-control",
        "network-inventory",
        "network-observability",
        "network-topology",
        "pon-access",
    }
)

_SHA = "sha256:" + "a" * 64
_OTHER_SHA = "sha256:" + "b" * 64


def _release(digest: str = _SHA) -> ReleaseIdentity:
    return ReleaseIdentity(
        artifact_ref=f"forgejo.dotmac.local/dotmac/wheel@{digest}",
        digest=Digest.parse(digest),
    )


def _gate(satisfied: bool = True) -> ComparisonGate:
    return ComparisonGate(
        statement="shadow invoice totals equal Sub baseline totals for the fixture window",
        reconciliation_hash=("sha256:" + "c" * 64) if satisfied else None,
        satisfied=satisfied,
    )


def _entry(**overrides: Any) -> ModuleEntry:
    """A minimal *legal* entry. Overrides make exactly one thing wrong."""
    base: dict[str, Any] = {
        "module": "billing",
        "package": "dotmac-billing",
        "contract_version": "0.1.0a1",
        "source_revision": "0" * 40,
        "persistence_plane": PersistencePlane.TENANT,
        "adoption_state": AdoptionState.SOURCE_ONLY,
        "authority_mode": AuthorityMode.NONE,
        "release": None,
        "blocking_prerequisite": None,
        "comparison_gate": _gate(satisfied=False),
        "rollback_condition": "drop the shadow watermark; Sub baseline stays authoritative",
        "displaced_writers": (),
    }
    base.update(overrides)
    return ModuleEntry(**base)


# ── Production authority is structurally unrepresentable ────────────────────


def test_production_authority_is_not_a_representable_authority_mode() -> None:
    """The enum has no production member, so no entry can name one."""
    values = {mode.value for mode in AuthorityMode}
    offending = {v for v in values if "production" in v or "prod" in v}
    assert not offending, (
        f"AuthorityMode exposes {offending} — a shadow manifest that can spell "
        "production authority can grant it. Production authority must be "
        "absent from the vocabulary, not merely discouraged by a validator."
    )
    assert values == {"none", "observer", "shadow_authority"}


def test_an_entry_claiming_production_authority_is_refused() -> None:
    with pytest.raises(ValidationError):
        _entry(authority_mode="production_authority")


def test_the_production_authority_guard_still_admits_shadow_authority() -> None:
    """Sensitivity: the refusal above is about the *value*, not about all values."""
    entry = _entry(
        adoption_state=AdoptionState.SHADOW_AUTHORITY,
        authority_mode=AuthorityMode.SHADOW_AUTHORITY,
        release=_release(),
        comparison_gate=_gate(satisfied=True),
    )
    assert entry.authority_mode is AuthorityMode.SHADOW_AUTHORITY


def test_the_manifest_declares_itself_shadow_scoped() -> None:
    assert SHADOW_COHORT.environment == "shadow"
    with pytest.raises(ValidationError):
        CohortManifest(
            manifest_version=SHADOW_COHORT.manifest_version,
            environment="production",
            modules=SHADOW_COHORT.modules,
        )


# ── Duplicate identity ──────────────────────────────────────────────────────


def test_duplicate_module_names_are_rejected() -> None:
    dup = _entry()
    with pytest.raises(ValidationError, match="duplicate"):
        CohortManifest(manifest_version="1", environment="shadow", modules=(dup, dup))


def test_duplicate_release_artifact_digests_are_rejected() -> None:
    """Two distinct modules cannot be the same published bytes."""
    a = _entry(
        module="billing",
        package="dotmac-billing",
        adoption_state=AdoptionState.RELEASED_UNCOMPOSED,
        release=_release(_SHA),
    )
    b = _entry(
        module="orders",
        package="dotmac-orders",
        adoption_state=AdoptionState.RELEASED_UNCOMPOSED,
        release=_release(_SHA),
    )
    with pytest.raises(ValidationError, match="duplicate"):
        CohortManifest(manifest_version="1", environment="shadow", modules=(a, b))


def test_distinct_modules_with_distinct_digests_are_accepted() -> None:
    """Sensitivity: the duplicate guards reject duplication, not plurality."""
    a = _entry(
        module="billing",
        package="dotmac-billing",
        adoption_state=AdoptionState.RELEASED_UNCOMPOSED,
        release=_release(_SHA),
    )
    b = _entry(
        module="orders",
        package="dotmac-orders",
        adoption_state=AdoptionState.RELEASED_UNCOMPOSED,
        release=_release(_OTHER_SHA),
    )
    manifest = CohortManifest(
        manifest_version="1", environment="shadow", modules=(a, b)
    )
    assert len(manifest.modules) == 2


def test_modules_may_share_a_source_revision() -> None:
    """A monorepo cohort legitimately pins many modules to one commit.

    Recorded deliberately: "duplicate source pins rejected" must not be read as
    "one commit per module", which would make an honest monorepo snapshot
    unrepresentable and push authors toward inventing per-module revisions.
    Duplicate *identity* (name, published artifact) is what is refused.
    """
    rev = "1" * 40
    a = _entry(module="ipam", package="dotmac-ipam", source_revision=rev)
    b = _entry(module="fiber-plant", package="dotmac-fiber-plant", source_revision=rev)
    manifest = CohortManifest(
        manifest_version="1", environment="shadow", modules=(a, b)
    )
    assert {m.source_revision for m in manifest.modules} == {rev}


def test_a_source_revision_must_be_a_full_commit_sha() -> None:
    with pytest.raises(ValidationError):
        _entry(source_revision="9a5db5d")  # abbreviated: ambiguous over time


# ── Mutable tags ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ref",
    [
        "forgejo.dotmac.local/dotmac/wheel:latest",
        "forgejo.dotmac.local/dotmac/wheel:0.1.0a1",
        "ghcr.io/michaelayoade/dotmac_sub:main",
        "dotmac-billing",
    ],
)
def test_mutable_tags_are_rejected_as_release_identity(ref: str) -> None:
    with pytest.raises(ValidationError):
        ReleaseIdentity(artifact_ref=ref, digest=Digest.parse(_SHA))


def test_a_digest_pinned_reference_is_accepted() -> None:
    """Sensitivity: the tag guard admits the pinned form."""
    identity = _release()
    assert identity.artifact_ref.endswith(f"@{_SHA}")


def test_a_reference_pinning_a_different_digest_is_rejected() -> None:
    """Adjacent columns must address the same bytes."""
    with pytest.raises(ValidationError):
        ReleaseIdentity(
            artifact_ref=f"forgejo.dotmac.local/dotmac/wheel@{_OTHER_SHA}",
            digest=Digest.parse(_SHA),
        )


# ── State progression honesty ───────────────────────────────────────────────


def test_the_progression_is_closed_and_ordered() -> None:
    assert ADOPTION_PROGRESSION == (
        AdoptionState.SOURCE_ONLY,
        AdoptionState.RELEASED_UNCOMPOSED,
        AdoptionState.INSTALLED_SHADOW,
        AdoptionState.COMPARED,
        AdoptionState.SHADOW_AUTHORITY,
    )
    assert set(ADOPTION_PROGRESSION) == set(AdoptionState)


@pytest.mark.parametrize(
    "state",
    [
        AdoptionState.RELEASED_UNCOMPOSED,
        AdoptionState.INSTALLED_SHADOW,
        AdoptionState.COMPARED,
        AdoptionState.SHADOW_AUTHORITY,
    ],
)
def test_a_state_beyond_source_only_requires_a_release_identity(
    state: AdoptionState,
) -> None:
    """Source presence is not a release. This is the central honesty rule."""
    with pytest.raises(ValidationError, match="release"):
        _entry(adoption_state=state, release=None, comparison_gate=_gate(True))


def test_released_state_is_accepted_once_a_release_identity_exists() -> None:
    """Sensitivity: the rule demands a release, it does not forbid the state."""
    entry = _entry(adoption_state=AdoptionState.RELEASED_UNCOMPOSED, release=_release())
    assert entry.adoption_state is AdoptionState.RELEASED_UNCOMPOSED


@pytest.mark.parametrize(
    "state", [AdoptionState.COMPARED, AdoptionState.SHADOW_AUTHORITY]
)
def test_comparison_states_require_a_satisfied_comparison_gate(
    state: AdoptionState,
) -> None:
    with pytest.raises(ValidationError, match="comparison"):
        _entry(
            adoption_state=state,
            release=_release(),
            comparison_gate=_gate(satisfied=False),
        )


def test_a_satisfied_gate_must_carry_a_reconciliation_hash() -> None:
    with pytest.raises(ValidationError, match="reconciliation"):
        ComparisonGate(
            statement="totals match", reconciliation_hash=None, satisfied=True
        )


def test_shadow_authority_requires_the_matching_authority_mode() -> None:
    """`shadow_authority` state and `observer` mode is a contradiction."""
    with pytest.raises(ValidationError):
        _entry(
            adoption_state=AdoptionState.SHADOW_AUTHORITY,
            authority_mode=AuthorityMode.OBSERVER,
            release=_release(),
            comparison_gate=_gate(True),
        )


def test_authority_mode_shadow_authority_requires_the_terminal_state() -> None:
    with pytest.raises(ValidationError):
        _entry(
            adoption_state=AdoptionState.INSTALLED_SHADOW,
            authority_mode=AuthorityMode.SHADOW_AUTHORITY,
            release=_release(),
        )


def test_a_blocked_module_cannot_claim_shadow_authority() -> None:
    with pytest.raises(ValidationError, match="blocking"):
        _entry(
            adoption_state=AdoptionState.SHADOW_AUTHORITY,
            authority_mode=AuthorityMode.SHADOW_AUTHORITY,
            release=_release(),
            comparison_gate=_gate(True),
            blocking_prerequisite=BlockingPrerequisite(
                code="vendor-cp-platform-adoption",
                statement="Vendor CP must adopt the platform plane first",
            ),
        )


def test_an_unblocked_module_may_reach_shadow_authority() -> None:
    """Sensitivity: the block is what stops it, not the terminal state itself."""
    entry = _entry(
        adoption_state=AdoptionState.SHADOW_AUTHORITY,
        authority_mode=AuthorityMode.SHADOW_AUTHORITY,
        release=_release(),
        comparison_gate=_gate(True),
        blocking_prerequisite=None,
    )
    assert entry.adoption_state is AdoptionState.SHADOW_AUTHORITY


def test_every_entry_states_a_rollback_condition() -> None:
    with pytest.raises(ValidationError):
        _entry(rollback_condition="  ")


# ── Two-directional retirement ratchets ─────────────────────────────────────


def test_a_ratchet_pins_its_own_ceiling() -> None:
    """Two-directional: a rise fails, and a fall fails until the ceiling moves."""
    assert RetirementRatchet(remaining=3, ceiling=3).remaining == 3
    with pytest.raises(ValidationError):
        RetirementRatchet(remaining=4, ceiling=3)  # a new legacy writer appeared
    with pytest.raises(ValidationError):
        RetirementRatchet(remaining=2, ceiling=3)  # retired, but ceiling not lowered


def test_a_displaced_writer_names_the_sub_symbol_it_replaces() -> None:
    writer = DisplacedWriter(
        sub_writer="app.services.billing.invoicing.create_invoice",
        ratchet=RetirementRatchet(remaining=1, ceiling=1),
    )
    assert writer.sub_writer.startswith("app.")
    with pytest.raises(ValidationError):
        DisplacedWriter(
            sub_writer="", ratchet=RetirementRatchet(remaining=1, ceiling=1)
        )


def test_a_module_holding_shadow_authority_must_displace_a_writer() -> None:
    """Authority with nothing displaced means the old writer is still live."""
    with pytest.raises(ValidationError, match="displace"):
        CohortManifest(
            manifest_version="1",
            environment="shadow",
            modules=(
                _entry(
                    adoption_state=AdoptionState.SHADOW_AUTHORITY,
                    authority_mode=AuthorityMode.SHADOW_AUTHORITY,
                    release=_release(),
                    comparison_gate=_gate(True),
                    displaced_writers=(),
                ),
            ),
        )


# ── The real cohort, and what it is allowed to claim today ──────────────────


def test_the_cohort_covers_exactly_the_declared_owners() -> None:
    assert {m.module for m in SHADOW_COHORT.modules} == EXPECTED_OWNERS


def test_no_cohort_module_claims_authority_it_has_not_earned() -> None:
    """Today every module is source-only or released; none is authoritative.

    This is a *ratchet*, not a permanent truth: when a module genuinely reaches
    `installed_shadow`, this test is the place that must be consciously updated,
    which is exactly the review moment the programme needs.
    """
    overclaiming = {
        m.module: m.adoption_state.value
        for m in SHADOW_COHORT.modules
        if m.adoption_state
        not in (AdoptionState.SOURCE_ONLY, AdoptionState.RELEASED_UNCOMPOSED)
    }
    assert not overclaiming, (
        f"{overclaiming} claim more than source/release state. Nothing in this "
        "cohort is installed into the shadow image: the shadow app runs the "
        "pinned Sub baseline, which contains none of these packages."
    )


def test_no_cohort_module_holds_any_authority_mode_yet() -> None:
    holding = {
        m.module: m.authority_mode.value
        for m in SHADOW_COHORT.modules
        if m.authority_mode is not AuthorityMode.NONE
    }
    assert not holding, f"{holding} claim an authority mode before comparison"


def test_production_hold_modules_record_their_blocking_prerequisite() -> None:
    """The production constraints named in the handoff must be recorded, not lost."""
    required = {
        "billing": "vendor-cp-platform-adoption",
        "subscriptions": "vendor-cp-platform-adoption",
        "analytics": "erp-first-adopter",
        "web-analytics": "backoffice-first-adopter",
        "positioning": "positioning-production-adoption-hold",
    }
    by_module = {m.module: m for m in SHADOW_COHORT.modules}
    for module, code in required.items():
        prerequisite = by_module[module].blocking_prerequisite
        assert prerequisite is not None, f"{module} must record its production hold"
        assert prerequisite.code == code


def test_every_cohort_module_states_a_rollback_condition_and_gate() -> None:
    for module in SHADOW_COHORT.modules:
        assert module.rollback_condition.strip()
        assert module.comparison_gate.statement.strip()


# ── Typed contracts ─────────────────────────────────────────────────────────

_FORBIDDEN_ANNOTATIONS = ("Any", "object", "dict[str, Any]", "Dict[str, Any]")


def _public_models() -> list[type[BaseModel]]:
    import app.shadow as shadow

    return [
        obj
        for name in shadow.__all__
        if isinstance(obj := getattr(shadow, name), type) and issubclass(obj, BaseModel)
    ]


def test_the_public_surface_exposes_at_least_the_contract_models() -> None:
    """Sensitivity: the sweep below is worthless over an empty set."""
    names = {model.__name__ for model in _public_models()}
    assert {
        "CohortManifest",
        "ModuleEntry",
        "ReleaseIdentity",
        "ComparisonGate",
        "RetirementRatchet",
        "DisplacedWriter",
        "BlockingPrerequisite",
    } <= names


def test_no_public_contract_field_is_untyped() -> None:
    offenders: list[str] = []
    for model in _public_models():
        hints = typing.get_type_hints(model)
        for field, annotation in hints.items():
            if annotation is Any or annotation is object:
                offenders.append(f"{model.__name__}.{field}")
            rendered = str(annotation)
            if "typing.Any" in rendered:
                offenders.append(f"{model.__name__}.{field}: {rendered}")
    assert not offenders, (
        f"untyped public contract fields: {offenders} — a shadow manifest whose "
        "inputs are unshaped cannot refuse anything"
    )


def test_no_public_contract_field_is_a_bare_mapping() -> None:
    offenders: list[str] = []
    for model in _public_models():
        for field, annotation in typing.get_type_hints(model).items():
            rendered = str(annotation)
            if any(
                bad in rendered
                for bad in ("dict[str, typing.Any]", "Mapping[str, typing.Any]")
            ):
                offenders.append(f"{model.__name__}.{field}: {rendered}")
    assert not offenders, f"unshaped dict fields: {offenders}"


def test_contract_models_refuse_unknown_fields() -> None:
    """A closed manifest: an unrecognised key is a typo, not an extension."""
    with pytest.raises(ValidationError):
        _entry(shadow_authority_override=True)
