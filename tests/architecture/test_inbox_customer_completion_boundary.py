from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == name
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        )
    ]


def test_every_team_inbox_conversation_creator_snapshots_customer_policy():
    missing: list[str] = []
    for path in sorted((APP / "services").glob("team_inbox*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _calls_named(tree, "InboxConversation"):
            keyword_names = {keyword.arg for keyword in call.keywords}
            if "customer_completion_policy_version_id" not in keyword_names:
                missing.append(f"{path.relative_to(ROOT)}:{call.lineno}")

    assert not missing, (
        "Every Team Inbox conversation creator must snapshot the active Customer "
        f"completion policy: {missing}"
    )


def test_agent_resolution_gate_has_one_central_enforcement_caller():
    callers: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _calls_named(tree, "require_agent_resolution_ready"):
            callers.append(path.relative_to(ROOT).as_posix())

    assert callers == ["app/services/team_inbox_status.py"]
