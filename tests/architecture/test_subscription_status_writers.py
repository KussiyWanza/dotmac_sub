"""Subscription.status has one transition owner: app.services.account_lifecycle.

The 2026-07-13 re-audit traced customer-impacting incidents to raw
``subscription.status = ...`` writes that bypassed
``assert_legal_subscription_transition`` and the enforcement-lock ledger — see
tests/test_access_enforcement_strays.py (S3: the reseller portal reactivated
with a raw status write, so nothing re-provisioned the service). Those callers
were routed through the owner; this test pins the consolidation so a new raw
writer is a build failure, not a re-audit finding.

Allowlisted writers (each an explicit ownership decision):

- ``app/services/account_lifecycle.py`` — the transition owner.
- ``app/services/web_system_restore_tool.py`` — snapshot-restore tooling:
  reinstates recorded state; not a business transition.

Detection is AST-based, not string matching: any attribute assignment whose
target is named ``status`` and whose right-hand side references
``SubscriptionStatus`` counts as a subscription transition write. Constructor
keywords (``Subscription(status=...)``) are creation, not transition, and are
deliberately out of scope. KNOWN LIMIT (disclosed, not hidden): a write
laundered through a local variable (``s = SubscriptionStatus.active; sub.status
= s``) evades the value check unless the variable name itself ends in
``status`` AND the receiver is named ``subscription*`` — the secondary
heuristic below catches the common spelling without flagging the many other
``<run|job>.status = status`` job-state writes, and ``Subscriber.status``
display writes stay excluded because their right-hand sides reference
``AccountStatus``/derived helpers, not ``SubscriptionStatus``.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

ALLOWED_WRITERS = {
    "app/services/account_lifecycle.py",
    "app/services/web_system_restore_tool.py",
}


def _references_subscription_status(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "SubscriptionStatus":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "SubscriptionStatus":
            return True
    return False


def _imports_subscription_status(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "SubscriptionStatus" for alias in node.names):
                return True
    return False


def _laundered_subscription_write(target: ast.Attribute, value: ast.AST) -> bool:
    """Secondary heuristic: ``subscription.status = <name>_status``.

    Requires the receiver to be named ``subscription*`` so that the many
    legitimate ``<run|job>.status = status`` job-state writes don't flag.
    """
    receiver_is_subscription = isinstance(
        target.value, ast.Name
    ) and target.value.id.startswith("subscription")
    return (
        receiver_is_subscription
        and isinstance(value, ast.Name)
        and value.id.endswith("status")
    )


def test_subscription_status_assignments_have_one_owner() -> None:
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in ALLOWED_WRITERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — syntax is checked elsewhere
            continue
        module_imports_enum = _imports_subscription_status(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AugAssign):
                targets, value = [node.target], node.value
            else:
                continue
            direct = _references_subscription_status(value)
            for target in targets:
                if not (isinstance(target, ast.Attribute) and target.attr == "status"):
                    continue
                laundered = module_imports_enum and _laundered_subscription_write(
                    target, value
                )
                if direct or laundered:
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "Raw Subscription.status writes outside the allowlisted owners — route "
        "them through app.services.account_lifecycle (see this test's module "
        "docstring for the ownership decisions): " + ", ".join(sorted(offenders))
    )


def test_allowlist_entries_still_write_status() -> None:
    """Shrink-only allowlist: an entry that no longer writes status is stale."""
    stale: list[str] = []
    for rel in sorted(ALLOWED_WRITERS):
        path = PROJECT_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        writes = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            else:
                continue
            if any(
                isinstance(t, ast.Attribute) and t.attr == "status" for t in targets
            ):
                writes = True
                break
        if not writes:
            stale.append(rel)
    assert not stale, f"Allowlisted files no longer write .status — remove: {stale}"
