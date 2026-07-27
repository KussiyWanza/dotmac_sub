from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import auth_flow as auth_flow_service
from app.services import team_inbox_realtime
from app.services.chat_session_authority import (
    ChatSessionAuthority,
    resolve_chat_session_authority,
)
from app.services.common import coerce_uuid
from app.services.customer_support_links import (
    ticket_customer_link_filter,
    ticket_customer_linked_ids,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.realtime_platform import EventType

T = TypeVar("T")
OWNER = "communications.team_inbox_widget"
_WIDGET_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="authenticated visitor message and read-state commands",
    name="execute_team_inbox_widget_command",
)


class TeamInboxWidgetError(DomainError):
    """Stable visitor-widget failure mapped by transport adapters."""


def _error(suffix: str, message: str) -> TeamInboxWidgetError:
    return TeamInboxWidgetError(code=f"{OWNER}.{suffix}", message=message)


def _commit(db: Session, action: Callable[[], T]) -> T:
    return execute_owner_command(
        db,
        definition=_WIDGET_COMMAND,
        context=CommandContext.system(
            actor="transport:team-inbox-widget",
            scope="team-inbox:widget-command",
            reason="execute authenticated visitor Inbox command",
        ),
        operation=action,
    )


@dataclass(frozen=True)
class WidgetPrincipal:
    conversation_id: uuid.UUID
    session_id: str
    surface: str
    subscriber_id: str | None = None
    reseller_id: str | None = None


def _require_enabled(db: Session) -> None:
    if not settings.chat_live_enabled:
        raise _error("disabled", "Live chat is not enabled.")
    if resolve_chat_session_authority(db).authority != ChatSessionAuthority.SELFCARE:
        raise _error(
            "authority_external",
            "Live chat is currently handled by the CRM.",
        )


def _jwt_payload(
    db: Session,
    *,
    session_id: str,
    conversation_id: uuid.UUID,
    surface: str,
    subscriber_id: uuid.UUID | None = None,
    reseller_id: uuid.UUID | None = None,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "type": "chat_widget",
        "sub": f"chat_widget:{session_id}",
        "session_id": session_id,
        "conversation_id": str(conversation_id),
        "surface": surface,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=8)).timestamp()),
    }
    if subscriber_id is not None:
        payload["subscriber_id"] = str(subscriber_id)
    if reseller_id is not None:
        payload["reseller_id"] = str(reseller_id)
    return auth_flow_service._jwt_encode_token(  # noqa: SLF001
        payload,
        auth_flow_service._jwt_secret(db),  # noqa: SLF001
        auth_flow_service._jwt_algorithm(db),  # noqa: SLF001
    )


def decode_widget_token(db: Session, token: str) -> WidgetPrincipal:
    try:
        payload = jwt.decode(
            token,
            auth_flow_service._jwt_secret(db),  # noqa: SLF001
            algorithms=[auth_flow_service._jwt_algorithm(db)],  # noqa: SLF001
        )
    except JWTError as exc:
        raise _error("invalid_token", "Invalid visitor token.") from exc
    if payload.get("type") != "chat_widget":
        raise _error("invalid_token", "Invalid visitor token.")
    conversation_id = coerce_uuid(payload.get("conversation_id"))
    session_id = str(payload.get("session_id") or "").strip()
    if conversation_id is None or not session_id:
        raise _error("invalid_token", "Invalid visitor token.")
    return WidgetPrincipal(
        conversation_id=conversation_id,
        session_id=session_id,
        surface=str(payload.get("surface") or "customer"),
        subscriber_id=str(payload.get("subscriber_id") or "") or None,
        reseller_id=str(payload.get("reseller_id") or "") or None,
    )


def _thread_id(
    *, surface: str, entity_id: str, ticket_id: str | None, project_id: str | None
) -> str:
    context = ticket_id or project_id or "general"
    return f"chat_widget:{surface}:{entity_id}:{context}"[:255]


def _owned_ticket(db: Session, subscriber_id: uuid.UUID, ticket_id: str) -> bool:
    from app.models.support import Ticket

    native_id = coerce_uuid(ticket_id)
    return native_id is not None and (
        db.query(Ticket.id)
        .filter(
            Ticket.id == native_id,
            ticket_customer_link_filter(Ticket, subscriber_id),
        )
        .first()
        is not None
    )


def _owned_project(db: Session, subscriber_id: uuid.UUID, project_id: str) -> bool:
    from app.models.project import Project

    native_id = coerce_uuid(project_id)
    return native_id is not None and (
        db.query(Project.id)
        .filter(
            Project.id == native_id,
            Project.subscriber_id == subscriber_id,
            Project.is_active.is_(True),
        )
        .first()
        is not None
    )


def _reseller_owns(
    db: Session, reseller_id: uuid.UUID, subscriber_id: uuid.UUID | None
) -> bool:
    from app.services import reseller_portal

    return subscriber_id is not None and (
        reseller_portal.owned_account(db, str(reseller_id), str(subscriber_id))
        is not None
    )


def _customer_context(
    db: Session,
    subscriber_id: uuid.UUID,
    *,
    ticket_id: str | None,
    project_id: str | None,
) -> tuple[str | None, str | None]:
    if ticket_id and not _owned_ticket(db, subscriber_id, ticket_id):
        ticket_id = None
    if project_id and not _owned_project(db, subscriber_id, project_id):
        project_id = None
    return ticket_id, project_id


def _reseller_context(
    db: Session,
    reseller_id: uuid.UUID,
    *,
    ticket_id: str | None,
    project_id: str | None,
) -> tuple[str | None, str | None]:
    from app.models.project import Project
    from app.models.support import Ticket

    if ticket_id:
        native_id = coerce_uuid(ticket_id)
        ticket = db.get(Ticket, native_id) if native_id is not None else None
        if ticket is None or not any(
            _reseller_owns(db, reseller_id, coerce_uuid(owner_id))
            for owner_id in ticket_customer_linked_ids(ticket)
        ):
            ticket_id = None
    if project_id:
        native_id = coerce_uuid(project_id)
        owner_id = (
            db.query(Project.subscriber_id)
            .filter(Project.id == native_id, Project.is_active.is_(True))
            .scalar()
            if native_id is not None
            else None
        )
        if not _reseller_owns(db, reseller_id, owner_id):
            project_id = None
    return ticket_id, project_id


def resolve_customer_context(
    db: Session,
    subscriber_id: uuid.UUID,
    *,
    ticket_id: str | None,
    project_id: str | None,
) -> tuple[str | None, str | None]:
    return _customer_context(
        db,
        subscriber_id,
        ticket_id=ticket_id,
        project_id=project_id,
    )


def resolve_reseller_context(
    db: Session,
    reseller_id: uuid.UUID,
    *,
    ticket_id: str | None,
    project_id: str | None,
) -> tuple[str | None, str | None]:
    return _reseller_context(
        db,
        reseller_id,
        ticket_id=ticket_id,
        project_id=project_id,
    )


def _conversation(
    db: Session,
    *,
    surface: str,
    entity_id: str,
    contact_address: str | None,
    subject: str,
    subscriber_id: uuid.UUID | None = None,
    reseller_id: uuid.UUID | None = None,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> InboxConversation:
    external_thread_id = _thread_id(
        surface=surface,
        entity_id=entity_id,
        ticket_id=ticket_id,
        project_id=project_id,
    )
    conversation = (
        db.query(InboxConversation)
        .filter(InboxConversation.channel_type == InboxChannelType.chat_widget.value)
        .filter(InboxConversation.external_thread_id == external_thread_id)
        .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
        .filter(InboxConversation.is_active.is_(True))
        .order_by(InboxConversation.last_message_at.desc().nullslast())
        .first()
    )
    metadata = {
        key: value
        for key, value in {
            "surface": surface,
            "subscriber_id": str(subscriber_id) if subscriber_id else None,
            "reseller_id": str(reseller_id) if reseller_id else None,
            "ticket_id": ticket_id,
            "project_id": project_id,
            "source": "native_chat_widget",
        }.items()
        if value is not None
    }
    if conversation is None:
        now = datetime.now(UTC)
        conversation = InboxConversation(
            subscriber_id=subscriber_id,
            channel_type=InboxChannelType.chat_widget.value,
            status=InboxConversationStatus.open.value,
            subject=subject[:200],
            contact_address=contact_address,
            external_thread_id=external_thread_id,
            first_message_at=now,
            last_message_at=now,
            metadata_=metadata,
        )
        db.add(conversation)
        db.flush()
    else:
        merged = dict(conversation.metadata_ or {})
        merged.update({key: value for key, value in metadata.items() if value})
        conversation.metadata_ = merged
        if subscriber_id and not conversation.subscriber_id:
            conversation.subscriber_id = subscriber_id
        if contact_address and not conversation.contact_address:
            conversation.contact_address = contact_address
        db.flush()
    return conversation


def _session_response(
    db: Session,
    *,
    conversation: InboxConversation,
    surface: str,
    subscriber_id: uuid.UUID | None = None,
    reseller_id: uuid.UUID | None = None,
) -> dict[str, str | None]:
    session_id = str(conversation.id)
    return {
        "session_id": session_id,
        "visitor_token": _jwt_payload(
            db,
            session_id=session_id,
            conversation_id=conversation.id,
            surface=surface,
            subscriber_id=subscriber_id,
            reseller_id=reseller_id,
        ),
        "conversation_id": str(conversation.id),
        "ws_url": "/ws/inbox",
        "api_base": "/widget",
    }


def broker_customer_session(
    db: Session,
    subscriber_id: str,
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, str | None]:
    _require_enabled(db)
    sub = db.get(Subscriber, coerce_uuid(subscriber_id))
    if sub is None:
        raise _error("subscriber_not_found", "Subscriber not found.")
    name = (
        sub.display_name
        or " ".join(part for part in [sub.first_name, sub.last_name] if part).strip()
    )
    subject = "Chat with customer"
    if ticket_id:
        subject = "Chat about a support ticket"
    elif project_id:
        subject = "Chat about an installation project"
    conversation = _conversation(
        db,
        surface="customer",
        entity_id=str(sub.id),
        contact_address=sub.email or sub.phone,
        subject=subject,
        subscriber_id=sub.id,
        ticket_id=ticket_id,
        project_id=project_id,
    )
    metadata = dict(conversation.metadata_ or {})
    metadata["customer_name"] = name or None
    conversation.metadata_ = metadata
    db.flush()
    return _session_response(
        db,
        conversation=conversation,
        surface="customer",
        subscriber_id=sub.id,
    )


def broker_customer_session_committed(
    db: Session,
    subscriber_id: str,
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, str | None]:
    def action() -> dict[str, str | None]:
        subscriber_uuid = coerce_uuid(subscriber_id)
        subscriber = db.get(Subscriber, subscriber_uuid) if subscriber_uuid else None
        if subscriber is None:
            raise _error("subscriber_not_found", "Subscriber not found.")
        scoped_ticket, scoped_project = _customer_context(
            db,
            subscriber.id,
            ticket_id=ticket_id,
            project_id=project_id,
        )
        return broker_customer_session(
            db,
            str(subscriber.id),
            ticket_id=scoped_ticket,
            project_id=scoped_project,
        )

    return _commit(db, action)


def broker_reseller_session(
    db: Session,
    reseller_id: str,
    principal: dict,
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, str | None]:
    _require_enabled(db)
    reseller = db.get(Reseller, coerce_uuid(reseller_id))
    if reseller is None:
        raise _error("reseller_not_found", "Reseller not found.")
    email: str | None = None
    name: str | None = None
    if principal.get("principal_type") == "reseller_user":
        ru = db.get(ResellerUser, coerce_uuid(principal.get("principal_id")))
        if ru is not None and ru.is_active:
            email = ru.email
            name = ru.full_name
    conversation = _conversation(
        db,
        surface="reseller_portal",
        entity_id=str(reseller.id),
        contact_address=email or reseller.contact_email or reseller.contact_phone,
        subject="Chat with reseller",
        reseller_id=reseller.id,
        ticket_id=ticket_id,
        project_id=project_id,
    )
    metadata = dict(conversation.metadata_ or {})
    metadata["reseller_name"] = name or reseller.name
    conversation.metadata_ = metadata
    db.flush()
    return _session_response(
        db,
        conversation=conversation,
        surface="reseller_portal",
        reseller_id=reseller.id,
    )


def broker_reseller_session_committed(
    db: Session,
    reseller_id: str,
    principal: dict[str, object],
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, str | None]:
    def action() -> dict[str, str | None]:
        reseller_uuid = coerce_uuid(reseller_id)
        reseller = db.get(Reseller, reseller_uuid) if reseller_uuid else None
        if reseller is None:
            raise _error("reseller_not_found", "Reseller not found.")
        scoped_ticket, scoped_project = _reseller_context(
            db,
            reseller.id,
            ticket_id=ticket_id,
            project_id=project_id,
        )
        return broker_reseller_session(
            db,
            str(reseller.id),
            principal,
            ticket_id=scoped_ticket,
            project_id=scoped_project,
        )

    return _commit(db, action)


def list_session_messages(
    db: Session,
    *,
    principal: WidgetPrincipal,
    limit: int = 50,
) -> dict[str, list[dict[str, Any]]]:
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == principal.conversation_id)
        .order_by(InboxMessage.created_at.desc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    messages.reverse()
    return {
        "messages": [
            {
                "id": str(message.id),
                "message_id": str(message.id),
                "conversation_id": str(message.conversation_id),
                "body": message.body,
                "direction": message.direction,
                "created_at": message.created_at.isoformat()
                if message.created_at
                else None,
                "sender_type": "visitor"
                if message.direction == InboxMessageDirection.inbound.value
                else "agent",
                "from_customer": message.direction
                == InboxMessageDirection.inbound.value,
            }
            for message in messages
            if message.direction != InboxMessageDirection.internal.value
        ]
    }


def add_visitor_message(
    db: Session,
    *,
    principal: WidgetPrincipal,
    body: str,
    client_message_id: str | None = None,
) -> dict[str, Any]:
    _require_enabled(db)
    clean_body = str(body or "").strip()
    if not clean_body:
        raise _error("message_required", "Message body is required.")
    conversation = db.get(InboxConversation, principal.conversation_id)
    if conversation is None or not conversation.is_active:
        raise _error("conversation_not_found", "Conversation not found.")
    now = datetime.now(UTC)
    metadata = {
        "source": "native_chat_widget",
        "client_message_id": client_message_id,
        "session_id": principal.session_id,
        "surface": principal.surface,
    }
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.chat_widget.value,
        direction=InboxMessageDirection.inbound.value,
        body=clean_body,
        from_address=conversation.contact_address,
        received_at=now,
        metadata_=metadata,
    )
    db.add(message)
    conversation.last_message_at = now
    if conversation.status == InboxConversationStatus.resolved.value:
        conversation.status = InboxConversationStatus.open.value
    db.flush()
    payload = team_inbox_realtime.message_event_payload(
        conversation_id=str(conversation.id),
        message_id=str(message.id),
        body=message.body,
        direction=message.direction,
        channel_type=message.channel_type,
        created_at=message.created_at,
        extra={
            "client_message_id": client_message_id,
            "sender_type": "visitor",
            "from_customer": True,
        },
    )
    team_inbox_realtime.publish_conversation_event(
        db,
        str(conversation.id),
        event_type=EventType.MESSAGE_NEW,
        payload=payload,
    )
    return payload


def _require_session_match(principal: WidgetPrincipal, session_id: str) -> None:
    if principal.session_id != session_id:
        raise _error("session_mismatch", "Session mismatch.")


def add_visitor_message_committed(
    db: Session,
    *,
    session_id: str,
    principal: WidgetPrincipal,
    body: str,
    client_message_id: str | None = None,
) -> dict[str, Any]:
    _require_session_match(principal, session_id)
    return _commit(
        db,
        lambda: add_visitor_message(
            db,
            principal=principal,
            body=body,
            client_message_id=client_message_id,
        ),
    )


def mark_session_read(db: Session, *, principal: WidgetPrincipal) -> dict[str, bool]:
    conversation = db.get(InboxConversation, principal.conversation_id)
    if conversation is None:
        raise _error("conversation_not_found", "Conversation not found.")
    metadata = dict(conversation.metadata_ or {})
    metadata["visitor_last_read_at"] = datetime.now(UTC).isoformat()
    conversation.metadata_ = metadata
    db.flush()
    return {"ok": True}


def mark_session_read_committed(
    db: Session,
    *,
    session_id: str,
    principal: WidgetPrincipal,
) -> dict[str, bool]:
    _require_session_match(principal, session_id)
    return _commit(db, lambda: mark_session_read(db, principal=principal))


def record_session_satisfaction_committed(
    db: Session,
    *,
    session_id: str,
    principal: WidgetPrincipal,
    rating: int,
    comment: str | None = None,
) -> dict[str, bool]:
    from app.services import team_inbox_operations

    _require_session_match(principal, session_id)

    def _record() -> dict[str, bool]:
        conversation = db.get(InboxConversation, principal.conversation_id)
        if conversation is None:
            raise _error("conversation_not_found", "Conversation not found.")
        team_inbox_operations.set_satisfaction(
            db,
            conversation=conversation,
            rating=rating,
            comment=comment,
            actor=principal.subscriber_id
            or principal.reseller_id
            or principal.session_id,
        )
        return {"ok": True}

    return _commit(db, _record)
