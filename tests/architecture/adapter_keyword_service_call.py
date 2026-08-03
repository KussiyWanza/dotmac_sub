"""Shared analysis for the adapter keyword-argument guard.

Routes and other adapters call owning services across a module boundary that
has no arity check: when a service signature gains a parameter in the middle,
every positional argument after it silently rebinds to a different meaning.
That is not hypothetical — four auth API endpoints answered 400/500 to every
request because ``order_dir`` had shifted into ``order_by`` (issue #1895).

Short calls stay readable positionally; long ones are where meaning is carried
by position alone and drift goes unnoticed, so those must use keywords.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.source_index import python_ast, python_files

# (db, a, b) reads fine; beyond that, position stops being self-describing.
MAX_POSITIONAL_ARGS = 3

ADAPTER_ROOTS = ("app/api", "app/web")


def _service_aliases(tree: ast.Module) -> set[str]:
    """Names in this module that refer to something under ``app.services``."""

    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("app.services"):
                aliases.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.services"):
                    aliases.add(alias.asname or alias.name.split(".")[-1])
    return aliases


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def violations_for(path: Path) -> list[int]:
    """Line numbers of over-long positional service calls in one adapter."""

    tree = python_ast(path)
    aliases = _service_aliases(tree)
    if not aliases:
        return []
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and _root_name(node.func) in aliases
        and len(node.args) > MAX_POSITIONAL_ARGS
    )


def counts_by_module() -> dict[str, int]:
    """Current violation count per adapter module, keyed by repo path."""

    counts: dict[str, int] = {}
    for root in ADAPTER_ROOTS:
        for path in python_files(Path(root)):
            found = violations_for(path)
            if found:
                counts[path.as_posix()] = len(found)
    return counts
