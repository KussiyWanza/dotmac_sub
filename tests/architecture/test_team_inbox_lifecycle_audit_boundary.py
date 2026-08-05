from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "app" / "services"


def _direct_status_writers(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "conversation"
            and target.attr == "status"
            for target in node.targets
        )
    ]


def test_only_status_owner_directly_writes_conversation_status():
    offenders: list[str] = []
    for path in SERVICES.glob("team_inbox*.py"):
        if path.name == "team_inbox_status.py":
            continue
        for line in _direct_status_writers(path):
            offenders.append(f"{path.name}:{line}")
    assert offenders == []


def test_lifecycle_event_tables_are_written_only_by_named_owners():
    allowed = {
        "team_inbox_assignment.py",
        "team_inbox_status.py",
        "team_inbox_audit_reconstruction.py",
    }
    event_names = (
        "InboxRoutingEvent(",
        "InboxStatusTransitionEvent(",
        "InboxAgentPresenceEvent(",
    )
    offenders = []
    for path in SERVICES.glob("team_inbox*.py"):
        source = path.read_text(encoding="utf-8")
        if path.name not in allowed and any(name in source for name in event_names):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_assignment_closure_is_owned_by_routing_service():
    offenders = []
    for path in SERVICES.glob("team_inbox*.py"):
        if path.name == "team_inbox_assignment.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "assignment.is_active = False" in source:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
