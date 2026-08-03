"""Architecture checks for thin web/api wrappers."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROUTE_DIRS = [PROJECT_ROOT / "app" / "web", PROJECT_ROOT / "app" / "api"]
DISALLOWED_PATTERNS = [
    re.compile(r"\bdb\.query\("),
    re.compile(r"\bdb\.execute\("),
    re.compile(r"\bselect\("),
]

# Files that legitimately need direct DB access (health checks, helpers)
EXCLUDED_FILES = {
    "health.py",  # Health checks require direct DB access
    # GenieACS BOOTSTRAP webhook resolves the ONT by serial before
    # dispatching to ``reconcile_ont``. The lookup is a single bounded
    # query and the wrapper is intentionally minimal -- there is no
    # service-layer abstraction worth introducing for one SELECT.
    "reconcile_webhooks.py",
}

MIGRATED_MUTATION_WRAPPERS = {
    PROJECT_ROOT / "app" / "web" / "admin" / "inbox.py",
}

INBOX_COMMAND_ROUTES = {
    "team_inbox_reply": "reply",
    "team_inbox_label_create": "create_label",
    "team_inbox_label_apply": "apply_label",
    "team_inbox_label_remove": "remove_label",
    "team_inbox_macro_create": "create_macro",
    "team_inbox_template_create": "create_template",
    "team_inbox_message_retry": "retry_message",
    "team_inbox_retry_failed_batch": "retry_failed_batch",
    "team_inbox_workflow_action": "update_workflow",
    "team_inbox_saved_filter_create": "save_filter",
    "team_inbox_bulk_action": "bulk_action",
    "team_inbox_presence_action": "set_agent_presence",
    "team_inbox_contact_link": "link_contact",
    "team_inbox_internal_note": "create_internal_note",
    "team_inbox_comment_create": "create_comment",
    "team_inbox_comment_resolve": "resolve_comment",
    "team_inbox_status_action": "update_status",
}


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for route_dir in ROUTE_DIRS:
        files.extend(
            path
            for path in route_dir.rglob("*.py")
            if path.is_file() and path.name not in EXCLUDED_FILES
        )
    return sorted(files)


def test_web_and_api_wrappers_do_not_issue_direct_queries() -> None:
    violations: list[str] = []

    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in DISALLOWED_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                rel = path.relative_to(PROJECT_ROOT)
                violations.append(f"{rel}:{line} -> {pattern.pattern}")

    assert not violations, "\n".join(violations)


def _attribute_root(node: ast.Attribute) -> ast.expr:
    root: ast.expr = node
    while isinstance(root, ast.Attribute):
        root = root.value
    return root


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def test_migrated_mutation_wrappers_have_no_session_or_attribute_writes() -> None:
    """Keep migrated UI adapters from regaining model/transaction ownership."""
    violations: list[str] = []

    for path in sorted(MIGRATED_MUTATION_WRAPPERS):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(PROJECT_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                root = _attribute_root(node.func)
                if isinstance(root, ast.Name) and root.id in {"db", "session"}:
                    violations.append(
                        f"{rel}:{node.lineno} -> direct session call "
                        f"{_dotted_name(node.func)}"
                    )
            targets: list[ast.expr] = []
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                else:
                    targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    violations.append(
                        f"{rel}:{target.lineno} -> direct attribute write "
                        f"{_dotted_name(target)}"
                    )

    assert not violations, "\n".join(violations)


def test_admin_inbox_mutations_delegate_to_canonical_commands() -> None:
    path = PROJECT_ROOT / "app" / "web" / "admin" / "inbox.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    violations: list[str] = []

    for route_name, command_name in INBOX_COMMAND_ROUTES.items():
        route = functions.get(route_name)
        if route is None:
            violations.append(f"missing route function {route_name}")
            continue
        expected = f"team_inbox_commands.{command_name}"
        calls = {
            name
            for node in ast.walk(route)
            if isinstance(node, ast.Call)
            and (name := _dotted_name(node.func)) is not None
        }
        if expected not in calls:
            violations.append(f"{route_name} must delegate to {expected}")

    assert not violations, "\n".join(violations)


def test_admin_network_routes_are_not_registered_twice() -> None:
    """Catch legacy/split router overlap for OLT/ONT admin paths."""
    from app.web.admin import router

    seen: dict[tuple[tuple[str, ...], str], list[str]] = defaultdict(list)
    for route in router.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/admin/network/olt") and not path.startswith(
            "/admin/network/ont"
        ):
            continue
        methods = tuple(sorted(getattr(route, "methods", set()) or set()))
        endpoint = getattr(route, "endpoint", None)
        name = (
            f"{endpoint.__module__}.{endpoint.__name__}"
            if endpoint is not None
            else repr(route)
        )
        seen[(methods, path)].append(name)

    duplicates = [
        f"{methods} {path}: {', '.join(endpoints)}"
        for (methods, path), endpoints in sorted(seen.items())
        if len(endpoints) > 1
    ]
    assert not duplicates, "\n".join(duplicates)
