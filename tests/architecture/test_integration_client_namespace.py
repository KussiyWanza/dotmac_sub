"""The transport client must not collide with the Starter module namespace."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD_NAMESPACE = "dotmac_integration"
CLIENT_NAMESPACE = "dotmac_integration_client"


def imported_roots(source: str) -> set[str]:
    """Return import roots from executable syntax, ignoring prose."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_transport_consumers_use_the_collision_free_client_namespace() -> None:
    imports = {
        path.relative_to(ROOT): imported_roots(path.read_text(encoding="utf-8"))
        for path in (ROOT / "app").rglob("*.py")
    }
    old_consumers = sorted(
        path for path, roots in imports.items() if OLD_NAMESPACE in roots
    )
    new_consumers = sorted(
        path for path, roots in imports.items() if CLIENT_NAMESPACE in roots
    )

    assert old_consumers == [], (
        "the client import collides with dotmac-integration's server module: "
        f"{old_consumers}"
    )
    assert new_consumers, "no production code consumes the renamed client namespace"


def test_client_dependency_pins_the_collision_free_release() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependency = project["tool"]["poetry"]["dependencies"]["dotmac-integration-client"]

    assert dependency["tag"] == "v0.2.0"


def test_namespace_guard_has_a_sensitivity_proof() -> None:
    assert imported_roots("from dotmac_integration import IntegrationHttpClient") == {
        OLD_NAMESPACE
    }
    assert imported_roots('"""from dotmac_integration import Fake"""') == set()
