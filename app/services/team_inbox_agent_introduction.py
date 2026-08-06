"""Per-agent introduction templates and chat-widget pickup greeting policy."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxAgentIntroductionPreference,
    InboxChannelType,
    InboxConversation,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_outbound
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_agent_introduction"
DEFAULT_TEMPLATE = "Hi, my name is {agent_name} and I will be assisting you today."
_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="per-agent introduction preference",
    name="update_team_inbox_agent_introduction",
)


@dataclass(frozen=True)
class AgentIntroductionPreference:
    template: str
    auto_send_chat_widget: bool


@dataclass(frozen=True)
class UpdateAgentIntroductionCommand:
    context: CommandContext
    person_id: UUID
    template: str
    auto_send_chat_widget: bool


def _validate_template(template: str) -> str:
    clean = str(template or "").strip()
    if not clean or len(clean) > 500:
        raise ValueError("Introduction template must be between 1 and 500 characters.")
    if "{" in clean.replace("{agent_name}", "") or "}" in clean.replace(
        "{agent_name}", ""
    ):
        raise ValueError("Only the {agent_name} placeholder is supported.")
    return clean


def preference_for_agent(db: Session, person_id: UUID) -> AgentIntroductionPreference:
    row = (
        db.query(InboxAgentIntroductionPreference)
        .filter(InboxAgentIntroductionPreference.person_id == person_id)
        .one_or_none()
    )
    return AgentIntroductionPreference(
        template=row.template if row else DEFAULT_TEMPLATE,
        auto_send_chat_widget=row.auto_send_chat_widget if row else True,
    )


def update_preference(
    db: Session,
    *,
    person_id: UUID,
    template: str,
    auto_send_chat_widget: bool,
) -> AgentIntroductionPreference:
    clean = _validate_template(template)
    row = (
        db.query(InboxAgentIntroductionPreference)
        .filter(InboxAgentIntroductionPreference.person_id == person_id)
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        row = InboxAgentIntroductionPreference(person_id=person_id, template=clean)
        db.add(row)
    row.template = clean
    row.auto_send_chat_widget = auto_send_chat_widget
    db.flush()
    return AgentIntroductionPreference(
        template=row.template, auto_send_chat_widget=row.auto_send_chat_widget
    )


def update_preference_committed(
    db: Session, command: UpdateAgentIntroductionCommand
) -> AgentIntroductionPreference:
    return execute_owner_command(
        db,
        definition=_COMMAND,
        context=command.context,
        operation=lambda: update_preference(
            db,
            person_id=command.person_id,
            template=command.template,
            auto_send_chat_widget=command.auto_send_chat_widget,
        ),
    )


def rendered_introduction(db: Session, person_id: UUID) -> str:
    user = db.get(SystemUser, person_id)
    if user is None:
        return ""
    name = str(user.display_name or f"{user.first_name} {user.last_name}").strip()
    return preference_for_agent(db, person_id).template.replace("{agent_name}", name)


def maybe_send_on_pickup(
    db: Session,
    *,
    conversation: InboxConversation,
    person_id: UUID,
) -> bool:
    if conversation.channel_type != InboxChannelType.chat_widget.value:
        return False
    preference = preference_for_agent(db, person_id)
    if not preference.auto_send_chat_widget:
        return False
    already_sent = (
        db.query(InboxMessage.id)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .filter(
            InboxMessage.metadata_["sent_by_person_id"].as_string() == str(person_id)
        )
        .first()
        is not None
    )
    if already_sent:
        return False
    body = rendered_introduction(db, person_id)
    if not body:
        return False
    result = team_inbox_outbound.send_inbox_reply(
        db,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html=body,
            body_text=body,
            sent_by_person_id=person_id,
            metadata={"message_kind": "agent_introduction", "auto_sent": True},
        ),
    )
    return result.kind in {"sent", "queued"}
