"""OntUnit.sync_status has one transition owner: ont_status.set_sync_status.

Mirrors ``tests/architecture/test_subscription_status_writers.py``'s shape
for the analogous ``sync_status`` consolidation (2026-09-09): a scan of every
raw ``ont.sync_status = ...`` assignment across the codebase found 7 write
sites across 4 files (``reconcile/core.py`` x4, ``reconcile/locking.py``,
``reconcile/lifecycle.py``, ``network_subscriber_bridge.py``), all now routed
through ``app.services.network.ont_status.set_sync_status``. This test pins
that consolidation so a new raw writer is a build failure, not a re-audit
finding.

Allowlisted writers (each an explicit ownership decision):

- ``app/services/network/ont_status.py`` — the single owning setter itself
  (``set_sync_status``'s own ``ont.sync_status = next_status`` write).

Detection is AST-based, not string matching: any attribute assignment whose
target attribute is named ``sync_status`` counts as a sync-status transition
write, regardless of what the right-hand side is. Unlike the
subscription-status precedent — where ``status`` is a generic attribute name
shared with many unrelated ``<run|job>.status`` job-state writes, forcing a
right-hand-side ``SubscriptionStatus`` reference check to avoid mass false
positives — ``sync_status`` is unique to ``OntUnit`` in this codebase
(verified: only one model field is literally named ``sync_status``;
``backoffice_sync_status`` on ``VendorRoute`` is a distinct attribute name
and does not match). So no right-hand-side check or laundering heuristic is
needed here: a bare ``<anything>.sync_status = <anything>`` is unambiguous,
and there is no known-limit evasion to disclose. Constructor keywords
(``OntUnit(sync_status=...)``) are creation, not transition, and are
deliberately out of scope — there are none of these in the codebase today
(the column has a server default).
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

ALLOWED_WRITERS = {
    "app/services/network/ont_status.py",
}


def test_sync_status_assignments_have_one_owner() -> None:
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in ALLOWED_WRITERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — syntax is checked elsewhere
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "sync_status":
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "Raw OntUnit.sync_status writes outside the allowlisted owner — route "
        "them through app.services.network.ont_status.set_sync_status (see "
        "this test's module docstring for the ownership decision): "
        + ", ".join(sorted(offenders))
    )


def test_allowlist_entry_still_writes_sync_status() -> None:
    """Shrink-only allowlist: an entry that no longer writes sync_status is stale."""
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
                isinstance(t, ast.Attribute) and t.attr == "sync_status"
                for t in targets
            ):
                writes = True
                break
        if not writes:
            stale.append(rel)
    assert not stale, (
        f"Allowlisted files no longer write .sync_status — remove: {stale}"
    )
