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

from app.models.audit import AuditActorType
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
    team_inbox_field_job,
    team_inbox_media,
    team_inbox_operations,
    team_inbox_outbound,
    team_inbox_participants,
    team_inbox_routing,
)
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
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
    if owner_command_active(db):
        return action()
    from app.services.db_session_adapter import db_session_adapter

    db_session_adapter.release_read_transaction(db)
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
    attachment_ids: Sequence[str] | None = None,
    send_after: datetime | None = None,
    idempotency_key: str | None = None,
    reply_to_message_id: str | UUID | None = None,
) -> ReplyOutcome:
    def action() -> ReplyOutcome:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        clean_body = str(body_text or "").strip()
        scheduled_for = send_after
        if scheduled_for is not None:
            if scheduled_for.tzinfo is None:
                scheduled_for = scheduled_for.replace(tzinfo=UTC)
            if scheduled_for <= datetime.now(UTC):
                raise InboxCommandError("Choose a send time in the future.")
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

        if scheduled_for is not None:
            scheduled = team_inbox_outbound.schedule_inbox_reply(
                db,
                conversation=conversation,
                payload=team_inbox_outbound.InboxReplyPayload(
                    body_html=body_html,
                    body_text=clean_body,
                    subject=template.subject if template is not None else None,
                    sent_by_person_id=actor_person_id,
                    metadata=reply_metadata,
                ),
                send_after=scheduled_for,
            )
            if attachment_ids:
                team_inbox_media.bind_assets_to_message(
                    db, message=scheduled, asset_ids=list(attachment_ids)
                )
            team_inbox_operations.record_macro_use(db, macro_id)
            return ReplyOutcome(
                conversation_id=str(conversation.id),
                kind="scheduled",
                sender="scheduled",
            )
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
        # Bind staged uploads to the message that actually carried them, inside
        # the same command — an attachment must never outlive a reply that
        # failed to send.
        if attachment_ids and result.message_id:
            message = db.get(InboxMessage, coerce_uuid(result.message_id))
            if message is not None:
                team_inbox_media.bind_assets_to_message(
                    db, message=message, asset_ids=list(attachment_ids)
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
    snooze_until: datetime | None = None,
    snooze_until_reply: bool = False,
    actor_person_id: str | UUID | None = None,
) -> None:
    def action() -> None:
        conversation = _active_conversation(db, conversation_id)
        if snooze_until_reply:
            # No wake time — the customer's next message wakes it. Priority and
            # mute still apply; they arrived in the same submit and dropping
            # them would silently discard half the operator's action.
            if priority is not None or is_muted is not None:
                team_inbox_operations.update_conversation_workflow(
                    db,
                    conversation=conversation,
                    priority=priority,
                    is_muted=is_muted,
                    actor_person_id=actor_person_id,
                )
            team_inbox_operations.snooze_until_reply(
                db, conversation=conversation, actor_person_id=actor_person_id
            )
            return
        team_inbox_operations.update_conversation_workflow(
            db,
            conversation=conversation,
            priority=priority,
            is_muted=is_muted,
            snooze_minutes=snooze_minutes,
            snooze_until=snooze_until,
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


def create_email_route(
    db: Session,
    *,
    service_team_id: str | UUID,
    email_address: str,
    is_primary: bool = False,
    priority: int = 100,
) -> str:
    """Route an inbound mailbox to a service team.

    `team_inbox_routing` owns the routing table; this is its committed entry
    point. Until now the table had no writer at all outside direct SQL, which
    is why production ran six live mailboxes against zero rows.
    """

    def action() -> str:
        route = team_inbox_routing.create_email_route(
            db,
            service_team_id=service_team_id,
            email_address=email_address,
            is_primary=is_primary,
            priority=priority,
        )
        # Captured inside the transaction: reading it afterwards would re-open
        # one and the next owner command refuses a session already in a
        # transaction.
        return str(route.id)

    return _commit(db, action)


def update_email_route(
    db: Session,
    *,
    route_id: str | UUID,
    is_primary: bool | None = None,
    priority: int | None = None,
    is_active: bool | None = None,
) -> None:
    def action() -> None:
        team_inbox_routing.update_email_route(
            db,
            route_id,
            is_primary=is_primary,
            priority=priority,
            is_active=is_active,
        )

    _commit(db, action)


def delete_email_route(db: Session, *, route_id: str | UUID) -> None:
    def action() -> None:
        team_inbox_routing.delete_email_route(db, route_id)

    _commit(db, action)


def stage_attachments(
    db: Session,
    *,
    conversation_id: str | UUID,
    uploads: Sequence[tuple[str, str | None, bytes]],
    actor_person_id: str | UUID | None = None,
) -> list[str]:
    """Store operator-supplied files against a conversation.

    Returns the staged asset ids so the composer can submit them with the reply
    they belong to. They stay unbound until that reply is sent, so abandoning
    the composer leaves no attachment claiming to belong to a message.
    """

    def action() -> list[str]:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        staged: list[str] = []
        for file_name, content_type, data in uploads:
            asset = team_inbox_media.stage_outbound_attachment(
                db,
                conversation=conversation,
                file_name=file_name,
                content_type=content_type,
                data=data,
                uploaded_by=str(actor_person_id) if actor_person_id else None,
            )
            staged.append(str(asset.id))
        return staged

    return _commit(db, action)


@dataclass(frozen=True)
class StartConversationOutcome:
    conversation_id: str
    kind: str
    sender: str
    contact_status: str


def start_conversation(
    db: Session,
    *,
    channel_type: str,
    contact_address: str,
    body_text: str,
    subject: str | None = None,
    service_team_id: str | UUID | None = None,
    subscriber_id: str | UUID | None = None,
    actor_person_id: str | UUID | None = None,
    attachment_ids: Sequence[str] | None = None,
    contact_name: str | None = None,
    template_id: str | UUID | None = None,
    template_values: Sequence[str] | None = None,
    uploads: Sequence[tuple[str, str | None, bytes]] | None = None,
) -> StartConversationOutcome:
    """Open a new outbound conversation and send its first message.

    Reuses the inbound contact resolver, so a thread an operator starts resolves
    to the same subscriber an inbound message from that address would. An
    unmatched address is allowed — the operator may be reaching someone the
    system does not know yet — and the resolution status is recorded on the
    conversation so the drawer can offer a contact link rather than silently
    showing an anonymous thread.
    """
    from app.models.team_inbox import InboxConversationStatus
    from app.services import team_inbox_channel_receive

    def action() -> StartConversationOutcome:
        clean_channel = str(channel_type or "").strip().lower()
        clean_body = str(body_text or "").strip()
        if clean_channel not in {c.value for c in InboxChannelType}:
            raise InboxCommandError("Choose a channel for this conversation.")
        if not str(contact_address or "").strip():
            raise InboxCommandError("Enter who this conversation is with.")
        template = None
        if template_id is not None and str(template_id).strip():
            template = team_inbox_operations.get_template(db, str(template_id))
            if template.channel_type not in {clean_channel, "any"}:
                raise InboxCommandError(
                    "The selected template is not available for this channel."
                )
            if not clean_body:
                clean_body = str(template.body_text or "").strip()
        if not clean_body:
            raise InboxCommandError("Enter the first message.")

        resolution = team_inbox_channel_receive.resolve_contact_context(
            db,
            channel_type=clean_channel,
            contact_address=contact_address,
            subscriber_id=subscriber_id,
        )

        conversation_metadata: dict[str, object] = {
            "source": "operator_initiated",
            "contact_resolution": resolution.as_metadata(),
        }
        clean_contact_name = str(contact_name or "").strip()
        if clean_contact_name:
            conversation_metadata["contact_name"] = clean_contact_name[:200]
        conversation = InboxConversation(
            channel_type=clean_channel,
            subject=(
                (subject or "").strip()
                or (str(template.subject or "").strip() if template is not None else "")
            )[:200]
            or None,
            contact_address=resolution.normalized_contact or contact_address.strip(),
            status=InboxConversationStatus.open.value,
            subscriber_id=resolution.subscriber_id,
            primary_service_team_id=coerce_uuid(service_team_id),
            first_message_at=datetime.now(UTC),
            metadata_=conversation_metadata,
        )
        db.add(conversation)
        db.flush()

        # Give the thread an owning team link, not just a primary id. An
        # operator-started conversation used to have no `InboxConversationTeam`
        # row at all, so it was invisible to every team filter and to "My team"
        # the moment it was created — including to the operator who started it.
        team_inbox_routing.apply_email_routing_plan(
            db,
            conversation=conversation,
            plan=team_inbox_routing.build_email_team_routing_plan(
                db,
                to_addresses=[],
                cc_addresses=[],
                fallback_service_team_id=(
                    coerce_uuid(service_team_id)
                    or team_inbox_routing.default_service_team_id(db)
                ),
            ),
        )
        staged_attachment_ids = list(attachment_ids or ())
        for file_name, content_type, data in uploads or ():
            asset = team_inbox_media.stage_outbound_attachment(
                db,
                conversation=conversation,
                file_name=file_name,
                content_type=content_type,
                data=data,
                uploaded_by=str(actor_person_id) if actor_person_id else None,
            )
            staged_attachment_ids.append(str(asset.id))

        body_html = (
            "<p>"
            + "<br>".join(escape(line) for line in clean_body.splitlines())
            + "</p>"
        )
        reply_metadata: dict[str, object] = {
            "source": "operator_initiated",
            "template_id": str(template.id) if template is not None else None,
        }
        if template is not None and clean_channel == InboxChannelType.whatsapp.value:
            template_metadata = dict(template.metadata_ or {})
            provider_template_name = str(
                template_metadata.get("provider_template_name")
                or template_metadata.get("whatsapp_template_name")
                or ""
            ).strip()
            if provider_template_name:
                submitted_values = tuple(
                    str(value).strip()
                    for value in (template_values or ())
                    if str(value).strip()
                )
                configured_values = template_metadata.get("provider_template_variables")
                reply_metadata["whatsapp_template"] = {
                    "name": provider_template_name,
                    "language": str(
                        template_metadata.get("provider_template_language") or ""
                    ).strip()
                    or None,
                    "variables": (
                        {
                            str(index): value
                            for index, value in enumerate(submitted_values, 1)
                        }
                        if submitted_values
                        else (
                            configured_values
                            if isinstance(configured_values, dict)
                            else {}
                        )
                    ),
                    "inbox_template_id": str(template.id),
                }
        result = team_inbox_outbound.send_inbox_reply(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.InboxReplyPayload(
                body_html=body_html,
                body_text=clean_body,
                subject=conversation.subject,
                sent_by_person_id=actor_person_id,
                metadata=reply_metadata,
            ),
            record_failure=True,
        )
        if result.kind not in {"sent", "queued"}:
            # Fail the whole command: a conversation whose opening message never
            # left is worse than no conversation, because the queue would show a
            # thread the customer never received.
            raise InboxCommandRejected(
                result.reason or "Could not send the first message.",
                conversation_id=conversation.id,
            )

        if staged_attachment_ids and result.message_id:
            message = db.get(InboxMessage, coerce_uuid(result.message_id))
            if message is not None:
                team_inbox_media.bind_assets_to_message(
                    db,
                    message=message,
                    asset_ids=staged_attachment_ids,
                )

        return StartConversationOutcome(
            conversation_id=str(conversation.id),
            kind=result.kind,
            sender=result.from_address or result.sender_key or "team sender",
            contact_status=resolution.status,
        )

    return _commit(db, action)


TRANSCRIPT_AUDIT_ACTION = "conversation.transcript_exported"


def _recipient_is_on_record(
    db: Session,
    *,
    conversation: InboxConversation,
    recipient: str,
) -> bool:
    """Whether this address already appears on the conversation or its customer.

    The decisive field in the export audit. Restricting transcripts to
    addresses already on the record is the tightest available control, but
    whether it is affordable depends on how often operators send elsewhere —
    and nothing recorded that. This answers it from real use.
    """
    normalized = team_inbox_routing.normalize_email_address(recipient)
    if not normalized:
        return False
    known = {team_inbox_routing.normalize_email_address(conversation.contact_address)}
    if conversation.subscriber_id is not None:
        from app.models.subscriber import Subscriber

        subscriber = db.get(Subscriber, conversation.subscriber_id)
        if subscriber is not None:
            known.add(team_inbox_routing.normalize_email_address(subscriber.email))
    return normalized in {value for value in known if value}


def _recipient_seen_on_thread(
    db: Session,
    *,
    conversation: InboxConversation,
    recipient: str,
) -> bool:
    """Whether this address was ever observed in the thread's own headers.

    `recipient_on_record` is measured against a scalar `contact_address`, so a
    genuine participant — a colleague on the Cc line, a vendor who replied —
    scores false and reads as an exception. Counting those as exceptions would
    overstate how often operators export outside the conversation, and a
    restriction policy judged on that figure would be judged on the wrong
    number.
    """
    normalized = team_inbox_routing.normalize_email_address(recipient)
    if not normalized:
        return False
    return team_inbox_participants.endpoint_is_participant(
        db,
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        endpoint=normalized,
    )


def email_transcript(
    db: Session,
    *,
    conversation_id: str | UUID,
    recipient: str,
    actor_person_id: str | UUID | None = None,
    actor_type: AuditActorType = AuditActorType.user,
    request_id: str | None = None,
) -> str:
    """Email a conversation transcript to a chosen address.

    Sends through the same outbound path a reply uses, so the transcript
    inherits the team's sender and delivery handling rather than inventing a
    second way to send mail. Internal notes and comments are excluded by the
    renderer — a transcript is often forwarded onward.

    Exporting a whole customer conversation to an arbitrary address is the
    widest data-egress path in this module and rides the ordinary
    ``support:ticket:update`` permission, so every export is audited. The audit
    records rather than prevents; whether this also needs its own permission or
    a recipient restriction is a policy decision the recorded evidence is meant
    to inform.
    """

    def action() -> str:
        conversation = _active_conversation(db, conversation_id)
        clean_recipient = str(recipient or "").strip()
        if "@" not in clean_recipient:
            raise InboxCommandError("Enter a valid email address.")

        transcript = team_inbox_operations.render_conversation_transcript(
            db, conversation=conversation
        )
        result = team_inbox_outbound.send_transcript(
            db,
            conversation=conversation,
            recipient=clean_recipient,
            subject=transcript.subject,
            body_html=transcript.html,
            sent_by_person_id=actor_person_id,
        )
        if result.kind not in {"sent", "queued"}:
            raise InboxCommandRejected(
                result.reason or "Could not send the transcript.",
                conversation_id=conversation.id,
            )
        # Staged inside the command, so the record commits with the send or not
        # at all — an export can never leave without its audit row.
        stage_audit_event(
            db,
            action=TRANSCRIPT_AUDIT_ACTION,
            entity_type="inbox_conversation",
            entity_id=str(conversation.id),
            actor_type=actor_type,
            actor_id=str(actor_person_id) if actor_person_id else None,
            request_id=request_id,
            metadata={
                "owner": OWNER,
                "recipient": clean_recipient,
                "recipient_on_record": _recipient_is_on_record(
                    db, conversation=conversation, recipient=clean_recipient
                ),
                "recipient_seen_on_thread": _recipient_seen_on_thread(
                    db, conversation=conversation, recipient=clean_recipient
                ),
                "channel_type": conversation.channel_type,
                "subscriber_id": str(conversation.subscriber_id)
                if conversation.subscriber_id
                else None,
                "message_count": transcript.message_count,
            },
        )
        return clean_recipient

    return _commit(db, action)


def record_field_job_customer_message(
    db: Session,
    *,
    work_order_public_id: str,
    body: str,
    author_name: str | None = None,
) -> dict:
    """Commit one customer message on a job chat.

    The portal adapter has already established that the caller owns the visit;
    this is the family's commit boundary for that write, so the inbox owner
    itself stays transaction-free.
    """

    def action() -> dict:
        conversation = (
            db.query(InboxConversation)
            .filter(
                InboxConversation.channel_type == team_inbox_field_job.FIELD_JOB_CHANNEL
            )
            .filter(InboxConversation.external_thread_id == work_order_public_id)
            .one_or_none()
        )
        if conversation is None:
            raise ConversationNotFoundError()
        return team_inbox_field_job.record_customer_message(
            db,
            conversation=conversation,
            body=body,
            author_name=author_name,
        )

    return _commit(db, action)
