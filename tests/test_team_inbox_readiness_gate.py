"""Slice 5: the gate before conversation traffic moves onto this workspace.

Production carries 84 conversations against CRM's ~37,539 and has never
exercised operator workflows at scale, so this asserts the three things that
have to hold before that changes:

1. **RBAC** — every mutating route is gated on `support:ticket:update`, every
   read on `support:ticket:read`, and the drawer's sensitive fields have their
   own gates.
2. **Attribution** — every operator mutation records *who* did it. There is no
   central audit trail for inbox commands (see the module note below), so the
   per-row actor columns are the whole story and must not regress.
3. **Volume** — the queue read stays correct and bounded as the table grows.

On audit: neither `team_inbox_commands` nor `execute_owner_command` writes
`audit_events`. Provenance is split: the relational rows carry proper actor
columns (`InboxComment.author_person_id`,
`InboxConversationLabel.applied_by_person_id`,
`InboxConversationAssignment.assigned_by_person_id`), while **everything
written to `InboxMessage` puts the actor in JSON metadata instead** — replies
as `sent_by_person_id`, internal notes as `actor_id` — and conversation
workflow changes live in `workflow_history` metadata. `communications.conversation_ticket_handoff` is
the exception and does stage an audit event. That split is deliberate enough to
pin, so a future change cannot quietly drop attribution.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 5.
"""

from __future__ import annotations

import ast
import re
import time
import uuid
from pathlib import Path

import pytest

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxComment,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMessage,
)
from app.services import team_inbox_commands, team_inbox_read
from tests.staff_identity_fixtures import add_bound_staff_user

ROUTES_SOURCE = Path("app/web/admin/inbox.py").read_text()
DRAWER = Path("templates/admin/inbox/_contact_drawer.html").read_text()


# --- 1. RBAC ------------------------------------------------------------


def _inbox_routes() -> list[tuple[str, str, str | None]]:
    """(http_method, path, required_permission) for every route in the module."""
    tree = ast.parse(ROUTES_SOURCE)
    found: list[tuple[str, str, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = ast.unparse(decorator.func)
            match = re.fullmatch(r"router\.(get|post|put|patch|delete)", func)
            if not match:
                continue
            path = (
                decorator.args[0].value
                if decorator.args and isinstance(decorator.args[0], ast.Constant)
                else ""
            )
            rendered = ast.unparse(decorator)
            permission = None
            perm_match = re.search(r'require_permission\(["\']([^"\']+)', rendered)
            if perm_match:
                permission = perm_match.group(1)
            found.append((match.group(1).upper(), path, permission))
    return found


def test_every_inbox_route_declares_a_permission():
    ungated = [
        f"{method} {path}"
        for method, path, permission in _inbox_routes()
        if permission is None
    ]
    assert not ungated, f"inbox routes without require_permission: {ungated}"


# Saving a personal view is a preference, not a conversation mutation, so it is
# deliberately gated on :read. Anything else posting under :read is a mistake.
READ_GATED_POSTS = {"/filters/save"}


def test_mutating_routes_require_update_and_reads_require_read():
    wrong = []
    for method, path, permission in _inbox_routes():
        if method == "GET":
            expected = "support:ticket:read"
        elif path in READ_GATED_POSTS:
            expected = "support:ticket:read"
        else:
            expected = "support:ticket:update"
        if permission != expected:
            wrong.append(f"{method} {path} -> {permission} (expected {expected})")
    assert not wrong, "inbox route permissions are inconsistent:\n" + "\n".join(wrong)


def test_the_read_gated_post_allowlist_stays_small():
    """Each entry weakens the write gate, so it must be justified and few."""
    assert READ_GATED_POSTS == {"/filters/save"}


def test_the_drawer_keeps_its_own_gates_for_sensitive_fields():
    """Arrears and session IP are more sensitive than the conversation."""
    assert 'can(request, "billing:account:read")' in ROUTES_SOURCE
    assert 'can(request, "network:ip:read")' in ROUTES_SOURCE
    assert "can_view_financials" in DRAWER
    assert "can_view_network_detail" in DRAWER
    # A denied principal is told, not shown an empty panel.
    assert "Billing detail hidden" in DRAWER


# --- 2. Attribution -----------------------------------------------------


@pytest.fixture()
def actor() -> uuid.UUID:
    return uuid.uuid4()


def _conversation_id(db_session, *, team_id=None):
    conversation = InboxConversation(
        channel_type="email",
        subject="Readiness",
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
        primary_service_team_id=team_id,
    )
    db_session.add(conversation)
    db_session.flush()
    captured = conversation.id
    db_session.commit()
    return captured


def test_a_private_note_records_its_author(db_session, actor):
    """A note is an InboxMessage with direction='internal', and its author is
    in metadata rather than a column."""
    conversation_id = _conversation_id(db_session)

    team_inbox_commands.create_internal_note(
        db_session,
        conversation_id=conversation_id,
        body="Checked the ONT.",
        actor_person_id=actor,
    )

    note = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "internal")
        .one()
    )
    assert (note.metadata_ or {}).get("actor_id") == str(actor)


def test_a_team_comment_records_its_author_in_a_column(db_session, actor):
    """Comments are the relational side and do carry a real column."""
    conversation_id = _conversation_id(db_session)

    team_inbox_commands.create_comment(
        db_session,
        conversation_id=conversation_id,
        body="Needs a field visit.",
        actor_person_id=actor,
    )

    assert db_session.query(InboxComment).one().author_person_id == actor


def test_a_status_change_records_the_actor_in_workflow_history(db_session, actor):
    conversation_id = _conversation_id(db_session)

    team_inbox_commands.update_workflow(
        db_session,
        conversation_id=conversation_id,
        priority=25,
        actor_person_id=actor,
    )

    conversation = db_session.get(InboxConversation, conversation_id)
    history = (conversation.metadata_ or {}).get("workflow_history") or []
    assert history, "workflow change left no history entry"
    assert history[-1]["actor_id"] == str(actor)


def test_an_assignment_records_who_assigned_it(db_session, actor):
    agent, agent_person = add_bound_staff_user(
        db_session,
        email="readiness-agent@example.test",
    )
    team = ServiceTeam(name="Readiness", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=agent_person.id,
            is_active=True,
        )
    )
    team_id = team.id
    agent_id = agent.id
    db_session.commit()
    conversation_id = _conversation_id(db_session, team_id=team_id)

    team_inbox_commands.assign_conversation(
        db_session,
        conversation_id=conversation_id,
        service_team_id=team_id,
        person_id=agent_id,
        actor_person_id=actor,
    )

    assignment = (
        db_session.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .one()
    )
    assert assignment.person_id == agent_id
    assert assignment.assigned_by_person_id == actor


def test_attribution_columns_still_exist_on_every_operator_row():
    """These columns are the audit story for the inbox; if one is dropped,
    attribution silently disappears."""
    assert hasattr(InboxComment, "author_person_id")
    assert hasattr(InboxConversationAssignment, "assigned_by_person_id")


def test_message_attribution_is_only_in_metadata_not_a_column():
    """Known weakness, pinned rather than assumed.

    `InboxMessage` has no actor column at all. An outbound reply records the
    team `from_address` and keeps the operator in
    `metadata["sent_by_person_id"]`; an internal note keeps it in
    `metadata["actor_id"]`. So "what did agent X send" is not a queryable
    question — it needs a JSON scan, and no index helps.

    Tolerable at 84 conversations. Worth deciding before ~37k arrive, which is
    exactly what this gate exists to surface.
    """
    columns = {c.name for c in InboxMessage.__table__.columns}
    assert "sent_by_person_id" not in columns
    assert "actor_person_id" not in columns

    outbound = Path("app/services/team_inbox_outbound.py").read_text()
    operations = Path("app/services/team_inbox_operations.py").read_text()
    assert '"sent_by_person_id": str(payload.sent_by_person_id)' in outbound
    assert '"actor_id": str(actor_person_id)' in operations


def test_the_audit_trail_covers_egress_and_ownership_crossings_only():
    """Pins which operator actions are audited, so nobody assumes coverage.

    This gate previously asserted that ``team_inbox_commands`` staged *no*
    audit events at all. That remains true of the everyday operator commands —
    replies, labels, notes, status and workflow changes record provenance in
    row metadata, not the audit log, which is the limitation the gate above
    describes.

    Two actions are audited, for two distinct reasons:

    - the conversation → ticket handoff, because it crosses an ownership line
      into ``support.ticket_lifecycle``;
    - a transcript export, because it sends an entire customer conversation to
      an arbitrary address on the ordinary ``support:ticket:update``
      permission, which is the widest data-egress path in this module.

    A third entry here should be a decision, not drift.
    """
    commands = Path("app/services/team_inbox_commands.py").read_text()
    handoff = Path("app/services/conversation_ticket_handoff.py").read_text()

    assert "stage_audit_event" in handoff
    assert commands.count("stage_audit_event(") == 1
    assert "TRANSCRIPT_AUDIT_ACTION" in commands
    for command in ("def reply(", "def apply_label(", "def update_status("):
        body = commands.split(command, 1)[1].split("\ndef ", 1)[0]
        assert "stage_audit_event" not in body, command


# --- 3. Representative volume -------------------------------------------


def _seed(db_session, count: int, *, subscriber_id=None):
    db_session.bulk_save_objects(
        [
            InboxConversation(
                id=uuid.uuid4(),
                channel_type="email",
                subject=f"Thread {index}",
                contact_address=f"c{index}@example.com",
                status=InboxConversationStatus.open.value,
                subscriber_id=subscriber_id,
                priority=100,
            )
            for index in range(count)
        ]
    )
    db_session.commit()


def test_the_queue_page_stays_bounded_at_volume(db_session):
    """A page must return page-size rows, not the table."""
    _seed(db_session, 500)

    result = team_inbox_read.list_conversations(db_session, limit=25)

    assert len(result.items) == 25
    assert result.count == 500


def test_paging_does_not_repeat_or_skip_rows(db_session):
    """The queue sorts on a composite; without a stable tie-break, pages
    overlap and an operator works the same thread twice."""
    _seed(db_session, 120)

    seen: list[str] = []
    for offset in range(0, 120, 25):
        page = team_inbox_read.list_conversations(db_session, limit=25, offset=offset)
        seen.extend(row.id for row in page.items)

    assert len(seen) == 120
    assert len(set(seen)) == 120, "pagination returned duplicate conversations"


def test_a_filtered_read_stays_responsive_at_volume(db_session):
    """Not a benchmark — a guard against an accidental full scan per row."""
    _seed(db_session, 500)

    started = time.perf_counter()
    result = team_inbox_read.list_conversations(db_session, open_only=True, limit=25)
    elapsed = time.perf_counter() - started

    assert len(result.items) == 25
    assert elapsed < 5.0, f"filtered queue read took {elapsed:.2f}s at 500 rows"


def test_customer_scoped_read_stays_selective_at_volume(db_session):
    """The customer 360 panel reads this; it must not widen as the table grows."""
    from app.models.subscriber import Subscriber
    from app.services.subscriber import _default_reseller_id

    row = Subscriber(
        first_name="Volume",
        last_name="Customer",
        email=f"volume-{uuid.uuid4().hex}@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(row)
    db_session.flush()
    subscriber_id = row.id
    db_session.commit()

    _seed(db_session, 400)
    _seed(db_session, 3, subscriber_id=subscriber_id)

    result = team_inbox_read.list_conversations(db_session, subscriber_id=subscriber_id)

    assert result.count == 3
