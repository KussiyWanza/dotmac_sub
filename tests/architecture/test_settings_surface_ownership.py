"""Every setting the operator console offers must be declared by an owner.

A setting is a decision input: it parameterises a decision, so it belongs to the
owner of that decision. `web_system_config` keeps its own key lists, parallel to
`settings_spec`, and nothing ever required a key to appear in both. The surface
therefore accumulated controls no owner declares — an operator sets
`auto_suspend_on_overdue = false`, saves it, and is told suspension is off while
suspension continues.

That is worse than the control not existing: it produces confident wrong
conclusions during an incident.

The baseline is shrink-only. Removing an entry means either deleting the setting
or having its decision owner declare and read it. Wiring one is an affirmative
act by that owner, not a rescue during cleanup.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONSOLE = PROJECT_ROOT / "app" / "services" / "web_system_config.py"
SPEC = PROJECT_ROOT / "app" / "services" / "settings_spec.py"
SEED = PROJECT_ROOT / "app" / "services" / "settings_seed.py"
BASELINE = Path(__file__).with_name("unowned_setting_surface_baseline.txt")


def _string_items(node: ast.AST) -> set[str]:
    return {
        element.value
        for element in getattr(node, "elts", [])
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }


def offered_keys() -> set[str]:
    """Keys the operator console reads and writes, from its `*_KEYS` lists."""
    tree = ast.parse(CONSOLE.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not any(name.endswith("_KEYS") for name in names):
            continue
        keys |= _string_items(node.value)
    return keys


def declared_keys() -> set[str]:
    """Keys an owner declares, in the spec or the seed."""
    declared: set[str] = set()
    for source in (SPEC, SEED):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "key":
                if isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str
                ):
                    declared.add(node.value.value)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                declared.add(node.value)
    return declared


def _baseline() -> set[str]:
    return {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _unowned() -> set[str]:
    return offered_keys() - declared_keys()


def test_the_console_offers_no_new_unowned_setting() -> None:
    added = sorted(_unowned() - _baseline())

    assert not added, (
        "the operator console offers settings that no owner declares. A control "
        "nobody reads is worse than no control: it reports a decision that is "
        "not being made. Declare each in settings_spec and read it from the "
        "owning service, or do not offer it:\n  " + "\n  ".join(added)
    )


def test_the_unowned_baseline_only_shrinks() -> None:
    resolved = sorted(_baseline() - _unowned())

    assert not resolved, (
        "these settings gained an owner or were removed. Delete them from "
        "unowned_setting_surface_baseline.txt so the repair is permanent:\n  "
        + "\n  ".join(resolved)
    )


def test_the_console_offers_settings_at_all() -> None:
    """Guards the extractor: a silent parse change must not empty the check."""
    assert len(offered_keys()) > 50
    assert len(declared_keys()) > 50
