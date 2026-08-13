"""An adapter is identified by the registry, not by which directory it sits in.

`test_thin_wrappers` enforces "no direct queries in an adapter" over
`app/web` and `app/api`. The presenter layer those routes delegate to — 181
`app/services/web_*.py` modules, imported by 86 of the 130 `app/web` files —
is outside it, and holds several times the direct database access the checked
directories do.

A directory-scoped check therefore reports compliance while missing most of
what it is about. Per ADR-0010 in `dotmac_starter_mt`, that is worse than
having no rule: it converts an unknown into a false assurance.

**The rule: a module is a SERVICE when `app/services/sot_registry/` declares
it, and an undeclared `app/services/web_*.py` module is an ADAPTER.**

Registration rather than a naming convention, because Sub already has an
executable ownership registry that answers "who owns this decision?" — asking
it "is this a service?" adds no second source to keep in sync, and a module
that genuinely owns a decision is declared there anyway. Twenty-five `web_*`
modules already are, and their direct access is legitimate service code.

The remaining debt is captured as a shrink-only baseline rather than fixed
here, deliberately. The value is making the number visible and bounded now;
the migration is per-module judgement, and some modules will resolve by being
DECLARED owners rather than by being thinned.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.architecture.sot_debt import declared_service_modules

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_GLOB = "app/services/web_*.py"
BASELINE = Path(__file__).with_name("adapter_identifiability_baseline.txt")

#: Direct database access — the same three shapes `test_thin_wrappers` forbids.
DISALLOWED = re.compile(r"\bdb\.query\(|\bdb\.execute\(|\bselect\(")

__all__ = ["declared_service_modules"]


def _module_path(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))[:-3].replace("/", ".")


def adapter_db_access() -> dict[str, int]:
    """Undeclared `web_*` modules, and how much direct access each still has."""

    declared = declared_service_modules()
    counts: dict[str, int] = {}
    for path in sorted(PROJECT_ROOT.glob(ADAPTER_GLOB)):
        if _module_path(path) in declared:
            continue
        hits = len(DISALLOWED.findall(path.read_text(encoding="utf-8")))
        if hits:
            counts[str(path.relative_to(PROJECT_ROOT))] = hits
    return counts


def _baseline() -> dict[str, int]:
    allowed: dict[str, int] = {}
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, _, count = line.rpartition(" ")
        allowed[path] = int(count)
    return allowed


def test_the_registry_actually_declares_services() -> None:
    """Guard the guard.

    If the declaration regex stopped matching, every `web_*` module would look
    like an adapter, the baseline would look enormous, and the natural response
    would be to widen the baseline rather than notice the detector broke.
    """

    declared = declared_service_modules()
    assert len(declared) > 50, (
        f"only {len(declared)} declared service modules found; the SOT registry "
        "declaration format has probably changed and this guard is measuring "
        "the wrong thing"
    )
    assert any(module.startswith("app.services.web_") for module in declared), (
        "no web_* module is declared a service; the split this guard depends on "
        "has collapsed"
    )


def test_no_new_direct_database_access_in_an_undeclared_adapter() -> None:
    allowed = _baseline()
    current = adapter_db_access()

    grew = {
        path: (allowed.get(path, 0), count)
        for path, count in current.items()
        if count > allowed.get(path, 0)
    }
    assert not grew, (
        "an undeclared app/services/web_*.py module gained direct database "
        "access. It is an adapter until the SOT registry declares it: move the "
        "logic to its owning service, or declare the module an owner in "
        "app/services/sot_registry/ if it genuinely owns the decision. Never "
        "widen the baseline:\n  "
        + "\n  ".join(
            f"{path}: {before} -> {after}"
            for path, (before, after) in sorted(grew.items())
        )
    )


def test_adapter_identifiability_baseline_only_shrinks() -> None:
    allowed = _baseline()
    current = adapter_db_access()

    resolved = {
        path: (count, current.get(path, 0))
        for path, count in allowed.items()
        if current.get(path, 0) < count
    }
    assert not resolved, (
        "adapter database-access debt was resolved; reduce or remove these "
        "baseline entries so the repair is permanent:\n  "
        + "\n  ".join(
            f"{path}: {before} -> {after}"
            for path, (before, after) in sorted(resolved.items())
        )
    )
