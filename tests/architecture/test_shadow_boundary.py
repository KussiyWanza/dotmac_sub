"""`app/shadow` is governance. It must not become runtime, and it must not lie.

Two families of guard:

**Naming.** A displaced writer is only meaningful if it names a Sub service that
actually exists. Free prose here would rot into a list of plausible-looking
strings that match nothing, and the retirement ratchets counting them would be
counting fiction. So every `sub_writer` is checked against Sub's own SOT
registry.

**Direction.** This package reads Sub's registry; nothing in Sub reads this
package. The moment a request path imports `app.shadow`, a shadow manifest is
deciding something in production — which is the precise failure the manifest was
written to make impossible. It also holds no database session, HTTP client or
credential: it decides nothing and reaches nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.sot_registry.registry import all_services
from app.shadow import SHADOW_COHORT

ROOT = Path(__file__).resolve().parents[2]
SHADOW_DIR = ROOT / "app/shadow"


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _shadow_sources() -> list[Path]:
    return sorted(SHADOW_DIR.glob("*.py"))


# ── Displaced writers name real Sub services ────────────────────────────────


def test_every_displaced_writer_names_a_registered_sub_sot_service() -> None:
    registered = {service.name for service in all_services()}
    unknown = {
        writer.sub_writer
        for module in SHADOW_COHORT.modules
        for writer in module.displaced_writers
        if writer.sub_writer not in registered
    }
    assert not unknown, (
        f"{sorted(unknown)} are not registered SOT services. A displaced writer "
        "that names nothing real makes its retirement ratchet a count of fiction."
    )


def test_the_registry_check_would_notice_a_fabricated_writer() -> None:
    """Sensitivity: the set above is real and does not contain invented names."""
    registered = {service.name for service in all_services()}
    assert registered, "SOT registry resolved to nothing; the check above is vacuous"
    assert "financial.invoices" in registered
    assert "financial.definitely_not_a_real_service" not in registered


def test_at_least_one_module_actually_declares_a_displaced_writer() -> None:
    """Sensitivity: a cohort with no writers anywhere would pass the sweep."""
    declared = sum(len(m.displaced_writers) for m in SHADOW_COHORT.modules)
    assert declared >= 20, f"only {declared} displaced writers recorded"


def test_modules_with_no_sub_owner_declare_no_writer() -> None:
    """`projects` displaces nothing because Sub declares no project owner."""
    assert SHADOW_COHORT.by_module("projects").displaced_writers == ()


# ── Direction: shadow reads Sub, Sub does not read shadow ───────────────────


def test_the_shadow_package_imports_no_sub_runtime_module() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _shadow_sources():
        bad = {
            module
            for module in _imported_modules(path.read_text(encoding="utf-8"))
            if module.startswith(("app.services", "app.models", "app.api", "app.web"))
        }
        if bad:
            offenders[path.name] = bad
    assert not offenders, (
        f"{offenders}: app/shadow is a governance record, not a participant in "
        "Sub's request path"
    )


def test_the_import_scanner_detects_a_constructed_runtime_import() -> None:
    """Sensitivity: prove the scanner above can actually see such an import."""
    detected = _imported_modules("from app.services.billing import invoicing\n")
    assert "app.services.billing" in detected


def test_nothing_under_app_imports_the_shadow_package() -> None:
    offenders: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        if SHADOW_DIR in path.parents or path.parent == SHADOW_DIR:
            continue
        try:
            modules = _imported_modules(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        if any(module.startswith("app.shadow") for module in modules):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        f"{offenders} import app.shadow — a shadow manifest must never be "
        "consulted by production code"
    )


def test_the_reverse_import_scanner_is_not_vacuous() -> None:
    """Sensitivity: the sweep above visits a non-trivial number of files."""
    visited = [
        path
        for path in (ROOT / "app").rglob("*.py")
        if path.parent != SHADOW_DIR and SHADOW_DIR not in path.parents
    ]
    assert len(visited) > 100, f"only {len(visited)} files swept"
    assert "app.shadow" in _imported_modules("import app.shadow\n")


# ── The package reaches nothing ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    ["sqlalchemy", "httpx", "requests", "redis", "boto3", "celery", "paramiko"],
)
def test_the_shadow_package_holds_no_client_session_or_transport(
    forbidden: str,
) -> None:
    offenders = [
        path.name
        for path in _shadow_sources()
        if any(
            module.split(".")[0] == forbidden
            for module in _imported_modules(path.read_text(encoding="utf-8"))
        )
    ]
    assert not offenders, f"{offenders} import {forbidden}"


def test_the_shadow_package_is_not_empty() -> None:
    """Sensitivity: every sweep above is vacuous over an empty directory."""
    names = {path.name for path in _shadow_sources()}
    assert {
        "__init__.py",
        "cohort.py",
        "compose_contract.py",
        "identity.py",
        "manifest.py",
        "vocabulary.py",
    } <= names
