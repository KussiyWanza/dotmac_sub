"""Committed command boundary for team-inbox UI adapters.

The underlying team-inbox services own their focused policies. This module owns
admin command orchestration, model lookup, and the transaction boundary so web
routes never become a parallel writer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxSavedFilter,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_contact_links,
    team_inbox_operations,
    team_inbox_outbound,
)
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

T = TypeVar("T")


OWNER = "communications.team_inbox_commands"
_ADMIN_MUTATION = OwnerCommandDefinition(
    owner=OWNER,
    concern="operator conversation and collaboration commands",
    name="execute_team_inbox_admin_mutation",
)


class InboxCommandError(DomainError, ValueError):
    """Base error safe for an admin adapter to render."""

    def __init__(
        self,
        message: str,
        *,
        suffix: str = "rejected",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code=f"{OWNER}.{suffix}", message=message, details=details)


class ConversationNotFoundError(InboxCommandError):
    def __init__(self, message: str = "Conversation not found.") -> None:
        super().__init__(message, suffix="conversation_not_found")


class MessageNotFoundError(InboxCommandError):
    def __init__(self, message: str = "Message not found.") -> None:
        super().__init__(message, suffix="message_not_found")


class InboxCommandRejected(InboxCommandError):
    def __init__(self, message: str, *, conversation_id: UUID | str | None = None):
        super().__init__(
            message,
            suffix="command_rejected",
            details={"conversation_id": str(conversation_id)}
            if conversation_id
            else None,
        )
        self.conversation_id = str(conversation_id) if conversation_id else None


@dataclass(frozen=True)
class ReplyOutcome:
    conversation_id: str
    kind: str
    sender: str
    replayed: bool = False


@dataclass(frozen=True)
class ContactLinkOutcome:
    conversation_id: str
    channel_type: str
    target: str


@dataclass(frozen=True)
class BulkActionOutcome:
    message: str


@dataclass(frozen=True)
class StatusOutcome:
    conversation_id: str
    status: str
    already_set: bool


def _commit(
    db: Session,
    action: Callable[[], T],
    *,
    context: CommandContext | None = None,
) -> T:
    command_context = context or CommandContext.system(
        actor="system:team-inbox-admin-adapter",
        scope="team-inbox:operator-command",
        reason="execute typed Team Inbox operator command",
    )
    return execute_owner_command(
        db,
        definition=_ADMIN_MUTATION,
        context=command_context,
        operation=action,
    )


def _active_conversation(
    db: Session,
    conversation_id: str | UUID,
    *,
    for_update: bool = False,
) -> InboxConversation:
    conversation_uuid = coerce_uuid(conversation_id)
    conversation = None
    if conversation_uuid is not None:
        query = db.query(InboxConversation).filter(
            InboxConversation.id == conversation_uuid
        )
        if for_update:
            query = query.with_for_update()
        conversation = query.one_or_none()
    if conversation is None or not conversation.is_active:
        raise ConversationNotFoundError("Conversation not found.")
    return conversation


def reply(
    db: Session,
    *,
    conversation_id: str | UUID,
    body_text: str,
    actor_person_id: str | UUID | None,
    macro_id: str | UUID | None = None,
    template_id: str | UUID | None = None,
    idempotency_key: str | None = None,
    reply_to_message_id: str | UUID | None = None,
) -> ReplyOutcome:
    def action() -> ReplyOutcome:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        clean_body = str(body_text or "").strip()
        clean_idempotency_key = str(idempotency_key or "").strip()
        reply_to_uuid = coerce_uuid(reply_to_message_id)
        if reply_to_message_id and reply_to_uuid is None:
            raise InboxCommandRejected(
                "Quoted message is invalid.",
                conversation_id=conversation.id,
            )
        if len(clean_idempotency_key) > 200:
            raise InboxCommandError("Reply idempotency key is too long.")
        if clean_idempotency_key:
            previous = (
                db.query(InboxMessage)
                .filter(InboxMessage.conversation_id == conversation.id)
                .filter(InboxMessage.direction == "outbound")
                .filter(
                    InboxMessage.metadata_["idempotency_key"].as_string()
                    == clean_idempotency_key
                )
                .order_by(InboxMessage.created_at.desc())
                .first()
            )
            if previous is not None:
                previous_body = str(
                    (previous.metadata_ or {}).get("body_text") or ""
                ).strip()
                previous_reply = (previous.metadata_ or {}).get("reply_to")
                previous_reply_id = (
                    str(previous_reply.get("message_id") or "")
                    if isinstance(previous_reply, dict)
                    else ""
                )
                requested_reply_id = str(reply_to_uuid) if reply_to_uuid else ""
                if (
                    previous_body
                    and previous_body != clean_body
                    or previous_reply_id != requested_reply_id
                ):
                    raise InboxCommandRejected(
                        "This send key was already used for a different reply.",
                        conversation_id=conversation.id,
                    )
                return ReplyOutcome(
                    conversation_id=str(conversation.id),
                    kind=str(
                        (previous.metadata_ or {}).get("delivery_status") or "queued"
                    ),
                    sender=previous.from_address or "team sender",
                    replayed=True,
                )
        template = None
        clean_template_id = (
            str(template_id).strip()
            if isinstance(template_id, (str, UUID)) and str(template_id).strip()
            else None
        )
        if clean_template_id:
            template = team_inbox_operations.get_template(db, clean_template_id)
            if not clean_body:
                clean_body = template.body_text.strip()
        if not clean_body:
            raise InboxCommandError("Reply body is required.")

        body_html = (
            "<p>"
            + "<br>".join(escape(line) for line in clean_body.splitlines())
            + "</p>"
        )
        reply_metadata: dict[str, object] = {
            "source_route": "admin_inbox_detail_reply",
            "template_id": str(template.id) if template is not None else None,
            "idempotency_key": clean_idempotency_key or None,
        }
        if reply_to_uuid is not None:
            quoted_message = db.get(InboxMessage, reply_to_uuid)
            if (
                quoted_message is None
                or quoted_message.conversation_id != conversation.id
            ):
                raise InboxCommandRejected(
                    "Quoted message does not belong to this conversation.",
                    conversation_id=conversation.id,
                )
            reply_metadata["reply_to"] = {
                "message_id": str(quoted_message.id),
                "author": quoted_message.from_address
                or (
                    "Support agent"
                    if quoted_message.direction == "outbound"
                    else "Customer"
                ),
                "excerpt": str(quoted_message.body or "")[:240],
            }
        if (
            template is not None
            and conversation.channel_type == InboxChannelType.whatsapp.value
        ):
            template_metadata = dict(template.metadata_ or {})
            provider_template_name = str(
                template_metadata.get("provider_template_name")
                or template_metadata.get("whatsapp_template_name")
                or ""
            ).strip()
            if provider_template_name:
                variables = template_metadata.get("provider_template_variables")
                reply_metadata["whatsapp_template"] = {
                    "name": provider_template_name,
                    "language": str(
                        template_metadata.get("provider_template_language") or ""
                    ).strip()
                    or None,
                    "variables": variables if isinstance(variables, dict) else {},
                    "inbox_template_id": str(template.id),
                }

        result = team_inbox_outbound.send_inbox_reply(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.InboxReplyPayload(
                body_html=body_html,
                body_text=clean_body,
                subject=template.subject if template is not None else None,
                sent_by_person_id=actor_person_id,
                metadata=reply_metadata,
            ),
            record_failure=True,
        )
        if result.kind not in {"sent", "queued"}:
            raise InboxCommandRejected(
                result.reason or "Reply could not be sent.",
                conversation_id=conversation.id,
            )
        team_inbox_operations.record_macro_use(db, macro_id)
        return ReplyOutcome(
            conversation_id=str(conversation.id),
            kind=result.kind,
            sender=result.from_address or result.sender_key or "team sender",
        )

    return _commit(db, action)


def create_label(db: Session, *, name: str, color: str | None = None) -> None:
    _commit(
        db,
        lambda: team_inbox_operations.create_or_reactivate_label(
            db, name=name, color=color
        ),
    )


def apply_label(
    db: Session,
    *,
    conversation_id: str | UUID,
    label_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> None:
    def action() -> None:
        conversation = _active_conversation(db, conversation_id)
        team_inbox_operations.apply_label(
            db,
            conversation=conversation,
            label_id=label_id,
            applied_by_person_id=actor_person_id,
        )

    _commit(db, action)


def remove_label(
    db: Session,
    *,
    conversation_id: str | UUID,
    label_id: str | UUID,
) -> None:
    def action() -> None:
        team_inbox_operations.remove_label(
            db,
            conversation=_active_conversation(db, conversation_id),
            label_id=label_id,
        )

    _commit(db, action)


def create_macro(
    db: Session,
    *,
    name: str,
    body_text: str,
    description: str | None = None,
    visibility: str = "shared",
    actor_person_id: str | UUID | None = None,
) -> None:
    _commit(
        db,
        lambda: team_inbox_operations.create_macro(
            db,
            name=name,
            body_text=body_text,
            description=description,
            visibility=visibility,
            created_by_person_id=actor_person_id,
        ),
    )


def create_template(
    db: Session,
    *,
    name: str,
    channel_type: str,
    subject: str | None,
    body_text: str,
    provider_template_name: str | None = None,
    provider_template_language: str | None = None,
) -> None:
    metadata = {
        key: value
        for key, value in {
            "provider_template_name": str(provider_template_name or "").strip(),
            "provider_template_language": str(provider_template_language or "").strip(),
        }.items()
        if value
    }
    _commit(
        db,
        lambda: team_inbox_operations.create_template(
            db,
            name=name,
            channel_type=channel_type,
            subject=subject,
            body_text=body_text,
            metadata=metadata or None,
        ),
    )


def retry_message(
    db: Session,
    *,
    message_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> str:
    def action() -> str:
        message_uuid = coerce_uuid(message_id)
        message = db.get(InboxMessage, message_uuid) if message_uuid else None
        if message is None:
            raise MessageNotFoundError("Message not found.")
        result = team_inbox_outbound.retry_outbound_message(
            db,
            message=message,
            sent_by_person_id=actor_person_id,
        )
        if result.kind not in {"sent", "queued"}:
            raise InboxCommandRejected(
                result.reason or "Retry failed.",
                conversation_id=message.conversation_id,
            )
        return str(message.conversation_id)

    return _commit(db, action)


def retry_failed_batch(db: Session, *, limit: int = 50) -> int:
    def action() -> int:
        result = team_inbox_operations.retry_failed_outbound_batch(db, limit=limit)
        retried = result.get("retried")
        return len(retried) if isinstance(retried, list) else 0

    return _commit(db, action)


def update_workflow(
    db: Session,
    *,
    conversation_id: str | UUID,
    priority: int | None = None,
    is_muted: bool | None = None,
    snooze_minutes: int | None = None,
    actor_person_id: str | UUID | None = None,
) -> None:
    def action() -> None:
        team_inbox_operations.update_conversation_workflow(
            db,
            conversation=_active_conversation(db, conversation_id),
            priority=priority,
            is_muted=is_muted,
            snooze_minutes=snooze_minutes,
            actor_person_id=actor_person_id,
        )

    _commit(db, action)


def save_filter(
    db: Session,
    *,
    name: str,
    filter_payload: dict[str, Any],
    actor_person_id: str | UUID | None = None,
    is_shared: bool = False,
) -> None:
    _commit(
        db,
        lambda: team_inbox_operations.save_filter(
            db,
            name=name,
            filter_payload=filter_payload,
            owner_person_id=actor_person_id,
            is_shared=is_shared,
        ),
    )


def delete_filter(
    db: Session,
    *,
    filter_id: str | UUID,
    actor_person_id: str | UUID | None,
) -> None:
    def action() -> None:
        filter_uuid = coerce_uuid(filter_id)
        actor_uuid = coerce_uuid(actor_person_id)
        saved_filter = db.get(InboxSavedFilter, filter_uuid) if filter_uuid else None
        if saved_filter is None or not saved_filter.is_active:
            raise InboxCommandError("Saved filter not found.")
        if actor_uuid is None or saved_filter.owner_person_id != actor_uuid:
            raise InboxCommandRejected("Only the saved view owner can delete it.")
        team_inbox_operations.delete_saved_filter(db, filter_id=saved_filter.id)

    _commit(db, action)


def bulk_action(
    db: Session,
    *,
    conversation_ids: Sequence[str | UUID],
    action: str,
    status_value: str | None = None,
    priority: int | None = None,
    label_id: str | UUID | None = None,
    service_team_id: str | UUID | None = None,
    assigned_person_id: str | UUID | None = None,
    auto_assign: bool = True,
    actor_person_id: str | UUID | None = None,
) -> BulkActionOutcome:
    if not conversation_ids:
        raise InboxCommandError("Select at least one conversation.")

    def execute() -> BulkActionOutcome:
        if action == "status":
            result = team_inbox_operations.bulk_update_status(
                db,
                conversation_ids=conversation_ids,
                status_value=status_value or "",
                actor_person_id=actor_person_id,
            )
            verb = "Updated"
            noun = "conversation statuses"
        elif action == "priority":
            result = team_inbox_operations.bulk_update_priority(
                db,
                conversation_ids=conversation_ids,
                priority=priority,
                actor_person_id=actor_person_id,
            )
            verb = "Updated priority for"
            noun = "conversations"
        elif action == "label":
            result = team_inbox_operations.bulk_apply_label(
                db,
                conversation_ids=conversation_ids,
                label_id=label_id or "",
                actor_person_id=actor_person_id,
            )
            verb = "Applied label to"
            noun = "conversations"
        elif action == "escalate":
            result = team_inbox_operations.bulk_escalate(
                db,
                conversation_ids=conversation_ids,
                service_team_id=service_team_id or "",
                assigned_person_id=assigned_person_id,
                auto_assign=auto_assign,
                actor_person_id=actor_person_id,
                reason="Bulk inbox escalation",
            )
            verb = "Escalated"
            noun = "conversations"
        else:
            raise InboxCommandError("Unsupported bulk action.")
        updated = result.get("updated")
        count = len(updated) if isinstance(updated, list) else 0
        return BulkActionOutcome(message=f"{verb} {count} {noun}.")

    return _commit(db, execute)


def link_contact(
    db: Session,
    *,
    conversation_id: str | UUID,
    target_type: str,
    subscriber_id: str | UUID | None = None,
    reseller_id: str | UUID | None = None,
    subscriber_id_manual: str | UUID | None = None,
    reseller_id_manual: str | UUID | None = None,
    actor_person_id: str | UUID | None = None,
    note: str | None = None,
) -> ContactLinkOutcome:
    def action() -> ContactLinkOutcome:
        conversation = _active_conversation(db, conversation_id)
        selected_subscriber = (
            str(subscriber_id_manual or subscriber_id or "").strip() or None
        )
        selected_reseller = str(reseller_id_manual or reseller_id or "").strip() or None
        if target_type == "subscriber":
            selected_reseller = None
        elif target_type == "reseller":
            selected_subscriber = None
        else:
            raise InboxCommandError(
                "Choose whether this contact belongs to a subscriber or reseller."
            )
        result = team_inbox_contact_links.link_conversation_contact(
            db,
            conversation=conversation,
            subscriber_id=selected_subscriber,
            reseller_id=selected_reseller,
            linked_by_person_id=actor_person_id,
            note=note,
        )
        return ContactLinkOutcome(
            conversation_id=str(conversation.id),
            channel_type=conversation.channel_type,
            target="subscriber" if result.subscriber_id else "reseller",
        )

    return _commit(db, action)


def create_internal_note(
    db: Session,
    *,
    conversation_id: str | UUID,
    body: str,
    actor_person_id: str | UUID | None = None,
) -> None:
    def action() -> None:
        team_inbox_operations.create_internal_note(
            db,
            conversation=_active_conversation(db, conversation_id),
            body=body,
            actor_person_id=actor_person_id,
        )

    _commit(db, action)


def create_comment(
    db: Session,
    *,
    conversation_id: str | UUID,
    body: str,
    message_id: str | UUID | None = None,
    actor_person_id: str | UUID | None = None,
) -> None:
    def action() -> None:
        team_inbox_operations.create_comment(
            db,
            conversation=_active_conversation(db, conversation_id),
            body=body,
            message_id=message_id,
            author_person_id=actor_person_id,
        )

    _commit(db, action)


def resolve_comment(
    db: Session,
    *,
    comment_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> str:
    def action() -> str:
        comment = team_inbox_operations.resolve_comment(
            db,
            comment_id=comment_id,
            resolved_by_person_id=actor_person_id,
        )
        return str(comment.conversation_id)

    return _commit(db, action)


def update_status(
    db: Session,
    *,
    conversation_id: str | UUID,
    status_value: str,
    actor_person_id: str | UUID | None = None,
) -> StatusOutcome:
    clean_status = str(status_value or "").strip().lower()
    allowed_statuses = {item.value for item in InboxConversationStatus}
    if clean_status not in allowed_statuses:
        raise InboxCommandError("Unsupported conversation status.")

    def action() -> StatusOutcome:
        conversation = _active_conversation(db, conversation_id)
        previous_status = conversation.status
        if previous_status == clean_status:
            return StatusOutcome(
                conversation_id=str(conversation.id),
                status=clean_status,
                already_set=True,
            )
        metadata = dict(conversation.metadata_ or {})
        history = metadata.get("status_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "from": previous_status,
                "to": clean_status,
                "at": datetime.now(UTC).isoformat(),
                "actor_id": str(actor_person_id) if actor_person_id else None,
                "source": "admin_inbox_status_action",
            }
        )
        metadata["status_history"] = history[-50:]
        conversation.status = clean_status
        conversation.metadata_ = metadata
        return StatusOutcome(
            conversation_id=str(conversation.id),
            status=clean_status,
            already_set=False,
        )

    return _commit(db, action)


def assign_conversation(
    db: Session,
    *,
    conversation_id: str | UUID,
    service_team_id: str | UUID,
    person_id: str | UUID,
    actor_person_id: str | UUID | None = None,
    reason: str | None = None,
) -> team_inbox_assignment.InboxAssignmentResult:
    """Assign one conversation to one agent.

    ``team_inbox_assignment`` decides routing and records the assignment;
    this is its committed entry point. Bulk escalation already had one through
    ``bulk_action(action="escalate")`` — the single-conversation case did not,
    which is why the workspace could only hand a thread to a teammate by
    pretending it was a bulk action of one.
    """

    def action() -> team_inbox_assignment.InboxAssignmentResult:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        return team_inbox_assignment.assign_conversation_to_agent(
            db,
            conversation=conversation,
            service_team_id=service_team_id,
            person_id=person_id,
            assigned_by_person_id=actor_person_id,
            reason=reason,
        )

    return _commit(db, action)


def run_macro(
    db: Session,
    *,
    conversation_id: str | UUID,
    macro_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> dict[str, object]:
    """Execute a macro's actions against one conversation.

    Distinct from inserting a macro body into the composer: that is text, this
    runs the macro's recorded actions (labels, status, assignment) through
    ``team_inbox_operations`` and counts the use. Until now
    ``execute_macro_actions`` had no committed entry point and no caller.
    """

    def action() -> dict[str, object]:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        return team_inbox_operations.execute_macro_actions(
            db,
            conversation=conversation,
            macro_id=macro_id,
            actor_person_id=actor_person_id,
        )

    return _commit(db, action)
