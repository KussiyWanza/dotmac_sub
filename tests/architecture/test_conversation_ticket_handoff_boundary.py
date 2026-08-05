"""Boundary guards for communications.conversation_ticket_handoff.

Mirrors tests/architecture/test_ticket_work_order_handoff_boundary.py: the
handoff owner is the only writer of the provenance link, and nothing that can
post a ticket payload may forge it.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = "app/services/conversation_ticket_handoff.py"


def _sets_ticket_origin(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not any(
        keyword.arg == "origin_conversation_id" for keyword in node.keywords
    ):
        return False
    callee = ast.unparse(node.func)
    return callee.endswith(".Tickets.create") or callee in {"Ticket", "support.Ticket"}


def test_only_the_handoff_owner_passes_provenance_to_the_ticket_command():
    callsites = []
    for path in (ROOT / "app").rglob("*.py"):
        if path.as_posix().endswith("app/services/support.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(_sets_ticket_origin(node) for node in ast.walk(tree)):
            callsites.append(path.relative_to(ROOT).as_posix())

    assert callsites == [OWNER]


def test_ticket_payload_schema_cannot_set_provenance():
    """If it were a payload field, any ticket API caller could forge origin."""
    schema_source = (ROOT / "app/schemas/support.py").read_text()
    assert "origin_conversation_id" not in schema_source


def test_provenance_is_keyword_only_on_the_ticket_create_command():
    support_source = (ROOT / "app/services/support.py").read_text()
    tree = ast.parse(support_source)

    creates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "create"
        and any(arg.arg == "origin_conversation_id" for arg in node.args.kwonlyargs)
    ]
    assert len(creates) == 1, "Ticket create must take provenance keyword-only"
    # And never positionally.
    assert all(
        arg.arg != "origin_conversation_id"
        for node in creates
        for arg in node.args.args
    )


def test_no_direct_orm_write_of_the_provenance_column_outside_the_owner():
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in {OWNER, "app/models/support.py", "app/services/support.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        writes = [
            node
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)
            )
            and any(
                isinstance(target, ast.Attribute)
                and target.attr == "origin_conversation_id"
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
        ]
        if writes:
            offenders.append(rel)

    assert offenders == []


def test_handoff_does_not_transition_the_conversation():
    """Conversation status belongs to communications.team_inbox.

    Checked by AST rather than substring: `conversation.status ==` is a
    legitimate read, and a naive `conversation.status =` match flags it.
    """
    owner_source = (ROOT / OWNER).read_text()
    for forbidden in ("team_inbox_commands.update_status", "team_inbox_operations"):
        assert forbidden not in owner_source

    tree = ast.parse(owner_source)
    written = [
        ast.unparse(target)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "status"
    ]
    assert written == []


def test_web_adapter_stays_thin():
    """app.web.admin.inbox is the only HTTP translator and must not decide."""
    routes = (ROOT / "app/web/admin/inbox.py").read_text()
    assert "conversation_ticket_handoff.issue_ticket" in routes
    # The route must not construct a Ticket or reach past the owner.
    assert "Ticket(" not in routes
    assert "Tickets.create" not in routes
