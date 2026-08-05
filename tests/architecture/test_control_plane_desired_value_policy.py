"""Unset desired state cannot become an executable device value.

``network.control_plane_intent`` owns the rule; providers register their own
sentinel tables and enforce the ruling on every delivery path. These guards pin
the boundary itself — that the policy has exactly one home, that the Huawei ONT
provider routes its decision through it rather than reimplementing it, and that
enforcement covers planning *and* applying, because a legacy caller can build
an action directly.

Behavioural coverage lives in ``tests/test_reconcile_sentinels.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.control_plane_intent import (
    ControlPlaneContractError,
    DesiredValueAdjudication,
    DesiredValueAuthority,
    DesiredValueDeclaration,
    DesiredValueProvenance,
    has_executable_desired_provenance,
    is_executable_desired_value,
)
from app.services.network.reconcile.sentinels import (
    RULES,
    authority_debt_baseline,
    rules_by_authority,
)
from app.services.sot_relationships import DOMAIN_SOT_RELATIONSHIPS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_OWNER = PROJECT_ROOT / "app/services/control_plane_intent.py"
ONT_REGISTRY = PROJECT_ROOT / "app/services/network/reconcile/sentinels.py"
PLANNER = PROJECT_ROOT / "app/services/network/reconcile/planner.py"
APPLIER = PROJECT_ROOT / "app/services/network/reconcile/applier.py"
SWEEPER = PROJECT_ROOT / "app/services/network/reconcile/sweeper.py"
DETECTOR = PROJECT_ROOT / "scripts/network/ont_sentinel_blast_radius.py"
SOT_MAP = PROJECT_ROOT / "docs/SOT_RELATIONSHIP_MAP.md"

CONCERN = "unset desired-value admissibility policy"


def _service(name: str):
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            if service.name == name:
                return service
    raise AssertionError(f"{name} is not registered")


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


# ── Registration ────────────────────────────────────────────────────────────


def test_policy_is_a_registered_concern_of_the_control_plane_owner():
    service = _service("network.control_plane_intent")
    assert CONCERN in service.owns
    assert service.contract is not None, "the policy concern requires a contract"
    contracted = {concern.name for concern in service.contract.concerns}
    assert CONCERN in contracted


def test_policy_concern_declares_its_provider_input():
    service = _service("network.control_plane_intent")
    concern = next(c for c in service.contract.concerns if c.name == CONCERN)
    declared = {item.name for item in service.contract.authoritative_inputs}
    assert set(concern.input_names) <= declared
    assert concern.input_names, "the ruling must name the input it rules on"


def test_policy_fails_closed_on_an_undeclared_default():
    service = _service("network.control_plane_intent")
    assert "undeclared unset desired value" in service.contract.errors.fail_closed_on


def test_provenance_policy_fails_closed_on_unknown_or_untyped_input():
    assert has_executable_desired_provenance(DesiredValueProvenance.explicit)
    assert has_executable_desired_provenance(DesiredValueProvenance.declared_default)
    assert not has_executable_desired_provenance(DesiredValueProvenance.unknown)
    assert not has_executable_desired_provenance("explicit")  # type: ignore[arg-type]


def test_design_rule_is_checked_in():
    # Normalised: the rule is prose in a wrapped document, so line breaks must
    # not decide whether the guard passes.
    text = " ".join(SOT_MAP.read_text(encoding="utf-8").split())
    assert "Unset desired state is not an executable device value" in text
    assert (
        "must remain typed as unknown and cannot become an executable device "
        "value unless a named owner explicitly declares that default" in text
    )


# ── One home for the rule ───────────────────────────────────────────────────


def test_the_ont_provider_routes_its_decision_through_the_policy_owner():
    """The registry supplies sentinel and disposition; it does not decide."""
    assert "is_executable_desired_value" in _imported_names(ONT_REGISTRY)


def test_no_other_module_reimplements_the_ruling():
    """Only the policy owner may define admissibility.

    A second implementation is how the rule quietly diverges per provider.
    """
    definers = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "is_executable_desired_value"
            ):
                definers.append(path)
    assert definers == [POLICY_OWNER], definers


def test_the_policy_owner_stays_free_of_provider_vocabulary():
    """The rule must not learn any vendor's field names."""
    text = POLICY_OWNER.read_text(encoding="utf-8")
    for vendor_term in ("wifi_ssid", "line_profile_id", "OntUnit", "AcsSet"):
        assert vendor_term not in text, vendor_term


# ── Enforcement on every delivery path ──────────────────────────────────────


def test_every_refused_field_is_enforced_in_planner_and_applier():
    """Planning alone is not enough — legacy callers build actions directly."""
    planner = PLANNER.read_text(encoding="utf-8")
    applier = APPLIER.read_text(encoding="utf-8")
    for rule in rules_by_authority(DesiredValueAuthority.inadmissible):
        assert f'"{rule.field}"' in planner, f"{rule.field} unguarded in planner"
        assert f'"{rule.field}"' in applier, f"{rule.field} unguarded in applier"


def test_refusal_is_decided_before_device_contact():
    """The guard must precede the adapter call inside each applier branch."""
    tree = ast.parse(APPLIER.read_text(encoding="utf-8"))
    guarded_cases = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.match_case):
            continue
        calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_refuse_unset"
        ]
        if not calls:
            continue
        guarded_cases += 1
        first = node.body[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), (
            "the unset refusal must be the first statement in the branch"
        )
        assert first.value.func.id == "_refuse_unset"
    assert guarded_cases >= 5


# ── Audit and measurement obligations ───────────────────────────────────────


def test_every_registered_rule_carries_an_authority_and_an_impact():
    for rule in RULES:
        assert isinstance(rule.authority, DesiredValueAuthority), rule.field
        assert isinstance(rule.adjudication, DesiredValueAdjudication), rule.field
        assert rule.impact.strip(), rule.field
        assert rule.writes.strip(), rule.field


# ── Authority debt is bounded and shrink-only ───────────────────────────────


def test_no_undeclared_default_escapes_the_debt_baseline():
    """A default that executes with no owner behind it must be named.

    Without this the ``undeclared`` authority would be exactly the permissive
    disposition it replaced — a way to keep executing while calling it pending.
    """
    undeclared = {
        rule.field for rule in rules_by_authority(DesiredValueAuthority.undeclared)
    }
    added = sorted(undeclared - authority_debt_baseline())
    assert not added, (
        "new undeclared defaults may not be added to the authority debt:\n  "
        + "\n  ".join(added)
        + "\nDeclare the default with its owner, or refuse it on every "
        "delivery path."
    )


def test_resolved_debt_is_removed_from_the_baseline():
    undeclared = {
        rule.field for rule in rules_by_authority(DesiredValueAuthority.undeclared)
    }
    stale = sorted(authority_debt_baseline() - undeclared)
    assert not stale, (
        "these fields are no longer undeclared; delete them from the "
        "shrink-only baseline:\n  " + "\n  ".join(stale)
    )


def test_debt_baseline_is_sorted_and_unique():
    entries = [
        line.strip()
        for line in (
            PROJECT_ROOT
            / "app/services/network/reconcile/desired_value_authority_debt.txt"
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert entries == sorted(set(entries))


# ── Delegation names a real owner and does not double-guard ─────────────────


def test_named_authority_entries_name_a_registered_owner():
    registered = {
        service.name
        for domain in DOMAIN_SOT_RELATIONSHIPS
        for service in domain.services
    }
    named_authorities = (
        DesiredValueAuthority.delegated,
        DesiredValueAuthority.declared_default,
    )
    for authority in named_authorities:
        for rule in rules_by_authority(authority):
            assert rule.declared_by in registered, rule.field


def test_delegated_fields_are_not_guarded_in_this_provider():
    """Two owners refusing the same value independently is how they diverge."""
    planner = PLANNER.read_text(encoding="utf-8")
    applier = APPLIER.read_text(encoding="utf-8")
    for rule in rules_by_authority(DesiredValueAuthority.delegated):
        assert f'is_deliverable("{rule.field}"' not in planner, rule.field
        assert f'is_deliverable("{rule.field}"' not in applier, rule.field
        assert f'("{rule.field}",' not in applier, rule.field


def test_detector_and_sweeper_share_one_candidate_query():
    """A detector with its own query reports a different fleet than the sweep."""
    detector = DETECTOR.read_text(encoding="utf-8")
    assert "sweep_candidates" in detector
    assert "def sweep_candidates" in SWEEPER.read_text(encoding="utf-8")
    assert "def sweep_candidates" not in detector


def test_unmeasurable_rules_are_reported_as_unmeasured():
    """A fake zero is the failure mode the audit exists to prevent."""
    detector = DETECTOR.read_text(encoding="utf-8")
    assert "UNMEASURED" in detector
    assert any(not rule.measurable for rule in RULES), (
        "no unmeasurable rule left to prove the reporting path"
    )


# ── The ruling itself ───────────────────────────────────────────────────────


def _declaration(**overrides) -> DesiredValueDeclaration:
    defaults = dict(
        field="probe",
        sentinel="",
        authority=DesiredValueAuthority.inadmissible,
        adjudication=DesiredValueAdjudication.refused,
    )
    defaults.update(overrides)
    return DesiredValueDeclaration(**defaults)


def test_inadmissible_blocks_only_the_declared_sentinel():
    assert not is_executable_desired_value("", declaration=_declaration())
    assert is_executable_desired_value("KURSI", declaration=_declaration())


def test_declared_default_executes():
    assert is_executable_desired_value(
        "",
        declaration=_declaration(
            authority=DesiredValueAuthority.declared_default,
            adjudication=DesiredValueAdjudication.approved,
            declared_by="network.control_plane_intent",
        ),
    )


def test_undecided_cannot_claim_execution_authority():
    """Blocker 2: review status must not be able to grant execution."""
    with pytest.raises(ControlPlaneContractError, match="only an approved default"):
        _declaration(
            authority=DesiredValueAuthority.declared_default,
            adjudication=DesiredValueAdjudication.undecided,
            declared_by="network.control_plane_intent",
        )


def test_an_authority_without_a_name_is_rejected():
    with pytest.raises(ControlPlaneContractError, match="without naming an owner"):
        _declaration(authority=DesiredValueAuthority.delegated)


def test_boolean_is_never_matched_against_a_numeric_sentinel():
    """``False == 0`` in Python; a disabled flag is not an unset profile id."""
    assert is_executable_desired_value(False, declaration=_declaration(sentinel=0))
    assert is_executable_desired_value(0, declaration=_declaration(sentinel=False))
