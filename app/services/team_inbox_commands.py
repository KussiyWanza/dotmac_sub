"""Committed command boundary for team-inbox UI adapters.

The underlying team-inbox services own their focused policies. This module owns
admin command orchestration, model lookup, and the transaction boundary so web
routes never become a parallel writer.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from html import escape
from typing import Any, TypeVar
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.notification import Notification, NotificationChannel
from app.models.organization import Organization
from app.models.party import (
    Party,
    PartyContactPointType,
    PartyRelationship,
    PartyRelationshipType,
    PartyRoleStatus,
    PartyRoleType,
    PartyType,
)
from app.models.sales import Lead, LeadCaptureMethod, LeadSourcePlatform
from app.models.service_team import ServiceTeamMember
from app.models.subscriber import Reseller, Subscriber
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
    InboxSavedFilter,
)
from app.schemas.notification import NotificationCreate
from app.schemas.sales import (
    LeadCapturePartyCreate,
    LeadCaptureRequest,
    LeadContactObservation,
    LeadOriginCaptureCreate,
)
from app.services import (
    party as party_service,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_contact_links,
    team_inbox_field_job,
    team_inbox_filters,
    team_inbox_media,
    team_inbox_operations,
    team_inbox_outbound,
    team_inbox_participants,
    team_inbox_routing,
    team_inbox_status,
)
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.domain_errors import DomainError
from app.services.notification import Notifications
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)
from app.services.sales import account_conversion
from app.services.sales import capture as sales_capture
from app.services.validation_api import validate_email_format

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


class ConversationBusyError(InboxCommandError):
    def __init__(self) -> None:
        super().__init__(
            "Another conversation update is completing. Please retry.",
            suffix="conversation_busy",
        )


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
    message_id: str | None = None
    notification_id: UUID | None = None
    replayed: bool = False


class WhatsAppTemplateComponentKind(StrEnum):
    header = "header"
    body = "body"
    button = "button"


class WhatsAppTemplateParameterKind(StrEnum):
    text = "text"
    image = "image"
    video = "video"
    document = "document"


@dataclass(frozen=True, slots=True)
class WhatsAppTemplateParameter:
    kind: WhatsAppTemplateParameterKind
    value: str


@dataclass(frozen=True, slots=True)
class WhatsAppTemplateComponent:
    kind: WhatsAppTemplateComponentKind
    parameters: tuple[WhatsAppTemplateParameter, ...]
    button_index: int | None = None


@dataclass(frozen=True, slots=True)
class ReplyCommand:
    conversation_id: UUID
    body_text: str
    actor_person_id: UUID | None
    macro_id: UUID | None = None
    template_id: UUID | None = None
    attachment_ids: tuple[str, ...] = ()
    send_after: datetime | None = None
    idempotency_key: str | None = None
    reply_to_message_id: UUID | None = None
    whatsapp_template_name: str | None = None
    whatsapp_template_language: str | None = None
    whatsapp_template_components: tuple[WhatsAppTemplateComponent, ...] = ()


@dataclass(frozen=True)
class ContactLinkOutcome:
    conversation_id: str
    channel_type: str
    target: str


@dataclass(frozen=True)
class LeadCreationOutcome:
    conversation_id: str
    lead_id: str
    party_id: str
    replayed: bool


@dataclass(frozen=True)
class LeadMergeOutcome:
    conversation_id: str
    lead_id: str
    target_type: str
    target_id: str
    subscriber_id: str | None = None
    reseller_id: str | None = None
    organization_id: str | None = None


@dataclass(frozen=True)
class ContactMergeOutcome:
    conversation_id: str
    target_type: str
    target_id: str
    lead_id: str | None = None


@dataclass(frozen=True)
class BulkActionOutcome:
    message: str


@dataclass(frozen=True)
class StatusOutcome:
    conversation_id: str
    status: str
    already_set: bool


@dataclass(frozen=True)
class AgentPresenceOutcome:
    person_id: str
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
    nowait: bool = False,
) -> InboxConversation:
    conversation_uuid = coerce_uuid(conversation_id)
    conversation = None
    if conversation_uuid is not None:
        query = db.query(InboxConversation).filter(
            InboxConversation.id == conversation_uuid
        )
        if for_update:
            query = query.with_for_update(nowait=nowait)
        conversation = query.one_or_none()
    if conversation is None or not conversation.is_active:
        raise ConversationNotFoundError("Conversation not found.")
    return conversation


def _is_lock_unavailable(exc: OperationalError) -> bool:
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate == "55P03"


def _normalize_email_recipients(
    values: Sequence[str],
    *,
    label: str,
    limit: int = 20,
) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        raw = str(raw_value or "").strip()
        if not raw:
            continue
        address = team_inbox_routing.normalize_email_address(raw)
        valid, _message = validate_email_format(address or "")
        if not address or not valid:
            raise InboxCommandError(f"Invalid {label} email address: {raw}")
        if address in seen:
            continue
        seen.add(address)
        normalized.append(address)
    if len(normalized) > limit:
        raise InboxCommandError(f"{label} allows at most {limit} email addresses.")
    return tuple(normalized)


def split_email_recipients(value: str | None) -> tuple[str, ...]:
    """Split a form field without deciding whether any address is valid."""

    return tuple(
        item.strip()
        for item in re.split(r"[,;\r\n]+", str(value or ""))
        if item.strip()
    )


_WHATSAPP_COUNTRY_CODES = {
    "NG": "234",
    "GH": "233",
    "ZA": "27",
    "KE": "254",
    "GB": "44",
    "US": "1",
}


def _normalize_whatsapp_recipient(value: str, country_code: str | None) -> str:
    calling_code = _WHATSAPP_COUNTRY_CODES.get(
        str(country_code or "NG").strip().upper(),
        _WHATSAPP_COUNTRY_CODES["NG"],
    )
    normalized = normalize_phone_identifier(
        value,
        default_country_code=calling_code,
    )
    if not normalized or len(re.sub(r"\D", "", normalized)) < 7:
        raise InboxCommandError("WhatsApp number is required.")
    return normalized


def _whatsapp_party_address(
    db: Session,
    party_id: str | UUID | None,
) -> str | None:
    """Resolve the preferred active WhatsApp-capable endpoint for one Party."""

    party_uuid = coerce_uuid(party_id)
    if party_uuid is None:
        return None
    from app.models.party import Party, PartyContactPoint, PartyIdentityStatus

    points = (
        db.query(PartyContactPoint)
        .join(Party, Party.id == PartyContactPoint.party_id)
        .filter(Party.id == party_uuid)
        .filter(Party.status == PartyIdentityStatus.active.value)
        .filter(PartyContactPoint.is_active.is_(True))
        .filter(PartyContactPoint.channel_type.in_(("whatsapp", "phone", "sms")))
        .all()
    )
    channel_rank = {"whatsapp": 0, "phone": 1, "sms": 2}
    points.sort(
        key=lambda point: (
            channel_rank.get(point.channel_type, 9),
            not point.is_primary,
            point.created_at,
        )
    )
    for point in points:
        address = str(point.normalized_value or point.display_value or "").strip()
        if address:
            return address
    return None


def _validate_whatsapp_components(
    values: Sequence[Mapping[str, object]],
) -> tuple[dict[str, Any], ...]:
    allowed_types = {"header", "body", "button"}
    normalized: list[dict[str, Any]] = []
    if len(values) > 50:
        raise InboxCommandError("WhatsApp template components are invalid.")
    for value in values:
        if not isinstance(value, dict):
            raise InboxCommandError("WhatsApp template components are invalid.")
        component = dict(value)
        component_type = str(component.get("type") or "").strip().lower()
        if component_type not in allowed_types:
            raise InboxCommandError("WhatsApp template components are invalid.")
        parameters = component.get("parameters")
        if not isinstance(parameters, list) or any(
            not isinstance(item, dict) for item in parameters
        ):
            raise InboxCommandError("WhatsApp template components are invalid.")
        clean_parameters: list[dict[str, Any]] = []
        for parameter in parameters:
            parameter_type = str(parameter.get("type") or "").strip().lower()
            if parameter_type == "text":
                text = str(parameter.get("text") or "").strip()
                if not text:
                    raise InboxCommandError(
                        "Complete all required WhatsApp template values."
                    )
                clean_parameters.append({"type": "text", "text": text[:2000]})
                continue
            if parameter_type not in {"image", "video", "document"}:
                raise InboxCommandError("WhatsApp template components are invalid.")
            media = parameter.get(parameter_type)
            if not isinstance(media, dict):
                raise InboxCommandError("WhatsApp template components are invalid.")
            link = str(media.get("link") or "").strip()
            parsed = urlparse(link)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise InboxCommandError(
                    "WhatsApp template media must use a public HTTP(S) URL."
                )
            clean_parameters.append(
                {"type": parameter_type, parameter_type: {"link": link}}
            )
        clean_component: dict[str, Any] = {
            "type": component_type,
            "parameters": clean_parameters,
        }
        if component_type == "button":
            if str(component.get("sub_type") or "").strip().lower() != "url":
                raise InboxCommandError("WhatsApp template components are invalid.")
            index = str(component.get("index") or "").strip()
            if not index.isdigit():
                raise InboxCommandError("WhatsApp template components are invalid.")
            clean_component["sub_type"] = "url"
            clean_component["index"] = index
        normalized.append(clean_component)
    return tuple(normalized)


def parse_whatsapp_template_components(
    values: Sequence[Mapping[str, object]],
) -> tuple[WhatsAppTemplateComponent, ...]:
    """Normalize the dynamic form payload into the typed reply boundary."""

    normalized = _validate_whatsapp_components(values)
    components: list[WhatsAppTemplateComponent] = []
    for value in normalized:
        parameters: list[WhatsAppTemplateParameter] = []
        for raw_parameter in value["parameters"]:
            parameter_kind = WhatsAppTemplateParameterKind(raw_parameter["type"])
            if parameter_kind is WhatsAppTemplateParameterKind.text:
                parameter_value = str(raw_parameter["text"])
            else:
                parameter_value = str(raw_parameter[parameter_kind.value]["link"])
            parameters.append(
                WhatsAppTemplateParameter(
                    kind=parameter_kind,
                    value=parameter_value,
                )
            )
        components.append(
            WhatsAppTemplateComponent(
                kind=WhatsAppTemplateComponentKind(value["type"]),
                parameters=tuple(parameters),
                button_index=(
                    int(value["index"])
                    if value["type"] == WhatsAppTemplateComponentKind.button.value
                    else None
                ),
            )
        )
    return tuple(components)


def _provider_whatsapp_components(
    values: Sequence[WhatsAppTemplateComponent],
) -> tuple[dict[str, Any], ...]:
    components: list[dict[str, Any]] = []
    for component in values:
        parameters: list[dict[str, Any]] = []
        for parameter in component.parameters:
            if parameter.kind is WhatsAppTemplateParameterKind.text:
                parameters.append(
                    {"type": parameter.kind.value, "text": parameter.value}
                )
            else:
                parameters.append(
                    {
                        "type": parameter.kind.value,
                        parameter.kind.value: {"link": parameter.value},
                    }
                )
        payload: dict[str, Any] = {
            "type": component.kind.value,
            "parameters": parameters,
        }
        if component.kind is WhatsAppTemplateComponentKind.button:
            payload["sub_type"] = "url"
            payload["index"] = str(component.button_index)
        components.append(payload)
    return tuple(components)


def _reply_replay(
    db: Session,
    *,
    conversation: InboxConversation,
    idempotency_key: str,
    body_text: str,
    reply_to_message_id: UUID | None,
) -> ReplyOutcome | None:
    if not idempotency_key:
        return None
    previous = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .filter(
            InboxMessage.metadata_["idempotency_key"].as_string() == idempotency_key
        )
        .order_by(InboxMessage.created_at.desc())
        .first()
    )
    if previous is None:
        return None
    previous_body = str((previous.metadata_ or {}).get("body_text") or "").strip()
    previous_reply = (previous.metadata_ or {}).get("reply_to")
    previous_reply_id = (
        str(previous_reply.get("message_id") or "")
        if isinstance(previous_reply, dict)
        else ""
    )
    requested_reply_id = str(reply_to_message_id) if reply_to_message_id else ""
    if (
        previous_body
        and previous_body != body_text
        or previous_reply_id != requested_reply_id
    ):
        raise InboxCommandRejected(
            "This send key was already used for a different reply.",
            conversation_id=conversation.id,
        )
    return ReplyOutcome(
        conversation_id=str(conversation.id),
        kind=str((previous.metadata_ or {}).get("delivery_status") or "queued"),
        sender=previous.from_address or "team sender",
        message_id=str(previous.id),
        notification_id=previous.notification_id,
        replayed=True,
    )


def reply(
    db: Session,
    *,
    command: ReplyCommand,
) -> ReplyOutcome:
    def action() -> ReplyOutcome:
        conversation = _active_conversation(db, command.conversation_id)
        clean_body = command.body_text.strip()
        scheduled_for = command.send_after
        if scheduled_for is not None:
            if scheduled_for.tzinfo is None:
                scheduled_for = scheduled_for.replace(tzinfo=UTC)
            if scheduled_for <= datetime.now(UTC):
                raise InboxCommandError("Choose a send time in the future.")
        clean_idempotency_key = str(command.idempotency_key or "").strip()
        reply_to_uuid = command.reply_to_message_id
        if len(clean_idempotency_key) > 200:
            raise InboxCommandError("Reply idempotency key is too long.")
        replay = _reply_replay(
            db,
            conversation=conversation,
            idempotency_key=clean_idempotency_key,
            body_text=clean_body,
            reply_to_message_id=reply_to_uuid,
        )
        if replay is not None:
            return replay
        template = None
        clean_template_id = (
            str(command.template_id).strip()
            if command.template_id is not None
            else None
        )
        if clean_template_id:
            template = team_inbox_operations.get_template(db, clean_template_id)
            if not clean_body:
                clean_body = template.body_text.strip()
        clean_provider_template_name = ""
        clean_provider_template_language = ""
        clean_provider_components: tuple[dict[str, Any], ...] = ()
        if conversation.channel_type == InboxChannelType.whatsapp.value:
            clean_provider_template_name = str(
                command.whatsapp_template_name or ""
            ).strip()
            clean_provider_template_language = str(
                command.whatsapp_template_language or ""
            ).strip()
            if clean_provider_template_name or clean_provider_template_language:
                if not clean_provider_template_name:
                    raise InboxCommandError("Choose an approved WhatsApp template.")
                if not clean_provider_template_language:
                    raise InboxCommandError("WhatsApp template language is required.")
                try:
                    from app.services.integrations import whatsapp_capability

                    approved_templates = whatsapp_capability.list_approved_templates(db)
                except Exception:
                    raise InboxCommandError(
                        "WhatsApp templates are unavailable. Please try again."
                    ) from None
                if not any(
                    str(item.get("name") or "").strip() == clean_provider_template_name
                    and str(item.get("language") or "").strip()
                    == clean_provider_template_language
                    for item in approved_templates
                ):
                    raise InboxCommandError("Choose an approved WhatsApp template.")
                clean_provider_components = _provider_whatsapp_components(
                    command.whatsapp_template_components
                )
                if not clean_body:
                    clean_body = f"[WhatsApp template: {clean_provider_template_name}]"
        if not clean_body:
            raise InboxCommandError("Reply body is required.")

        # The provider/template preparation above may perform external I/O. Do
        # not hold the canonical conversation row while it runs. Reacquire the
        # authoritative row only for the bounded database write phase, and
        # fail fast so contention becomes an explicit retry instead of a
        # ten-second PostgreSQL lock timeout and HTTP 500.
        conversation = _active_conversation(
            db,
            command.conversation_id,
            for_update=True,
            nowait=True,
        )
        replay = _reply_replay(
            db,
            conversation=conversation,
            idempotency_key=clean_idempotency_key,
            body_text=clean_body,
            reply_to_message_id=reply_to_uuid,
        )
        if replay is not None:
            return replay

        body_html = (
            "<p>"
            + "<br>".join(escape(line) for line in clean_body.splitlines())
            + "</p>"
        )
        submitted_attachment_ids = [
            str(item).strip() for item in command.attachment_ids if str(item).strip()
        ]
        try:
            staged_attachment_ids = list(
                team_inbox_media.validate_staged_asset_ids(
                    db,
                    conversation_id=conversation.id,
                    asset_ids=submitted_attachment_ids,
                )
            )
        except team_inbox_media.MediaUploadError as exc:
            raise InboxCommandRejected(
                str(exc), conversation_id=conversation.id
            ) from exc
        reply_metadata: dict[str, object] = {
            "source_route": "admin_inbox_detail_reply",
            "template_id": str(template.id) if template is not None else None,
            "idempotency_key": clean_idempotency_key or None,
        }
        if staged_attachment_ids:
            reply_metadata["inbox_attachment_ids"] = staged_attachment_ids
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
        if clean_provider_template_name:
            reply_metadata["whatsapp_template"] = {
                "name": clean_provider_template_name,
                "language": clean_provider_template_language,
                "components": list(clean_provider_components),
                "variables": {},
                "inbox_template_id": str(template.id) if template is not None else None,
            }

        if scheduled_for is not None:
            scheduled = team_inbox_outbound.schedule_inbox_reply(
                db,
                conversation=conversation,
                payload=team_inbox_outbound.InboxReplyPayload(
                    body_html=body_html,
                    body_text=clean_body,
                    subject=template.subject if template is not None else None,
                    sent_by_person_id=command.actor_person_id,
                    metadata=reply_metadata,
                ),
                send_after=scheduled_for,
            )
            if staged_attachment_ids:
                team_inbox_media.bind_assets_to_message(
                    db, message=scheduled, asset_ids=staged_attachment_ids
                )
            team_inbox_operations.record_macro_use(db, command.macro_id)
            return ReplyOutcome(
                conversation_id=str(conversation.id),
                kind="scheduled",
                sender="scheduled",
                message_id=str(scheduled.id),
            )
        result = team_inbox_outbound.send_inbox_reply(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.InboxReplyPayload(
                body_html=body_html,
                body_text=clean_body,
                subject=template.subject if template is not None else None,
                sent_by_person_id=command.actor_person_id,
                metadata=reply_metadata,
            ),
            record_failure=True,
        )
        if result.kind not in {"sent", "queued"}:
            raise InboxCommandRejected(
                result.reason or "Reply could not be sent.",
                conversation_id=conversation.id,
            )
        if conversation.channel_type in {
            InboxChannelType.facebook_comment.value,
            InboxChannelType.instagram_comment.value,
        }:
            stage_audit_event(
                db,
                action="reply_comment",
                entity_type="inbox_conversation",
                entity_id=str(conversation.id),
                actor_type=AuditActorType.user,
                actor_id=(
                    str(command.actor_person_id)
                    if command.actor_person_id is not None
                    else None
                ),
                metadata={
                    "owner": OWNER,
                    "channel_type": conversation.channel_type,
                    "message_id": result.message_id,
                },
            )
        # Bind staged uploads to the message that actually carried them, inside
        # the same command — an attachment must never outlive a reply that
        # failed to send.
        if staged_attachment_ids and result.message_id:
            message = db.get(InboxMessage, coerce_uuid(result.message_id))
            if message is not None:
                team_inbox_media.bind_assets_to_message(
                    db, message=message, asset_ids=staged_attachment_ids
                )
        team_inbox_operations.record_macro_use(db, command.macro_id)
        return ReplyOutcome(
            conversation_id=str(conversation.id),
            kind=result.kind,
            sender=result.from_address or result.sender_key or "team sender",
            message_id=result.message_id,
            notification_id=result.notification_id,
        )

    try:
        return _commit(db, action)
    except OperationalError as exc:
        if _is_lock_unavailable(exc):
            raise ConversationBusyError from exc
        raise


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
        if conversation.snoozed_until is not None:
            # The wake is a durable per-conversation timer staged atomically
            # with the snooze (ADR 0007 §7). Re-snoozing replaces it; an
            # inbound reply or resolution makes a stale firing a
            # state-guarded no-op in the receipted consumer.
            from app.services.runtime_durable_timers import (
                ScheduleTimerCommand,
                schedule_timer,
            )

            schedule_timer(
                db,
                ScheduleTimerCommand(
                    owner="communications.team_inbox_commands",
                    entity_kind="inbox_conversation",
                    entity_id=conversation.id,
                    purpose="snooze_wake",
                    due_at=conversation.snoozed_until,
                    output_event_type="team_inbox.snooze_wake",
                ),
                context=CommandContext.system(
                    actor=str(actor_person_id or "communications.team_inbox_commands"),
                    scope=str(conversation.id),
                    reason="conversation snooze wake",
                    idempotency_key=(
                        f"snooze-wake:{conversation.id}:"
                        f"{conversation.snoozed_until.isoformat()}"
                    ),
                ),
            )

    _commit(db, action)


def save_filter(
    db: Session,
    *,
    name: str,
    filter_payload: team_inbox_filters.InboxSavedFilterPayload,
    actor_person_id: str | UUID | None = None,
    is_shared: bool = False,
) -> None:
    def action() -> None:
        normalized_payload = team_inbox_filters.normalize_saved_filter_payload(
            db, filter_payload
        )
        team_inbox_operations.save_filter(
            db,
            name=name,
            filter_payload=normalized_payload,
            owner_person_id=actor_person_id,
            is_shared=is_shared,
        )

    _commit(
        db,
        action,
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


def set_agent_presence(
    db: Session,
    *,
    actor_person_id: str | UUID | None,
    status: str,
) -> AgentPresenceOutcome:
    actor_uuid = coerce_uuid(actor_person_id)
    if actor_uuid is None:
        raise InboxCommandRejected("Authenticated operator identity is required.")
    clean_status = str(status or "").strip().lower()
    if clean_status not in team_inbox_assignment.VALID_AGENT_PRESENCE_STATUSES:
        raise InboxCommandError("Unsupported inbox availability status.")

    def action() -> AgentPresenceOutcome:
        existing = (
            db.query(InboxAgentPresence)
            .filter(InboxAgentPresence.person_id == actor_uuid)
            .one_or_none()
        )
        previous = (
            existing.manual_override_status or existing.status
            if existing is not None
            else None
        )
        presence = team_inbox_assignment.set_agent_presence(
            db,
            person_id=actor_uuid,
            status=clean_status,
            actor_person_id=actor_uuid,
            source_id=f"presence-command:{uuid4()}",
        )
        return AgentPresenceOutcome(
            person_id=str(actor_uuid),
            status=presence.manual_override_status or presence.status,
            already_set=previous == clean_status,
        )

    return _commit(db, action)


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


def assign_conversation_to_me(
    db: Session,
    *,
    conversation_id: str | UUID,
    service_team_id: str | UUID | None = None,
    actor_person_id: str | UUID | None = None,
) -> BulkActionOutcome:
    actor_uuid = coerce_uuid(actor_person_id)
    conversation_uuid = coerce_uuid(conversation_id)
    if actor_uuid is None:
        raise InboxCommandError("Authenticated staff user required.")
    if conversation_uuid is None:
        raise InboxCommandError("Conversation not found.")

    def execute() -> BulkActionOutcome:
        conversation = db.get(InboxConversation, conversation_uuid)
        if conversation is None or not conversation.is_active:
            raise InboxCommandError("Conversation not found.")
        team_uuid = (
            coerce_uuid(service_team_id)
            or conversation.primary_service_team_id
            or coerce_uuid(team_inbox_routing.default_service_team_id(db))
        )
        if team_uuid is None:
            raise InboxCommandError("Choose a service team before assigning.")
        result = team_inbox_operations.bulk_escalate(
            db,
            conversation_ids=[conversation_uuid],
            service_team_id=team_uuid,
            assigned_person_id=actor_uuid,
            auto_assign=False,
            actor_person_id=actor_uuid,
            reason="Assign to me",
            require_team_membership=False,
        )
        updated = result.get("updated")
        if isinstance(updated, list) and updated:
            return BulkActionOutcome(message="Assigned conversation to you.")
        skipped = result.get("skipped")
        reason = None
        if isinstance(skipped, list) and skipped:
            first = skipped[0]
            if isinstance(first, dict):
                reason = first.get("reason")
        raise InboxCommandError(
            f"Could not assign conversation: {reason or 'assignment rejected'}"
        )

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


def _lead_source_for_channel(channel_type: str) -> str:
    return {
        InboxChannelType.email.value: "Email",
        InboxChannelType.whatsapp.value: "Whatsapp",
        InboxChannelType.facebook_messenger.value: "Facebook",
        InboxChannelType.instagram_dm.value: "Instagram",
        InboxChannelType.chat_widget.value: "Website",
    }.get(channel_type, "Website")


def _party_contact_type_for_channel(channel_type: str) -> PartyContactPointType | None:
    values = {
        InboxChannelType.email.value: PartyContactPointType.email,
        InboxChannelType.whatsapp.value: PartyContactPointType.whatsapp,
        InboxChannelType.facebook_messenger.value: PartyContactPointType.facebook_messenger,
        InboxChannelType.instagram_dm.value: PartyContactPointType.instagram_dm,
    }
    return values.get(channel_type)


def _lead_capture_payload(
    conversation: InboxConversation,
    *,
    title: str | None,
    note: str | None,
) -> LeadCaptureRequest:
    metadata = dict(conversation.metadata_ or {})
    channel_type = conversation.channel_type
    contact_type = _party_contact_type_for_channel(channel_type)
    contact_address = str(conversation.contact_address or "").strip()
    contacts: list[LeadContactObservation] = []
    if contact_type is not None:
        provider = None
        provider_account_id = None
        external_subject_id = None
        if channel_type in {
            InboxChannelType.facebook_messenger.value,
            InboxChannelType.instagram_dm.value,
        }:
            provider = str(metadata.get("provider") or "team_inbox")
            provider_account_id = str(
                metadata.get("external_account_id")
                or metadata.get("page_id")
                or metadata.get("instagram_account_id")
                or channel_type
            )
            external_subject_id = contact_address
        contacts.append(
            LeadContactObservation(
                channel_type=contact_type,
                value=contact_address,
                display_value=contact_address,
                provider=provider,
                provider_account_id=provider_account_id,
                external_subject_id=external_subject_id,
                is_primary=True,
            )
        )
    clean_title = (
        str(title or "").strip()
        or conversation.subject
        or f"{channel_type.replace('_', ' ').title()} inbox enquiry"
    )
    return LeadCaptureRequest(
        party=LeadCapturePartyCreate(
            display_name=contact_address,
            contacts=contacts,
        ),
        title=clean_title[:200],
        lead_source=_lead_source_for_channel(channel_type),
        origin=LeadOriginCaptureCreate(
            capture_method=LeadCaptureMethod.agent_declared,
            source_platform=LeadSourcePlatform.agent,
            source_interaction_id=f"team-inbox:{conversation.id}",
            capture_source="admin_inbox",
            capture_reason="Operator captured an unmatched inbox contact as a lead",
        ),
        notes=str(note or "").strip() or None,
    )


def create_lead_from_conversation_uncommitted(
    db: Session,
    *,
    conversation: InboxConversation,
    actor_person_id: str | UUID | None = None,
    title: str | None = None,
    note: str | None = None,
) -> LeadCreationOutcome:
    if conversation.subscriber_id is not None:
        raise InboxCommandRejected(
            "This conversation is already linked to a customer.",
            conversation_id=conversation.id,
        )
    if not conversation.contact_address:
        raise InboxCommandRejected(
            "Conversation has no contact address to capture as a lead.",
            conversation_id=conversation.id,
        )
    metadata = dict(conversation.metadata_ or {})
    existing = metadata.get("lead_capture")
    if (
        isinstance(existing, dict)
        and existing.get("lead_id")
        and existing.get("party_id")
    ):
        return LeadCreationOutcome(
            conversation_id=str(conversation.id),
            lead_id=str(existing["lead_id"]),
            party_id=str(existing["party_id"]),
            replayed=True,
        )
    try:
        result = sales_capture.capture_lead(
            db,
            _lead_capture_payload(conversation, title=title, note=note),
            actor_id=str(actor_person_id or "system:team-inbox-admin"),
            commit=False,
        )
    except sales_capture.LeadCaptureError as exc:
        raise InboxCommandRejected(str(exc), conversation_id=conversation.id) from exc
    metadata["lead_capture"] = {
        "lead_id": str(result.lead.id),
        "party_id": str(result.party_id),
        "origin_capture_id": str(result.origin.id),
        "source": "admin_inbox",
        "actor_person_id": str(actor_person_id or "") or None,
        "replayed": result.replayed,
    }
    conversation.metadata_ = metadata
    db.flush()
    return LeadCreationOutcome(
        conversation_id=str(conversation.id),
        lead_id=str(result.lead.id),
        party_id=str(result.party_id),
        replayed=result.replayed,
    )


def create_lead_from_conversation(
    db: Session,
    *,
    conversation_id: str | UUID,
    actor_person_id: str | UUID | None = None,
    title: str | None = None,
    note: str | None = None,
) -> LeadCreationOutcome:
    def action() -> LeadCreationOutcome:
        return create_lead_from_conversation_uncommitted(
            db,
            conversation=_active_conversation(db, conversation_id, for_update=True),
            actor_person_id=actor_person_id,
            title=title,
            note=note,
        )

    return _commit(db, action)


def _lead_from_conversation_metadata(
    db: Session, conversation: InboxConversation
) -> Lead:
    metadata = dict(conversation.metadata_ or {})
    lead_capture = metadata.get("lead_capture")
    lead_id = (
        coerce_uuid(lead_capture.get("lead_id"))
        if isinstance(lead_capture, dict)
        else None
    )
    lead = db.get(Lead, lead_id) if lead_id is not None else None
    if lead is None or lead.party_id is None:
        raise InboxCommandRejected(
            "Create a lead from this conversation before merging it.",
            conversation_id=conversation.id,
        )
    return lead


def _lead_from_conversation_or_create(
    db: Session,
    *,
    conversation: InboxConversation,
    actor_person_id: str | UUID | None,
) -> Lead:
    try:
        return _lead_from_conversation_metadata(db, conversation)
    except InboxCommandRejected:
        created = create_lead_from_conversation_uncommitted(
            db,
            conversation=conversation,
            actor_person_id=actor_person_id,
            note="Created during Inbox contact merge",
        )
        lead = db.get(Lead, coerce_uuid(created.lead_id))
        if lead is None or lead.party_id is None:
            raise InboxCommandRejected("Created lead could not be reloaded.")
        return lead


def _unique_target(rows: list[T], *, target_name: str, query: str) -> T:
    if not rows:
        raise InboxCommandRejected(f"No {target_name} matched '{query}'.")
    if len(rows) > 1:
        raise InboxCommandRejected(
            f"More than one {target_name} matched '{query}'. Narrow the search."
        )
    return rows[0]


def _search_subscriber(db: Session, query_text: str) -> Subscriber:
    like = f"%{query_text}%"
    rows = (
        db.query(Subscriber)
        .filter(Subscriber.is_active.is_(True))
        .filter(
            or_(
                Subscriber.email.ilike(like),
                Subscriber.phone.ilike(like),
                Subscriber.first_name.ilike(like),
                Subscriber.last_name.ilike(like),
                Subscriber.display_name.ilike(like),
                Subscriber.company_name.ilike(like),
                Subscriber.account_number.ilike(like),
                Subscriber.subscriber_number.ilike(like),
            )
        )
        .order_by(Subscriber.updated_at.desc().nullslast())
        .limit(2)
        .all()
    )
    return _unique_target(rows, target_name="customer", query=query_text)


def _search_reseller(db: Session, query_text: str) -> Reseller:
    like = f"%{query_text}%"
    rows = (
        db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .filter(
            or_(
                Reseller.name.ilike(like),
                Reseller.code.ilike(like),
                Reseller.contact_email.ilike(like),
                Reseller.contact_phone.ilike(like),
            )
        )
        .order_by(Reseller.name.asc())
        .limit(2)
        .all()
    )
    return _unique_target(rows, target_name="reseller", query=query_text)


def _search_organization(db: Session, query_text: str) -> Organization:
    like = f"%{query_text}%"
    rows = (
        db.query(Organization)
        .filter(Organization.is_active.is_(True))
        .filter(
            or_(
                Organization.name.ilike(like),
                Organization.legal_name.ilike(like),
                Organization.domain.ilike(like),
                Organization.email.ilike(like),
                Organization.phone.ilike(like),
            )
        )
        .order_by(Organization.updated_at.desc().nullslast())
        .limit(2)
        .all()
    )
    return _unique_target(rows, target_name="business", query=query_text)


def _organization_party_for_reseller(db: Session, reseller: Reseller) -> Party:
    if reseller.party_id is not None:
        party = db.get(Party, reseller.party_id)
        if party is None:
            raise InboxCommandRejected("Reseller Party binding is missing.")
        return party
    party = party_service.create_party(
        db,
        party_type=PartyType.organization,
        display_name=reseller.name,
        metadata={"created_by": "team_inbox_lead_merge"},
    )
    party_service.bind_reseller_profile(
        db,
        reseller_id=reseller.id,
        party_id=party.id,
        source="team_inbox_lead_merge",
        reason="Reviewed Inbox Lead association to reseller profile",
    )
    return party


def _organization_party_for_business(db: Session, organization: Organization) -> Party:
    if organization.party_id is not None:
        party = db.get(Party, organization.party_id)
        if party is None:
            raise InboxCommandRejected("Business Party binding is missing.")
        return party
    party = party_service.create_party(
        db,
        party_type=PartyType.organization,
        display_name=organization.name,
        metadata={"created_by": "team_inbox_lead_merge"},
    )
    party_service.bind_organization_profile(
        db,
        organization_id=organization.id,
        party_id=party.id,
        source="team_inbox_lead_merge",
        reason="Reviewed Inbox Lead association to business profile",
    )
    return party


def _relate_lead_party_to_target(
    db: Session,
    *,
    lead: Lead,
    target_party_id: UUID,
    target_type: str,
) -> None:
    if lead.party_id == target_party_id:
        return
    existing = (
        db.query(PartyRelationship)
        .filter(PartyRelationship.subject_party_id == lead.party_id)
        .filter(PartyRelationship.object_party_id == target_party_id)
        .filter(
            PartyRelationship.relationship_type
            == PartyRelationshipType.contact_for.value
        )
        .filter(PartyRelationship.relationship_key == "default")
        .one_or_none()
    )
    if existing is not None:
        existing.status = "active"
        metadata = dict(existing.metadata_ or {})
        metadata["last_reviewed_by"] = "team_inbox_lead_merge"
        existing.metadata_ = metadata
        return
    db.add(
        PartyRelationship(
            subject_party_id=lead.party_id,
            object_party_id=target_party_id,
            relationship_type=PartyRelationshipType.contact_for.value,
            relationship_key="default",
            status="active",
            source="team_inbox_lead_merge",
            metadata_={
                "reason": f"Inbox lead contact merged to {target_type}",
                "lead_id": str(lead.id),
            },
        )
    )


def _record_lead_merge(
    conversation: InboxConversation,
    lead: Lead,
    *,
    target_type: str,
    target_id: UUID,
    actor_person_id: str | UUID | None,
) -> None:
    lead_metadata = dict(lead.metadata_ or {})
    lead_metadata["inbox_merge"] = {
        "conversation_id": str(conversation.id),
        "target_type": target_type,
        "target_id": str(target_id),
        "actor_person_id": str(actor_person_id or "") or None,
    }
    lead.metadata_ = lead_metadata
    conversation_metadata = dict(conversation.metadata_ or {})
    capture = dict(conversation_metadata.get("lead_capture") or {})
    capture.update({"lead_id": str(lead.id), "party_id": str(lead.party_id)})
    capture["merge"] = {
        "target_type": target_type,
        "target_id": str(target_id),
        "actor_person_id": str(actor_person_id or "") or None,
    }
    conversation_metadata["lead_capture"] = capture
    conversation.metadata_ = conversation_metadata


def _merge_conversation_lead_uncommitted(
    db: Session,
    *,
    conversation: InboxConversation,
    lead: Lead,
    target_type: str,
    subscriber: Subscriber | None = None,
    reseller: Reseller | None = None,
    organization: Organization | None = None,
    actor_person_id: str | UUID | None,
) -> LeadMergeOutcome:
    if lead.party_id is None:
        raise InboxCommandRejected(
            "Create a lead from this conversation before merging it.",
            conversation_id=conversation.id,
        )
    lead_party_id = lead.party_id
    if target_type == "subscriber":
        if subscriber is None:
            raise InboxCommandRejected("Choose a customer to merge this lead into.")
        account_conversion.stage_lead_account_conversion(
            db,
            lead_id=lead.id,
            party_id=lead_party_id,
            subscriber_id=subscriber.id,
            actor_id=str(actor_person_id or "system:team-inbox-admin"),
        )
        team_inbox_contact_links.link_conversation_contact(
            db,
            conversation=conversation,
            subscriber_id=subscriber.id,
            linked_by_person_id=actor_person_id,
            note="Lead merged to customer from Inbox",
        )
        _record_lead_merge(
            conversation,
            lead,
            target_type="subscriber",
            target_id=subscriber.id,
            actor_person_id=actor_person_id,
        )
        return LeadMergeOutcome(
            conversation_id=str(conversation.id),
            lead_id=str(lead.id),
            target_type="subscriber",
            target_id=str(subscriber.id),
            subscriber_id=str(subscriber.id),
        )
    if target_type == "reseller":
        if reseller is None:
            raise InboxCommandRejected("Choose a reseller to merge this lead into.")
        reseller_party = _organization_party_for_reseller(db, reseller)
        party_service.ensure_role(
            db,
            party_id=reseller_party.id,
            role_type=PartyRoleType.reseller,
            status=PartyRoleStatus.active,
            source="team_inbox_lead_merge",
        )
        _relate_lead_party_to_target(
            db, lead=lead, target_party_id=reseller_party.id, target_type="reseller"
        )
        team_inbox_contact_links.link_conversation_contact(
            db,
            conversation=conversation,
            reseller_id=reseller.id,
            linked_by_person_id=actor_person_id,
            note="Lead merged to reseller from Inbox",
        )
        _record_lead_merge(
            conversation,
            lead,
            target_type="reseller",
            target_id=reseller.id,
            actor_person_id=actor_person_id,
        )
        return LeadMergeOutcome(
            conversation_id=str(conversation.id),
            lead_id=str(lead.id),
            target_type="reseller",
            target_id=str(reseller.id),
            reseller_id=str(reseller.id),
        )
    if target_type == "organization":
        if organization is None:
            raise InboxCommandRejected("Choose a business to merge this lead into.")
        organization_party = _organization_party_for_business(db, organization)
        party_service.ensure_role(
            db,
            party_id=organization_party.id,
            role_type=PartyRoleType.customer,
            status=PartyRoleStatus.active,
            source="team_inbox_lead_merge",
        )
        _relate_lead_party_to_target(
            db,
            lead=lead,
            target_party_id=organization_party.id,
            target_type="business",
        )
        _record_lead_merge(
            conversation,
            lead,
            target_type="organization",
            target_id=organization.id,
            actor_person_id=actor_person_id,
        )
        return LeadMergeOutcome(
            conversation_id=str(conversation.id),
            lead_id=str(lead.id),
            target_type="organization",
            target_id=str(organization.id),
            organization_id=str(organization.id),
        )
    raise InboxCommandError("Choose customer, reseller, or business.")


def merge_conversation_lead(
    db: Session,
    *,
    conversation_id: str | UUID,
    target_type: str,
    subscriber_id: str | UUID | None = None,
    reseller_id: str | UUID | None = None,
    organization_id: str | UUID | None = None,
    actor_person_id: str | UUID | None = None,
) -> LeadMergeOutcome:
    def action() -> LeadMergeOutcome:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        lead = _lead_from_conversation_metadata(db, conversation)
        subscriber = (
            db.get(Subscriber, coerce_uuid(subscriber_id)) if subscriber_id else None
        )
        reseller = db.get(Reseller, coerce_uuid(reseller_id)) if reseller_id else None
        organization = (
            db.get(Organization, coerce_uuid(organization_id))
            if organization_id
            else None
        )
        return _merge_conversation_lead_uncommitted(
            db,
            conversation=conversation,
            lead=lead,
            target_type=target_type,
            subscriber=subscriber,
            reseller=reseller,
            organization=organization,
            actor_person_id=actor_person_id,
        )

    return _commit(db, action)


def merge_contact(
    db: Session,
    *,
    conversation_id: str | UUID,
    target_type: str,
    target_query: str | None = None,
    actor_person_id: str | UUID | None = None,
) -> ContactMergeOutcome:
    clean_target = str(target_type or "").strip().lower()
    clean_query = str(target_query or "").strip()

    def action() -> ContactMergeOutcome:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        if clean_target == "lead":
            created = create_lead_from_conversation_uncommitted(
                db,
                conversation=conversation,
                actor_person_id=actor_person_id,
            )
            return ContactMergeOutcome(
                conversation_id=str(conversation.id),
                target_type="lead",
                target_id=created.lead_id,
                lead_id=created.lead_id,
            )
        if clean_target not in {"subscriber", "reseller", "organization"}:
            raise InboxCommandError("Choose lead, customer, reseller, or business.")
        if not clean_query:
            raise InboxCommandRejected(
                "Search for the customer, reseller, or business."
            )
        lead = _lead_from_conversation_or_create(
            db, conversation=conversation, actor_person_id=actor_person_id
        )
        if clean_target == "subscriber":
            outcome = _merge_conversation_lead_uncommitted(
                db,
                conversation=conversation,
                lead=lead,
                target_type="subscriber",
                subscriber=_search_subscriber(db, clean_query),
                actor_person_id=actor_person_id,
            )
        elif clean_target == "reseller":
            outcome = _merge_conversation_lead_uncommitted(
                db,
                conversation=conversation,
                lead=lead,
                target_type="reseller",
                reseller=_search_reseller(db, clean_query),
                actor_person_id=actor_person_id,
            )
        else:
            outcome = _merge_conversation_lead_uncommitted(
                db,
                conversation=conversation,
                lead=lead,
                target_type="organization",
                organization=_search_organization(db, clean_query),
                actor_person_id=actor_person_id,
            )
        return ContactMergeOutcome(
            conversation_id=outcome.conversation_id,
            target_type=outcome.target_type,
            target_id=outcome.target_id,
            lead_id=outcome.lead_id,
        )

    return _commit(db, action)


def create_internal_note(
    db: Session,
    *,
    conversation_id: str | UUID,
    body: str,
    actor_person_id: str | UUID | None = None,
    mention_user_ids: Sequence[str | UUID] = (),
) -> None:
    def action() -> None:
        conversation = _active_conversation(db, conversation_id)
        mentions = _eligible_mention_users(
            db,
            conversation=conversation,
            mention_user_ids=mention_user_ids,
        )
        note = team_inbox_operations.create_internal_note(
            db,
            conversation=conversation,
            body=body,
            actor_person_id=actor_person_id,
            metadata={"mentions": [str(user.id) for user in mentions]}
            if mentions
            else None,
        )
        _notify_internal_note_mentions(
            db,
            conversation=conversation,
            note=note,
            users=mentions,
            actor_person_id=actor_person_id,
        )

    _commit(db, action)


def _eligible_mention_users(
    db: Session,
    *,
    conversation: InboxConversation,
    mention_user_ids: Sequence[str | UUID],
) -> tuple[SystemUser, ...]:
    clean_ids = tuple(
        value
        for value in (coerce_uuid(item) for item in mention_user_ids)
        if value is not None
    )
    if not clean_ids:
        return ()
    unique_ids = tuple(dict.fromkeys(clean_ids))
    active_team_ids = [
        link.service_team_id
        for link in conversation.team_links
        if link.is_active and link.service_team_id is not None
    ]
    if not active_team_ids and conversation.primary_service_team_id is not None:
        active_team_ids = [conversation.primary_service_team_id]
    if not active_team_ids:
        raise InboxCommandRejected(
            "No eligible colleagues can be mentioned on this conversation.",
            conversation_id=conversation.id,
        )
    rows = (
        db.query(SystemUser)
        .join(
            ServiceTeamMember,
            ServiceTeamMember.person_id == SystemUser.person_party_id,
        )
        .filter(SystemUser.id.in_(unique_ids))
        .filter(SystemUser.is_active.is_(True))
        .filter(ServiceTeamMember.is_active.is_(True))
        .filter(ServiceTeamMember.team_id.in_(active_team_ids))
        .distinct()
        .all()
    )
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(unique_ids):
        raise InboxCommandRejected(
            "One or more mentioned colleagues cannot access this conversation.",
            conversation_id=conversation.id,
        )
    return tuple(by_id[user_id] for user_id in unique_ids)


def _notify_internal_note_mentions(
    db: Session,
    *,
    conversation: InboxConversation,
    note: InboxMessage,
    users: Sequence[SystemUser],
    actor_person_id: str | UUID | None,
) -> None:
    actor_uuid = coerce_uuid(actor_person_id)
    for user in users:
        if user.id == actor_uuid:
            continue
        dedupe_key = f"inbox-note-mention:{note.id}:{user.id}"
        existing = (
            db.query(Notification)
            .filter(Notification.channel == NotificationChannel.email)
            .filter(Notification.dedupe_key == dedupe_key)
            .one_or_none()
        )
        if existing is not None:
            continue
        notification = Notifications.queue_internal_notification(
            db,
            NotificationCreate(
                channel=NotificationChannel.email,
                audience_type="system_user",
                audience_id=user.id,
                recipient=user.email,
                event_type="team_inbox.private_note_mention",
                category="support",
                subject="You were mentioned in a Team Inbox note",
                body="A colleague mentioned you in a private Team Inbox note.",
                metadata_={
                    "conversation_id": str(conversation.id),
                    "message_id": str(note.id),
                    "target_url": f"/admin/inbox?c={conversation.id}",
                    "actor_person_id": str(actor_uuid) if actor_uuid else None,
                    "internal_only": True,
                },
            ),
        )
        notification.dedupe_key = dedupe_key


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
        team_inbox_status.apply_status_transition(
            db,
            conversation=conversation,
            status=InboxConversationStatus(clean_status),
            actor_person_id=coerce_uuid(actor_person_id),
            reason=team_inbox_status.InboxStatusReason.operator_change,
            source_id=f"operator-status:{uuid4()}",
            compatibility_source="admin_inbox_status_action",
        )
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
    outbound_email_sender_key: str | None = None,
    update_outbound_email_sender: bool = False,
) -> None:
    def action() -> None:
        team_inbox_routing.update_email_route(
            db,
            route_id,
            is_primary=is_primary,
            priority=priority,
            is_active=is_active,
            outbound_email_sender_key=outbound_email_sender_key,
            update_outbound_email_sender=update_outbound_email_sender,
        )

    _commit(db, action)


def delete_email_route(db: Session, *, route_id: str | UUID) -> None:
    def action() -> None:
        team_inbox_routing.delete_email_route(db, route_id)

    _commit(db, action)


def create_channel_route(
    db: Session,
    *,
    channel_type: str,
    provider: str | None,
    account_scope: str | None,
    service_team_id: str | UUID,
    display_name: str | None = None,
    allow_ai_routing: bool = True,
    priority: int = 100,
) -> str:
    def action() -> str:
        route = team_inbox_routing.create_channel_route(
            db,
            channel_type=channel_type,
            provider=provider,
            account_scope=account_scope,
            service_team_id=service_team_id,
            display_name=display_name,
            allow_ai_routing=allow_ai_routing,
            priority=priority,
        )
        return str(route.id)

    return _commit(db, action)


def update_channel_route(
    db: Session,
    *,
    route_id: str | UUID,
    service_team_id: str | UUID | None = None,
    display_name: str | None = None,
    allow_ai_routing: bool | None = None,
    priority: int | None = None,
    is_active: bool | None = None,
) -> None:
    def action() -> None:
        team_inbox_routing.update_channel_route(
            db,
            route_id,
            service_team_id=service_team_id,
            display_name=display_name,
            allow_ai_routing=allow_ai_routing,
            priority=priority,
            is_active=is_active,
        )

    _commit(db, action)


def delete_channel_route(db: Session, *, route_id: str | UUID) -> None:
    def action() -> None:
        team_inbox_routing.delete_channel_route(db, route_id)

    _commit(db, action)


def create_ai_route(
    db: Session,
    *,
    channel_type: str,
    intent_key: str,
    service_team_id: str | UUID,
    display_name: str | None = None,
    confidence_threshold: float = 0.75,
    priority: int = 100,
) -> str:
    def action() -> str:
        route = team_inbox_routing.create_ai_route(
            db,
            channel_type=channel_type,
            intent_key=intent_key,
            service_team_id=service_team_id,
            display_name=display_name,
            confidence_threshold=confidence_threshold,
            priority=priority,
        )
        return str(route.id)

    return _commit(db, action)


def update_ai_route(
    db: Session,
    *,
    route_id: str | UUID,
    service_team_id: str | UUID | None = None,
    display_name: str | None = None,
    confidence_threshold: float | None = None,
    priority: int | None = None,
    is_active: bool | None = None,
) -> None:
    def action() -> None:
        team_inbox_routing.update_ai_route(
            db,
            route_id,
            service_team_id=service_team_id,
            display_name=display_name,
            confidence_threshold=confidence_threshold,
            priority=priority,
            is_active=is_active,
        )

    _commit(db, action)


def delete_ai_route(db: Session, *, route_id: str | UUID) -> None:
    def action() -> None:
        team_inbox_routing.delete_ai_route(db, route_id)

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

    try:
        return _commit(db, action)
    except OperationalError as exc:
        text = str(getattr(exc, "orig", exc)).lower()
        if "lock timeout" in text or "locknotavailable" in text:
            raise ConversationBusyError from exc
        raise


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
    legacy_contact_subscriber_id: str | UUID | None = None,
    actor_person_id: str | UUID | None = None,
    attachment_ids: Sequence[str] | None = None,
    contact_name: str | None = None,
    contact_party_id: str | UUID | None = None,
    contact_country_code: str | None = None,
    template_id: str | UUID | None = None,
    template_values: Sequence[str] | None = None,
    whatsapp_template_name: str | None = None,
    whatsapp_template_language: str | None = None,
    whatsapp_template_components: Sequence[dict[str, Any]] = (),
    cc_addresses: Sequence[str] = (),
    bcc_addresses: Sequence[str] = (),
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
        submitted_contact_address = str(contact_address or "").strip()
        if (
            clean_channel == InboxChannelType.whatsapp.value
            and not submitted_contact_address
        ):
            submitted_contact_address = (
                _whatsapp_party_address(db, contact_party_id) or ""
            )
        if not submitted_contact_address:
            raise InboxCommandError("Enter who this conversation is with.")
        clean_cc: tuple[str, ...] = ()
        clean_bcc: tuple[str, ...] = ()
        if clean_channel == InboxChannelType.email.value:
            primary_email = team_inbox_routing.normalize_email_address(contact_address)
            valid_primary, _message = validate_email_format(primary_email or "")
            if not primary_email or not valid_primary:
                raise InboxCommandError("Enter a valid recipient email address.")
            clean_cc = _normalize_email_recipients(cc_addresses, label="CC")
            clean_bcc = _normalize_email_recipients(bcc_addresses, label="BCC")
        elif cc_addresses or bcc_addresses:
            raise InboxCommandError("CC and BCC are available only for email.")
        resolved_contact_address = submitted_contact_address
        resolved_subscriber_id = subscriber_id
        if clean_channel == InboxChannelType.whatsapp.value:
            resolved_contact_address = _normalize_whatsapp_recipient(
                resolved_contact_address,
                contact_country_code,
            )
            if legacy_contact_subscriber_id is not None:
                from app.models.subscriber import Subscriber, SubscriberStatus
                from app.services.customer_identity_normalization import (
                    normalize_phone_identifier,
                )

                subscriber_uuid = coerce_uuid(legacy_contact_subscriber_id)
                contact_subscriber = (
                    db.query(Subscriber)
                    .filter(Subscriber.id == subscriber_uuid)
                    .filter(Subscriber.party_id.is_(None))
                    .filter(Subscriber.is_active.is_(True))
                    .filter(Subscriber.status == SubscriberStatus.active)
                    .one_or_none()
                    if subscriber_uuid is not None
                    else None
                )
                if (
                    contact_subscriber is None
                    or normalize_phone_identifier(contact_subscriber.phone)
                    != resolved_contact_address
                ):
                    raise InboxCommandError(
                        "Selected customer no longer matches this WhatsApp number."
                    )
                resolved_subscriber_id = contact_subscriber.id
            clean_provider_template_name = str(whatsapp_template_name or "").strip()
            clean_provider_template_language = str(
                whatsapp_template_language or ""
            ).strip()
            if not clean_provider_template_name:
                raise InboxCommandError("Choose an approved WhatsApp template.")
            if not clean_provider_template_language:
                raise InboxCommandError("WhatsApp template language is required.")
            try:
                from app.services.integrations import whatsapp_capability

                approved_templates = whatsapp_capability.list_approved_templates(db)
            except Exception:
                raise InboxCommandError(
                    "WhatsApp templates are unavailable. Please try again."
                ) from None
            if not any(
                str(item.get("name") or "").strip() == clean_provider_template_name
                and str(item.get("language") or "").strip()
                == clean_provider_template_language
                for item in approved_templates
            ):
                raise InboxCommandError("Choose an approved WhatsApp template.")
            clean_provider_components = _validate_whatsapp_components(
                whatsapp_template_components
            )
            party_uuid = coerce_uuid(contact_party_id)
            if party_uuid is not None:
                from app.models.subscriber import Subscriber

                contact_subscriber = (
                    db.query(Subscriber)
                    .filter(Subscriber.party_id == party_uuid)
                    .filter(Subscriber.is_active.is_(True))
                    .one_or_none()
                )
                if contact_subscriber is not None:
                    resolved_subscriber_id = contact_subscriber.id
        else:
            clean_provider_template_name = ""
            clean_provider_template_language = ""
            clean_provider_components = ()
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
            contact_address=resolved_contact_address,
            subscriber_id=resolved_subscriber_id,
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
            contact_address=(resolution.normalized_contact or resolved_contact_address),
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
        try:
            staged_attachment_ids = list(
                team_inbox_media.validate_staged_asset_ids(
                    db,
                    conversation_id=conversation.id,
                    asset_ids=staged_attachment_ids,
                )
            )
        except team_inbox_media.MediaUploadError as exc:
            raise InboxCommandRejected(
                str(exc), conversation_id=conversation.id
            ) from exc

        body_html = (
            "<p>"
            + "<br>".join(escape(line) for line in clean_body.splitlines())
            + "</p>"
        )
        reply_metadata: dict[str, object] = {
            "source": "operator_initiated",
            "template_id": str(template.id) if template is not None else None,
        }
        if staged_attachment_ids:
            reply_metadata["inbox_attachment_ids"] = staged_attachment_ids
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
        if clean_channel == InboxChannelType.whatsapp.value:
            reply_metadata["whatsapp_template"] = {
                "name": clean_provider_template_name,
                "language": clean_provider_template_language,
                "components": list(clean_provider_components),
                "variables": {},
                "inbox_template_id": str(template.id) if template is not None else None,
            }
        result = team_inbox_outbound.send_inbox_reply(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.InboxReplyPayload(
                body_html=body_html,
                body_text=clean_body,
                subject=conversation.subject,
                cc_addresses=clean_cc,
                bcc_addresses=clean_bcc,
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

    if actor_type is not AuditActorType.system and actor_person_id is None:
        raise InboxCommandError("An audit actor is required to export a transcript.")

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


def consume_snooze_wake(
    db: Session,
    *,
    conversation_id: UUID,
    event_id: UUID,
    context: CommandContext,
) -> str | None:
    """Receipt one fired snooze timer into a conversation wake.

    State-guarded: a conversation that already woke (inbound reply, manual
    open, resolve) or re-snoozed to a later instant is left untouched.
    """
    from app.services.events.owner_outputs import consume_owner_output

    def _effect() -> str:
        from datetime import UTC, datetime

        from app.models.team_inbox import InboxConversation

        conversation = db.get(InboxConversation, conversation_id)
        if conversation is None:
            return "skipped_missing"
        if conversation.status != "snoozed" or conversation.snoozed_until is None:
            return "skipped_state"
        wake_at = conversation.snoozed_until
        if wake_at.tzinfo is None:
            wake_at = wake_at.replace(tzinfo=UTC)
        if wake_at > datetime.now(UTC):
            # Re-snoozed to a later instant after this timer fired.
            return "skipped_resnoozed"
        team_inbox_operations.wake_conversation(
            db, conversation=conversation, source="durable_timer"
        )
        return "woken"

    def _operation() -> str | None:
        return consume_owner_output(
            db,
            consumer="communications.team_inbox_commands",
            event_id=event_id,
            event_type="team_inbox.snooze_wake",
            producer_owner="runtime.durable_timers",
            context=context,
            operation=_effect,
        )[0]

    return _commit(db, _operation, context=context)
