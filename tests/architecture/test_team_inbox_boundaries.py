"""Architecture guard: the team-inbox owner family is the only inbox writer.

The contracted ``communications.team_inbox_*`` family owns all
inbox ORM mutations. Everything else — API webhooks, admin web routes, tasks,
other services — must go through the family's entrypoints
(``team_inbox_commands`` for admin mutations, ``*_committed`` receivers for
ingest). A new module that imports inbox models for writing is a parallel
writer and fails here.

The check is intentionally blunt: any ``app/`` module outside the allowed set
that instantiates an inbox model class or references it in an ORM-write
context (``db.add(Inbox…)``, ``update(Inbox…)``, ``delete(Inbox…)``,
``insert(Inbox…)``) is flagged. Read-only usage (``select``/``query`` for
projections) stays free.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP = PROJECT_ROOT / "app"

# Inbox ORM classes (app/models/team_inbox.py) that carry decision state.
_INBOX_MODELS = (
    "TeamInboxEmailRoute",
    "TeamInboxChannelRoute",
    "TeamInboxAiRoute",
    "InboxConversation",
    "InboxSavedFilter",
    "InboxLabel",
    "InboxConversationLabel",
    "InboxReplyMacro",
    "InboxMessageTemplate",
    "InboxContactLink",
    "InboxConversationTeam",
    "InboxMessage",
    "InboxMediaAsset",
    "InboxComment",
    "InboxAgentPresence",
    "InboxConversationAssignment",
    "InboxProviderObservation",
    "InboxConversationReadState",
)

_WRITE_CONTEXTS = (
    [re.compile(rf"\b(?:db|session)\.add\(\s*{m}\b") for m in _INBOX_MODELS]
    + [re.compile(rf"\b(?:update|delete|insert)\(\s*{m}\b") for m in _INBOX_MODELS]
    + [
        # direct construction assigned into a session elsewhere still starts here
        re.compile(rf"^\s*(?:\w+\s*=\s*)?{m}\(", re.M)
        for m in _INBOX_MODELS
    ]
)

# The owner family plus its models module.
_ALLOWED = {
    "app/models/team_inbox.py",
}


def _is_allowed(rel: str) -> bool:
    if rel in _ALLOWED:
        return True
    name = Path(rel).name
    return rel.startswith("app/services/") and (
        name.startswith("team_inbox") or name.startswith("team_outbound")
    )


def test_no_inbox_writer_outside_team_inbox_family():
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        rel = str(path.relative_to(PROJECT_ROOT))
        if _is_allowed(rel):
            continue
        text = path.read_text(encoding="utf-8")
        if not any(m in text for m in _INBOX_MODELS):
            continue
        for pattern in _WRITE_CONTEXTS:
            if pattern.search(text):
                offenders.append(f"{rel}: {pattern.pattern}")
                break
    assert not offenders, (
        "Inbox ORM writes outside the team_inbox owner family (route through "
        "team_inbox_commands / the *_committed receivers instead):\n"
        + "\n".join(sorted(offenders))
    )


def test_operator_reply_keeps_typed_command_and_late_nowait_lock():
    source = (APP / "services" / "team_inbox_commands.py").read_text(encoding="utf-8")
    reply_start = source.index("def reply(")
    reply_end = source.index("\ndef create_label(", reply_start)
    reply_source = source[reply_start:reply_end]

    assert "command: ReplyCommand" in reply_source
    assert "command.conversation_id" in reply_source
    assert "for_update=True" in reply_source
    assert "nowait=True" in reply_source
    assert reply_source.index("list_approved_templates") < reply_source.index(
        "nowait=True"
    )
    assert "except OperationalError as exc" in reply_source
    assert "_is_lock_unavailable(exc)" in reply_source
