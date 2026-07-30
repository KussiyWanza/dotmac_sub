"""Shared, process-local source index for static architecture guards.

The architecture suite runs with pytest-xdist, so each worker owns one index.
Within that worker, files are listed, read, and parsed once and then reused by
every guard that asks for the same path.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path


@cache
def files(root: Path, pattern: str) -> tuple[Path, ...]:
    """Return a stable, cached file listing below ``root``."""

    return tuple(
        sorted(
            path
            for path in root.rglob(pattern)
            if path.is_file() and "__pycache__" not in path.parts
        )
    )


def python_files(root: Path) -> tuple[Path, ...]:
    """Return cached Python files below ``root``."""

    return files(root, "*.py")


@cache
def source_text(path: Path) -> str:
    """Read one repository source file once per architecture worker."""

    return path.read_text(encoding="utf-8")


@cache
def python_ast(path: Path) -> ast.Module:
    """Parse one Python source file once per architecture worker."""

    return ast.parse(source_text(path), filename=str(path))


@cache
def python_nodes(path: Path) -> tuple[ast.AST, ...]:
    """Return one cached traversal of a Python module."""

    return tuple(ast.walk(python_ast(path)))


@cache
def string_constants(path: Path) -> frozenset[str]:
    """Return every string literal in one Python module."""

    return frozenset(
        node.value
        for node in python_nodes(path)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


@cache
def identifier_names(path: Path) -> frozenset[str]:
    """Return every loaded or stored identifier in one Python module."""

    return frozenset(
        node.id for node in python_nodes(path) if isinstance(node, ast.Name)
    )


@cache
def class_names(path: Path) -> frozenset[str]:
    """Return every class defined in one Python module."""

    return frozenset(
        node.name for node in python_nodes(path) if isinstance(node, ast.ClassDef)
    )


@cache
def call_lines(path: Path) -> dict[str, tuple[int, ...]]:
    """Index simple and attribute call names to their source lines."""

    found: dict[str, list[int]] = {}
    for node in python_nodes(path):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        else:
            continue
        found.setdefault(name, []).append(node.lineno)
    return {name: tuple(lines) for name, lines in found.items()}


def clear_source_index() -> None:
    """Clear worker-local caches for focused cache-behaviour tests."""

    files.cache_clear()
    source_text.cache_clear()
    python_ast.cache_clear()
    python_nodes.cache_clear()
    string_constants.cache_clear()
    identifier_names.cache_clear()
    class_names.cache_clear()
    call_lines.cache_clear()
