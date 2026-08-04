"""One owner for the reconciliation population.

The eligibility predicate had been copied into every caller that needed it.
Copies of a population rule do not stay equal, and the divergence is silent:
each caller keeps answering a slightly different question about which customer
devices an automatic process may drive.

This guard fails the build when a new copy appears, which a behaviour test
cannot do -- a duplicate reads identically until the day someone edits one of
them.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
CANONICAL = APP / "services" / "network" / "reconcile" / "candidates.py"
HELPER = "restrict_to_reconcile_candidates"

# `ont_runtime_status` answers a different question: which **OLTs** the native
# Huawei bulk status collector owns, expressed as `huawei_olt_status_pollable`
# and already single-sourced within that module. It shares the four OLT-side
# conditions but carries none of the ONT-side ones, and it is an observation
# path rather than a delivery path -- polling a device's status is not driving
# its configuration.
#
# Whether "an OLT Dotmac's native Huawei tooling owns" should become one shared
# concept behind both is a real question, but coupling reconciliation's
# population to the collector's ownership rule would let a change in one
# silently move the other. Left as a deliberate, documented exemption pending
# that decision rather than resolved by inference.
EXEMPT = {APP / "services" / "network" / "ont_runtime_status.py"}

# The vendor test is the predicate's fingerprint: it is the one condition that
# cannot be arrived at incidentally.
VENDOR_ATTR = "vendor"


def _python_files() -> list[Path]:
    return [p for p in APP.rglob("*.py") if p.is_file()]


def _mentions_olt_vendor_comparison(tree: ast.AST) -> bool:
    """True when the module compares an OLTDevice vendor to a literal."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        source = ast.dump(node)
        if (
            f"attr='{VENDOR_ATTR}'" in source
            and "OLTDevice" in source
            and "huawei" in source.lower()
        ):
            return True
    return False


def test_only_the_canonical_module_defines_the_population() -> None:
    offenders = []
    for path in _python_files():
        if path == CANONICAL or path in EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        if _mentions_olt_vendor_comparison(tree):
            offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, (
        "These modules restate the ONT reconciliation eligibility predicate "
        f"instead of consuming {HELPER}: {offenders}. A second definition of "
        "which devices may be driven automatically will drift from the first."
    )


def test_the_sweeper_consumes_the_canonical_predicate() -> None:
    source = (APP / "services" / "network" / "reconcile" / "sweeper.py").read_text(
        encoding="utf-8"
    )

    assert HELPER in source


def test_the_expired_remote_access_cleanup_consumes_the_canonical_predicate() -> None:
    """It grants access on devices reconciliation owns, so it shares the set.

    These two ran off separate copies of the predicate while the fleet-wide
    control coupled them, which is exactly the pairing that must not drift.
    """
    source = (APP / "tasks" / "ont_reconcile.py").read_text(encoding="utf-8")

    assert HELPER in source


def test_the_canonical_module_exports_the_helper() -> None:
    tree = ast.parse(CANONICAL.read_text(encoding="utf-8"))
    exported = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    assert HELPER in exported
