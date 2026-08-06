"""Architecture guardrails for declared source-of-truth owners.

The registry is an operational map, not merely documentation.  Every module it
names as an owner must be reachable from application or operator code.  A module
that is only imported by tests, or named by the registry itself, is not a live
owner.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from app.services import sot_relationships
from scripts.architecture import sot_debt

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
SCRIPT_DIR = PROJECT_ROOT / "scripts"
UNDECLARED_WRITER_BASELINE = Path(__file__).with_name("sot_writer_baseline.txt")

# Temporary debt only. Entries may be removed when an owner is wired or struck
# from the registry; adding an entry requires an explicit ownership decision.
KNOWN_DEAD_OWNERS: set[str] = set()


def _imported_modules(path: Path) -> set[str]:
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - syntax checks fail elsewhere
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


@cache
def _writer_modules() -> set[str]:
    return sot_debt.persistence_writer_modules()


@cache
def _declared_owner_modules() -> set[str]:
    return sot_debt.declared_owner_modules(sot_relationships.DOMAIN_SOT_RELATIONSHIPS)


@cache
def _undeclared_writer_baseline() -> set[str]:
    return sot_debt.read_name_baseline(UNDECLARED_WRITER_BASELINE)


@cache
def _import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for root in (APP_DIR, SCRIPT_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(PROJECT_ROOT)
            dotted = ".".join(rel.with_suffix("").parts)
            graph[dotted] = _imported_modules(path)
    return graph


def _has_real_caller(module: str, graph: dict[str, set[str]]) -> bool:
    for importer, imports in graph.items():
        if importer == module or importer.endswith("sot_relationships"):
            continue
        if module in imports:
            return True
    return False


def test_every_declared_owner_has_a_real_caller() -> None:
    graph = _import_graph()
    dead = [
        f"{service.name} -> {service.module}"
        for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS
        for service in domain.services
        if service.module not in KNOWN_DEAD_OWNERS
        and not _has_real_caller(service.module, graph)
    ]

    assert not dead, (
        "declared SOT owners that application and operator code never call. "
        "Wire each owner, or remove the false ownership claim:\n  "
        + "\n  ".join(sorted(dead))
    )


def test_the_dead_owner_list_only_shrinks() -> None:
    graph = _import_graph()
    resurrected = [
        module for module in KNOWN_DEAD_OWNERS if _has_real_caller(module, graph)
    ]
    assert not resurrected, (
        "owners now have callers; remove their liveness exemptions:\n  "
        + "\n  ".join(sorted(resurrected))
    )


def test_no_new_undeclared_persistence_writers() -> None:
    undeclared = _writer_modules() - _declared_owner_modules()
    new_writers = sorted(undeclared - _undeclared_writer_baseline())
    assert not new_writers, (
        "new persistence-writing service modules have no declared SOT owner. "
        "Register the canonical owner; update the debt baseline only after an "
        "explicit ownership review:\n  " + "\n  ".join(new_writers)
    )


def test_undeclared_writer_baseline_only_shrinks() -> None:
    undeclared = _writer_modules() - _declared_owner_modules()
    stale = sorted(_undeclared_writer_baseline() - undeclared)
    assert not stale, (
        "undeclared-writer debt was resolved or stopped writing; remove these "
        "entries from the shrink-only baseline:\n  " + "\n  ".join(stale)
    )


def test_no_module_is_declared_under_unexpected_owner_names() -> None:
    seen: dict[str, list[str]] = {}
    for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            seen.setdefault(service.module, []).append(service.name)

    duplicates = {module: names for module, names in seen.items() if len(names) > 1}
    known = {
        "app.services.domain_settings",
        "app.services.enforcement",
        "app.services.network.radius_sessions",
    }
    unexpected = {
        module: names for module, names in duplicates.items() if module not in known
    }
    stale_exemptions = sorted(known - duplicates.keys())
    assert not stale_exemptions, (
        "duplicate-owner exemptions that no longer describe the registry:\n  "
        + "\n  ".join(stale_exemptions)
    )
    assert not unexpected, (
        "module declared under multiple owner names:\n  "
        + "\n  ".join(
            f"{module}: {', '.join(names)}"
            for module, names in sorted(unexpected.items())
        )
    )
