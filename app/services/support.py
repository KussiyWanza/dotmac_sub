from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.domain_settings import SettingDomain
from app.models.notification import NotificationChannel, NotificationStatus
from app.models.sales import Lead
from app.models.subscriber import Subscriber
from app.models.support import (
    AutomationTrigger,
    Ticket,
    TicketAccessToken,
    TicketAssignee,
    TicketChannel,
    TicketComment,
    TicketCommentAuthorType,
    TicketLink,
    TicketMerge,
    TicketSlaEvent,
    TicketStatus,
)
from app.schemas.notification import NotificationCreate
from app.schemas.support import (
    TicketBulkUpdateRequest,
    TicketCommentCreate,
    TicketCommentUpdate,
    TicketCreate,
    TicketMergeRequest,
    TicketSlaEventCreate,
    TicketSlaEventUpdate,
    TicketUpdate,
)
from app.services import domain_settings as domain_settings_service
from app.services import notification as notification_service
from app.services import numbering as numbering_service
from app.services import service_address as service_address_service
from app.services import support_ticket_filters, ticket_validation
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services.audit_helpers import log_audit_event
from app.services.common import apply_ordering, apply_pagination
from app.services.customer_identity_resolution import (
    AUTOMATION_SUPPRESSION_REASON_IDENTITY_REVIEW,
    identity_resolution_allows_sensitive_automation,
    identity_resolution_requires_manual_review,
    resolve_customer_identity,
)
from app.services.customer_support_links import ticket_customer_link_filter
from app.services.domain_errors import DomainError
from app.services.dynamic_filters import FilterValidationError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)
from app.services.sales import lifecycle as lead_lifecycle
from app.services.staff_notifications import queue_staff_email, queue_staff_push

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")
_LIFECYCLE_CONCERN = "ticket lifecycle mutations"


class SupportTicketError(DomainError):
    """Transport-neutral failure from the canonical Ticket owner."""


def _ticket_error(code: str, message: str, **details: object) -> SupportTicketError:
    return SupportTicketError(code=code, message=message, details=details)


def _command_idempotency_key(request: object | None) -> str | None:
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = headers.get("Idempotency-Key")
    return str(value).strip() or None if value is not None else None


def ticket_owner_command(name: str):
    """Enter the canonical Ticket transaction around one typed public method.

    Calls made by another Ticket operation (for example a bulk command invoking
    the single-ticket update participant) remain flush-only inside the existing
    root command.
    """

    definition = OwnerCommandDefinition(
        owner="support.ticket_lifecycle",
        concern=_LIFECYCLE_CONCERN,
        name=name,
    )

    def decorate(operation):
        @wraps(operation)
        def wrapped(db: Session, *args, **kwargs):
            # Participate in any owner command already in flight, not just this
            # owner's. A cross-domain coordinator (for example
            # communications.conversation_ticket_handoff issuing a ticket from a
            # conversation) provides the transaction; opening a second root
            # command inside it is rejected as nested. Ticket state is still
            # decided here — only the transaction boundary is the caller's.
            #
            # The participant path intentionally does not emit the realtime
            # workqueue invalidation: it fires post-commit, and here the commit
            # belongs to the coordinator, which emits it once its own
            # transaction lands. Workqueue holds no authority, so a late
            # invalidation costs a pane its instant refresh, never a fact.
            if owner_command_active(db):
                return operation(db, *args, **kwargs)

            from app.services.db_session_adapter import db_session_adapter

            db_session_adapter.release_read_transaction(db)
            actor = str(kwargs.get("actor_id") or "support-system")
            context = CommandContext.system(
                actor=actor,
                scope=f"support.ticket:{name}",
                reason=f"execute canonical Ticket {name} command",
                idempotency_key=_command_idempotency_key(kwargs.get("request")),
            )
            result = execute_owner_command(
                db,
                definition=definition,
                context=context,
                operation=lambda: operation(db, *args, **kwargs),
            )
            if name == "create" and isinstance(result, Ticket):
                _notify_workqueue(result, "added")
            elif name == "update" and isinstance(result, Ticket):
                previous = db.info.pop("_support_previous_assignee_id", None)
                _notify_workqueue(
                    result,
                    "updated",
                    previous_assignee_id=previous,
                )
            return result

        return wrapped

    return decorate


# Ticket.status is a free-form string column; these guard every write at the
# boundary (no schema migration). closed/canceled/merged are terminal — they
# cannot be reopened except by an explicit admin action (allow_reopen=True),
# which keeps CRM pull and automation from silently resurrecting a closed
# ticket. Garbage values are rejected outright.
_VALID_TICKET_STATUSES: frozenset[str] = frozenset(s.value for s in TicketStatus)
_TICKET_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {TicketStatus.closed.value, TicketStatus.canceled.value, TicketStatus.merged.value}
)


def transition_ticket_status(
    ticket: Ticket,
    new_status: TicketStatus | str,
    *,
    source: str,
    allow_reopen: bool = False,
) -> bool:
    """Guarded write to ``Ticket.status``. Returns True if the status changed.

    - Rejects values that are not a real ``TicketStatus`` (raises ``ValueError``).
    - Refuses to move OUT of a terminal status (closed/canceled/merged) unless
      ``allow_reopen`` — this is the CRM-vs-local precedence point: a CRM pull or
      automation rule cannot reopen a locally-closed ticket.
    - Audits every change (and every blocked reopen) via the log.
    """
    raw = (
        new_status.value
        if isinstance(new_status, TicketStatus)
        else str(new_status).strip()
    )
    if raw not in _VALID_TICKET_STATUSES:
        raise ValueError(f"Invalid ticket status: {new_status!r}")
    current = ticket.status
    if current == raw:
        return False
    if current in _TICKET_TERMINAL_STATUSES and not allow_reopen:
        logger.info(
            "ticket_status_transition_blocked",
            extra={
                "event": "ticket_status_transition_blocked",
                "ticket_id": str(getattr(ticket, "id", None)),
                "from": current,
                "to": raw,
                "source": source,
            },
        )
        return False
    logger.info(
        "ticket_status_transition",
        extra={
            "event": "ticket_status_transition",
            "ticket_id": str(getattr(ticket, "id", None)),
            "from": current,
            "to": raw,
            "source": source,
        },
    )
    ticket.status = raw
    return True


MENTION_EMAIL_RE = re.compile(r"@([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")

# Ticket states worth interrupting a customer for, and what to say. Everything
# else (triage churn, internal reassignment) stays silent — a notification the
# customer cannot act on trains them to ignore the next one.
CUSTOMER_NOTIFIED_TICKET_STATUSES: dict[str, dict[str, str]] = {
    TicketStatus.open.value: {
        "label": "we're on it",
        "body": "is now being worked on. We'll update you as it progresses.",
    },
    TicketStatus.waiting_on_customer.value: {
        "label": "we need your input",
        "body": ("is waiting on you. Please reply on the ticket so we can continue."),
    },
    TicketStatus.on_hold.value: {
        "label": "on hold",
        "body": "has been placed on hold. We'll let you know when it resumes.",
    },
    TicketStatus.closed.value: {
        "label": "closed",
        "body": "has been closed. Reply if the problem is still there.",
    },
}

SUPPORT_NOTIFICATION_TOGGLE_KEY = "support_ticket_notifications_enabled"
SUPPORT_AUTO_ASSIGN_ENABLED_KEY = "support_ticket_auto_assign_enabled"
SUPPORT_REGION_ASSIGNMENT_RULES_KEY = "support_region_assignment_rules"
SUPPORT_SERVICE_TEAM_MEMBERS_KEY = "support_service_team_members"


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_subscriber_uuid(db: Session, value: str | UUID | None) -> UUID | None:
    """Return the UUID only when it points at a real subscriber row."""
    from app.models.subscriber import Subscriber

    uid = _coerce_uuid(str(value)) if value is not None else None
    if not uid:
        return None
    return uid if db.get(Subscriber, uid) else None


def _coerce_system_user_uuid(db: Session, value: str | UUID | None) -> UUID | None:
    """Return the UUID only when it points at a real system user row."""
    from app.models.system_user import SystemUser

    uid = _coerce_uuid(str(value)) if value is not None else None
    if not uid:
        return None
    return uid if db.get(SystemUser, uid) else None


def _normalize_comment_author_type(value: object) -> str:
    if hasattr(value, "value"):
        value = value.value
    text = str(value or "").strip()
    allowed = {item.value for item in TicketCommentAuthorType}
    return text if text in allowed else TicketCommentAuthorType.system.value


def _ensure_not_merged_source(ticket: Ticket) -> None:
    if (
        ticket.merged_into_ticket_id is not None
        or support_ticket_settings_service.status_is_merged(ticket.status)
    ):
        raise _ticket_error(
            "ticket_merged_source", "Cannot modify a merged source ticket"
        )


def _merge_attachment_dicts(
    base: list[dict] | None, extra: list[dict] | None
) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in (base or []) + (extra or []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("storage_key") or item.get("url") or "")
        name = str(item.get("file_name") or "")
        dedupe_key = (key, name)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        merged.append(item)
    return merged


def _read_bool_setting(
    db: Session, domain: SettingDomain, key: str, default: bool
) -> bool:
    try:
        setting = (
            domain_settings_service.settings.get_by_key(db, key)
            if domain is None
            else None
        )
    except Exception:
        setting = None

    if setting is None:
        try:
            domain_client = getattr(domain_settings_service, f"{domain.value}_settings")
            setting = domain_client.get_by_key(db, key)
        except Exception:
            setting = None

    if not setting:
        return default

    raw = setting.value_json if setting.value_json is not None else setting.value_text
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _read_json_setting(db: Session, domain: SettingDomain, key: str) -> dict[str, Any]:
    try:
        domain_client = getattr(domain_settings_service, f"{domain.value}_settings")
        setting = domain_client.get_by_key(db, key)
    except Exception:
        return {}
    if isinstance(setting.value_json, dict):
        return dict(setting.value_json)
    return {}


def _parse_mentions(body: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in MENTION_EMAIL_RE.findall(body):
        email = match.strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def _clean_optional_text(value: object | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _resolve_inbound_sender(
    *,
    channel: object | None,
    metadata: dict[str, Any] | None,
    inbound_sender: object | None,
    inbound_sender_type: object | None,
) -> tuple[str | None, str | None]:
    sender = _clean_optional_text(inbound_sender)
    sender_type = _clean_optional_text(inbound_sender_type)
    metadata_payload = dict(metadata or {})

    if not sender:
        for key in (
            "inbound_sender",
            "sender",
            "from",
            "from_email",
            "from_phone",
            "from_whatsapp",
        ):
            sender = _clean_optional_text(metadata_payload.get(key))
            if sender:
                break

    if not sender_type:
        for key in ("inbound_sender_type", "sender_type"):
            sender_type = _clean_optional_text(metadata_payload.get(key))
            if sender_type:
                break

    if not sender_type:
        normalized_channel = (
            channel.value if hasattr(channel, "value") else str(channel or "")
        ).strip()
        if normalized_channel == TicketChannel.email.value:
            sender_type = "email"
        elif normalized_channel == TicketChannel.phone.value:
            sender_type = "phone"
        elif normalized_channel == TicketChannel.chat.value:
            sender_type = "whatsapp"

    return sender, sender_type


def _apply_inbound_identity_resolution(db: Session, data: dict[str, Any]) -> None:
    metadata = dict(data.get("metadata_") or {})
    sender, sender_type = _resolve_inbound_sender(
        channel=data.get("channel"),
        metadata=metadata,
        inbound_sender=data.get("inbound_sender"),
        inbound_sender_type=data.get("inbound_sender_type"),
    )
    if not sender:
        if data.get("subscriber_id") and not data.get("customer_account_id"):
            data["customer_account_id"] = data["subscriber_id"]
        data["metadata_"] = metadata or None
        return

    resolution = resolve_customer_identity(db, sender, channel_hint=sender_type)
    metadata["identity_resolution"] = resolution.as_metadata()
    metadata["inbound_sender"] = sender
    metadata["normalized_inbound_sender"] = resolution.normalized_identifier
    if sender_type:
        metadata["inbound_sender_type"] = sender_type

    if identity_resolution_requires_manual_review(resolution):
        metadata["manual_review_required"] = True
        metadata["automation_paused"] = True
        metadata["ai_auto_actions_paused"] = True
        metadata["account_sensitive_automation_allowed"] = False
        metadata["automation_suppressed_reason"] = (
            AUTOMATION_SUPPRESSION_REASON_IDENTITY_REVIEW
        )
    elif not identity_resolution_allows_sensitive_automation(resolution, db):
        metadata["account_sensitive_automation_allowed"] = False
    else:
        metadata["account_sensitive_automation_allowed"] = True

    if resolution.ambiguous:
        metadata["manual_review_required"] = True
    elif resolution.matched:
        resolved_subscriber_id = resolution.subscriber_id
        existing_subscriber_id = data.get("subscriber_id")
        existing_account_id = data.get("customer_account_id")
        if existing_subscriber_id and resolved_subscriber_id:
            if str(existing_subscriber_id) != str(resolved_subscriber_id):
                logger.warning(
                    "support_ticket_identity_conflict inbound_sender=%r explicit_subscriber_id=%s resolved_subscriber_id=%s",
                    sender,
                    existing_subscriber_id,
                    resolved_subscriber_id,
                )
                metadata["identity_resolution_conflict"] = {
                    "explicit_subscriber_id": str(existing_subscriber_id),
                    "resolved_subscriber_id": str(resolved_subscriber_id),
                }
            else:
                data["subscriber_id"] = resolved_subscriber_id
        elif resolved_subscriber_id:
            data["subscriber_id"] = resolved_subscriber_id

        if existing_account_id and resolved_subscriber_id:
            if str(existing_account_id) != str(resolved_subscriber_id):
                logger.warning(
                    "support_ticket_account_identity_conflict inbound_sender=%r explicit_customer_account_id=%s resolved_customer_account_id=%s",
                    sender,
                    existing_account_id,
                    resolved_subscriber_id,
                )
                metadata["identity_account_conflict"] = {
                    "explicit_customer_account_id": str(existing_account_id),
                    "resolved_customer_account_id": str(resolved_subscriber_id),
                }
        elif resolved_subscriber_id:
            data["customer_account_id"] = resolved_subscriber_id

    if data.get("subscriber_id") and not data.get("customer_account_id"):
        data["customer_account_id"] = data["subscriber_id"]
    data["metadata_"] = metadata or None


def _validate_ticket_lead_alignment(
    db: Session,
    data: dict[str, Any],
    *,
    existing: Ticket | None = None,
) -> None:
    """Keep a Ticket's optional account links aligned with its Lead Party."""

    lead_id = (
        data["lead_id"] if "lead_id" in data else getattr(existing, "lead_id", None)
    )
    if lead_id is None:
        return
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise _ticket_error("lead_not_found", "Lead not found")

    subscriber_ids: set[UUID] = set()
    for field in ("subscriber_id", "customer_account_id", "customer_person_id"):
        value = data[field] if field in data else getattr(existing, field, None)
        if value is not None:
            subscriber_ids.add(value)
    for subscriber_id in subscriber_ids:
        subscriber = db.get(Subscriber, subscriber_id)
        if subscriber is None:
            raise _ticket_error(
                "subscriber_not_found",
                f"Ticket {subscriber_id} Subscriber link was not found",
            )
        try:
            lead_lifecycle.validate_lead_subscriber_alignment(
                db, lead=lead, subscriber=subscriber
            )
        except lead_lifecycle.LeadLifecycleError as exc:
            raise _ticket_error("lead_subscriber_mismatch", str(exc)) from exc


class TicketComments:
    @staticmethod
    def get(db: Session, comment_id: str) -> TicketComment:
        comment = db.get(TicketComment, comment_id)
        if not comment:
            raise _ticket_error("comment_not_found", "Ticket comment not found")
        return comment

    @staticmethod
    def list(
        db: Session,
        ticket_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TicketComment]:
        query = (
            db.query(TicketComment)
            .filter(TicketComment.ticket_id == ticket_id)
            .order_by(TicketComment.created_at.asc())
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def create(
        db: Session,
        *,
        ticket: Ticket,
        payload: TicketCommentCreate,
        actor_id: str | None,
        request=None,
    ) -> TicketComment:
        _ensure_not_merged_source(ticket)
        author_type = _normalize_comment_author_type(payload.author_type)
        author_person_id = _coerce_subscriber_uuid(db, payload.author_person_id)
        author_system_user_id = _coerce_system_user_uuid(
            db, payload.author_system_user_id
        )
        if author_type == TicketCommentAuthorType.customer.value:
            author_system_user_id = None
        elif author_type == TicketCommentAuthorType.staff.value:
            author_person_id = None
        else:
            author_person_id = None
            author_system_user_id = None

        comment = TicketComment(
            ticket_id=ticket.id,
            author_person_id=author_person_id,
            author_type=author_type,
            author_system_user_id=author_system_user_id,
            body=payload.body.strip(),
            is_internal=payload.is_internal,
            attachments=[item.model_dump() for item in payload.attachments],
        )
        db.add(comment)
        db.flush()

        log_audit_event(
            db=db,
            request=request,
            action="comment_add",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={
                "comment_id": str(comment.id),
                "is_internal": payload.is_internal,
            },
        )
        return comment

    @staticmethod
    def stage_system_projection(
        db: Session,
        *,
        ticket: Ticket,
        body: str,
        source: str,
        metadata: dict,
        actor_id: str | None = None,
    ) -> TicketComment:
        """Stage an authoritative external fact on the official ticket timeline.

        Unlike an operator-authored comment, a completion fact remains valid if
        the ticket was closed or merged while field work was underway. The caller
        owns the surrounding transaction, so the field event and projection
        commit or roll back together.
        """

        comment = TicketComment(
            ticket_id=ticket.id,
            author_type=TicketCommentAuthorType.system.value,
            body=body.strip(),
            is_internal=True,
            attachments=[],
            metadata_={"source": source, **metadata},
        )
        db.add(comment)
        db.flush()
        from app.models.audit import AuditActorType
        from app.services.audit_adapter import stage_audit_event

        stage_audit_event(
            db,
            action="system_projection_add",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_type=AuditActorType.user if actor_id else AuditActorType.system,
            actor_id=actor_id,
            metadata={
                "comment_id": str(comment.id),
                "source": source,
                **metadata,
            },
        )
        return comment

    @staticmethod
    @ticket_owner_command("update_comment")
    def update(
        db: Session,
        *,
        comment: TicketComment,
        payload: TicketCommentUpdate,
        actor_id: str | None,
        request=None,
    ) -> TicketComment:
        ticket = db.get(Ticket, comment.ticket_id)
        if not ticket:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        _ensure_not_merged_source(ticket)

        data = payload.model_dump(exclude_unset=True)
        if "body" in data:
            comment.body = str(data["body"]).strip()
            comment.metadata_ = {
                **(comment.metadata_ or {}),
                "edited_at": datetime.now(UTC).isoformat(),
            }
        if "is_internal" in data:
            comment.is_internal = bool(data["is_internal"])
        if "attachments" in data and data["attachments"] is not None:
            comment.attachments = [item.model_dump() for item in data["attachments"]]

        log_audit_event(
            db=db,
            request=request,
            action="comment_edit",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"comment_id": str(comment.id)},
        )
        db.flush()
        db.refresh(comment)
        return comment

    @staticmethod
    @ticket_owner_command("delete_comment")
    def delete(
        db: Session, *, comment: TicketComment, actor_id: str | None, request=None
    ) -> None:
        ticket = db.get(Ticket, comment.ticket_id)
        if not ticket:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        _ensure_not_merged_source(ticket)
        db.delete(comment)
        log_audit_event(
            db=db,
            request=request,
            action="comment_delete",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"comment_id": str(comment.id)},
        )
        db.flush()


class TicketSlaEvents:
    @staticmethod
    def get(db: Session, event_id: str) -> TicketSlaEvent:
        event = db.get(TicketSlaEvent, event_id)
        if not event:
            raise _ticket_error("sla_event_not_found", "Ticket SLA event not found")
        return event

    @staticmethod
    def list(
        db: Session, ticket_id: str, limit: int = 100, offset: int = 0
    ) -> list[TicketSlaEvent]:
        query = (
            db.query(TicketSlaEvent)
            .filter(TicketSlaEvent.ticket_id == ticket_id)
            .order_by(TicketSlaEvent.created_at.desc())
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    @ticket_owner_command("create_sla_event")
    def create(db: Session, payload: TicketSlaEventCreate) -> TicketSlaEvent:
        ticket = db.get(Ticket, payload.ticket_id)
        if not ticket:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        _ensure_not_merged_source(ticket)
        event = TicketSlaEvent(
            ticket_id=payload.ticket_id,
            event_type=payload.event_type,
            expected_at=payload.expected_at,
            actual_at=payload.actual_at,
            metadata_=payload.metadata_,
        )
        db.add(event)
        db.flush()
        db.refresh(event)
        return event

    @staticmethod
    @ticket_owner_command("update_sla_event")
    def update(
        db: Session, event: TicketSlaEvent, payload: TicketSlaEventUpdate
    ) -> TicketSlaEvent:
        ticket = db.get(Ticket, event.ticket_id)
        if not ticket:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        _ensure_not_merged_source(ticket)
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(event, key, value)
        db.flush()
        db.refresh(event)
        return event

    @staticmethod
    @ticket_owner_command("delete_sla_event")
    def delete(db: Session, event: TicketSlaEvent) -> None:
        ticket = db.get(Ticket, event.ticket_id)
        if not ticket:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        _ensure_not_merged_source(ticket)
        db.delete(event)
        db.flush()


def _notify_workqueue(
    ticket: Ticket,
    change: str,
    *,
    previous_assignee_id: UUID | None = None,
) -> None:
    """Push a ticket write onto the live workqueue channels (best-effort)."""
    try:
        from app.services.workqueue.events import emit_item_change
        from app.services.workqueue.types import ItemKind

        emit_item_change(
            item_kind=ItemKind.ticket,
            item_id=ticket.id,
            change=change,  # type: ignore[arg-type]
            assignee_id=ticket.assigned_to_person_id,
            previous_assignee_id=previous_assignee_id,
            service_team_id=ticket.service_team_id,
        )
    except Exception:  # pragma: no cover — realtime must never fail a write
        logger.debug("workqueue_notify_failed ticket_id=%s", ticket.id, exc_info=True)


class Tickets:
    @staticmethod
    def list_response(
        db: Session,
        search: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        assigned_to_person_id: str | None = None,
        project_manager_person_id: str | None = None,
        site_coordinator_person_id: str | None = None,
        subscriber_id: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
        priority: str | None = None,
        channel: str | None = None,
        created_by_person_id: str | None = None,
        is_active: bool | None = None,
        filters: str | None = None,
    ) -> dict[str, Any]:
        items = Tickets.list(
            db,
            search=search,
            status=status,
            ticket_type=ticket_type,
            assigned_to_person_id=assigned_to_person_id,
            project_manager_person_id=project_manager_person_id,
            site_coordinator_person_id=site_coordinator_person_id,
            subscriber_id=subscriber_id,
            order_by=order_by,
            order_dir=order_dir,
            limit=limit,
            offset=offset,
            priority=priority,
            channel=channel,
            created_by_person_id=created_by_person_id,
            is_active=is_active,
            filters=filters,
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @staticmethod
    def _resolve_ticket_number(db: Session) -> str | None:
        # Imported tickets can legitimately be ahead of the local document
        # sequence.  Keep advancing the locked canonical sequence until its
        # candidate is actually free instead of failing the customer request on
        # support_tickets_number_key.  All local creators delegate here, so the
        # sequence lock serializes concurrent reservations.
        for _attempt in range(10_000):
            candidate = numbering_service.generate_number(
                db=db,
                domain=SettingDomain.workflow,
                sequence_key="support_ticket",
                enabled_key="support_ticket_numbering_enabled",
                prefix_key="support_ticket_number_prefix",
                padding_key="support_ticket_number_padding",
                start_key="support_ticket_number_start",
            )
            if candidate is None:
                return None
            occupied = db.scalar(
                select(Ticket.id).where(Ticket.number == candidate).limit(1)
            )
            if occupied is None:
                return candidate
        raise RuntimeError("support ticket number sequence could not find a free value")

    @staticmethod
    def _assert_ticket_exists(db: Session, ticket_id: UUID) -> Ticket:
        ticket = db.get(Ticket, ticket_id)
        if not ticket or not ticket.is_active:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        return ticket

    @staticmethod
    def _apply_status_timestamp_rules(
        ticket: Ticket, explicit_data: dict[str, Any]
    ) -> None:
        if ticket.status == "resolved":
            if explicit_data.get("resolved_at") is None and ticket.resolved_at is None:
                ticket.resolved_at = _now()
        if ticket.status == "closed":
            if explicit_data.get("closed_at") is None and ticket.closed_at is None:
                ticket.closed_at = _now()

    @staticmethod
    def _apply_ncc_categorisation(
        ticket: Ticket, explicit_data: dict[str, Any]
    ) -> None:
        """Store the NCC complaints-return classification on write.

        The ticket owner applies it so the filing reads a captured decision
        rather than re-deriving from free text at report time. An
        agent-supplied category is marked as such and never re-derived.
        """
        from app.services import ncc_categorisation

        ncc_categorisation.apply_to_ticket(ticket, explicit_data=explicit_data)

    @staticmethod
    def _replace_assignees(db: Session, ticket: Ticket, person_ids: list[UUID]) -> None:
        deduped: list[UUID] = []
        seen: set[str] = set()
        for person_id in person_ids:
            key = str(person_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(person_id)

        db.query(TicketAssignee).filter(TicketAssignee.ticket_id == ticket.id).delete()
        for person_id in deduped:
            db.add(TicketAssignee(ticket_id=ticket.id, person_id=person_id))

        # Backward compatibility legacy single assignee.
        if deduped and not ticket.assigned_to_person_id:
            ticket.assigned_to_person_id = deduped[0]

    @staticmethod
    def _auto_assignment_enabled(db: Session) -> bool:
        return support_ticket_settings_service.auto_assign_enabled(db)

    @staticmethod
    def _notifications_enabled(db: Session) -> bool:
        return _read_bool_setting(
            db, SettingDomain.notification, SUPPORT_NOTIFICATION_TOGGLE_KEY, False
        )

    @staticmethod
    def _apply_region_auto_assignment(ticket: Ticket, db: Session) -> dict[str, Any]:
        rules = support_ticket_settings_service.region_assignment_rules(db)
        if not ticket.region:
            return {"matched": False, "reason": "region_missing"}
        region_key = support_ticket_settings_service.normalize_system_value(
            ticket.region
        )
        region_rule = rules.get(region_key) if isinstance(rules, dict) else None
        if not isinstance(region_rule, dict):
            return {"matched": False, "reason": "no_rule"}

        changed: dict[str, Any] = {}
        if not ticket.ticket_manager_person_id and region_rule.get(
            "ticket_manager_person_id"
        ):
            ticket.ticket_manager_person_id = _coerce_uuid(
                region_rule.get("ticket_manager_person_id")
            )
            changed["ticket_manager_person_id"] = str(ticket.ticket_manager_person_id)
        if not ticket.site_coordinator_person_id and region_rule.get(
            "site_coordinator_person_id"
        ):
            ticket.site_coordinator_person_id = _coerce_uuid(
                region_rule.get("site_coordinator_person_id")
            )
            changed["site_coordinator_person_id"] = str(
                ticket.site_coordinator_person_id
            )
        if not ticket.technician_person_id and region_rule.get("technician_person_id"):
            ticket.technician_person_id = _coerce_uuid(
                region_rule.get("technician_person_id")
            )
            changed["technician_person_id"] = str(ticket.technician_person_id)
        if not ticket.service_team_id and region_rule.get("service_team_id"):
            ticket.service_team_id = _coerce_uuid(region_rule.get("service_team_id"))
            changed["service_team_id"] = str(ticket.service_team_id)

        assignee_ids = (
            region_rule.get("assignee_person_ids")
            if isinstance(region_rule.get("assignee_person_ids"), list)
            else []
        )
        if ticket.service_team_id:
            members = support_ticket_settings_service.service_team_members(db).get(
                str(ticket.service_team_id), []
            )
            if members:
                current = set(assignee_ids)
                assignee_ids = [
                    *assignee_ids,
                    *[uid for uid in members if uid not in current],
                ]
        if assignee_ids:
            resolved = [
                uid
                for uid in (_coerce_uuid(v) for v in assignee_ids)
                if uid is not None
            ]
            if resolved:
                existing = {
                    str(row.person_id)
                    for row in db.query(TicketAssignee)
                    .filter(TicketAssignee.ticket_id == ticket.id)
                    .all()
                }
                for person_id in resolved:
                    if str(person_id) in existing:
                        continue
                    db.add(TicketAssignee(ticket_id=ticket.id, person_id=person_id))
                changed["assignee_person_ids"] = [str(uid) for uid in resolved]

        return {"matched": True, "changes": changed}

    @staticmethod
    def _apply_sla_policy(
        db: Session,
        ticket: Ticket,
        *,
        explicit_due_at: bool = False,
    ) -> None:
        if explicit_due_at or ticket.due_at is not None:
            return
        if support_ticket_settings_service.status_is_terminal(ticket.status):
            return
        policy = support_ticket_settings_service.sla_policy(db).get(
            str(ticket.priority or "").strip(), {}
        )
        resolution_hours = int(policy.get("resolution_hours") or 0)
        if resolution_hours <= 0:
            return
        expected_at = _now() + timedelta(hours=resolution_hours)
        ticket.due_at = expected_at
        db.add(
            TicketSlaEvent(
                ticket_id=ticket.id,
                event_type="resolution_due",
                expected_at=expected_at,
                metadata_={
                    "source": "priority_sla_policy",
                    "priority": ticket.priority,
                    "resolution_hours": resolution_hours,
                },
            )
        )

    @staticmethod
    def _apply_rule_auto_assignment(
        db: Session, ticket: Ticket
    ) -> dict[str, Any] | None:
        from app.services.ticket_assignment import engine as assignment_engine

        result = assignment_engine.auto_assign_ticket(
            db,
            str(ticket.id),
            max_open_tickets=support_ticket_settings_service.auto_assign_max_open_tickets(
                db
            ),
        )
        assignment_engine.stage_ticket_evaluation_evidence(
            db, ticket_id=ticket.id, result=result
        )
        if result.reason == "no_matching_rule":
            return None
        assignee_id = _coerce_uuid(result.assignee_person_id)
        team_id = _coerce_uuid(result.fallback_service_team_id)
        if result.assignment_target in {"team", "rule_scope"}:
            ticket.service_team_id = team_id
            ticket.assigned_to_person_id = None
            ticket.ticket_manager_person_id = None
            ticket.site_coordinator_person_id = None
            db.query(TicketAssignee).filter(
                TicketAssignee.ticket_id == ticket.id
            ).delete()
        elif result.reason == "queue_fallback_team_assigned" and team_id:
            ticket.service_team_id = team_id
        elif result.assignment_target == "technical_supervisor" and assignee_id:
            if not ticket.ticket_manager_person_id:
                ticket.ticket_manager_person_id = assignee_id
        elif result.assignment_target == "site_coordinator" and assignee_id:
            if not ticket.site_coordinator_person_id:
                ticket.site_coordinator_person_id = assignee_id
        elif result.assignment_target == "technician" and assignee_id:
            if not ticket.assigned_to_person_id:
                ticket.assigned_to_person_id = assignee_id
            if not any(row.person_id == assignee_id for row in ticket.assignees):
                db.add(TicketAssignee(ticket_id=ticket.id, person_id=assignee_id))
        return result.as_dict()

    @staticmethod
    def _apply_auto_assignment(ticket: Ticket, db: Session) -> dict[str, Any]:
        rule_result = Tickets._apply_rule_auto_assignment(db, ticket)
        if rule_result is not None:
            return rule_result
        return Tickets._apply_region_auto_assignment(ticket, db)

    @staticmethod
    def _apply_automation_rules(
        db: Session, ticket: Ticket, trigger: AutomationTrigger
    ) -> tuple[str, ...]:
        """Apply policy proposals inside the canonical Ticket writer."""
        from app.models.support import AutomationActionType
        from app.services import support_automation

        if support_automation.identity_review_blocks_automation(ticket):
            support_automation.mark_identity_automation_suppressed(ticket)
            return ()

        applied: list[str] = []
        for proposal in support_automation.evaluate_rules(db, ticket, trigger):
            if proposal.action_type == AutomationActionType.assign_team:
                ticket.service_team_id = proposal.service_team_id
            elif proposal.action_type == AutomationActionType.assign_technician:
                ticket.technician_person_id = proposal.technician_person_id
            elif proposal.action_type == AutomationActionType.set_priority:
                ticket.priority = proposal.priority or ticket.priority
            elif proposal.action_type == AutomationActionType.set_status:
                if proposal.status:
                    try:
                        transition_ticket_status(
                            ticket, proposal.status, source="automation"
                        )
                    except ValueError:
                        logger.warning(
                            "automation set_status ignored invalid status %r",
                            proposal.status,
                        )
                        continue
            elif proposal.action_type == AutomationActionType.set_due_in_hours:
                if proposal.due_in_hours is not None:
                    ticket.due_at = _now() + timedelta(hours=proposal.due_in_hours)
            elif proposal.action_type == AutomationActionType.add_tag:
                if proposal.tag:
                    tags = list(ticket.tags or [])
                    if proposal.tag not in tags:
                        tags.append(proposal.tag)
                        ticket.tags = tags
            applied.append(proposal.rule_name)
        return tuple(applied)

    @staticmethod
    def _queue_notifications_for_assignments(
        db: Session, ticket: Ticket, actor_id: str | None
    ) -> None:
        if not Tickets._notifications_enabled(db):
            return

        recipients: set[str] = set()
        for candidate in [
            ticket.technician_person_id,
            ticket.ticket_manager_person_id,
            ticket.site_coordinator_person_id,
            ticket.assigned_to_person_id,
        ]:
            if candidate:
                recipients.add(str(candidate))

        assignee_rows = (
            db.query(TicketAssignee).filter(TicketAssignee.ticket_id == ticket.id).all()
        )
        recipients.update(str(row.person_id) for row in assignee_rows)

        if ticket.service_team_id:
            team_map = _read_json_setting(
                db, SettingDomain.workflow, SUPPORT_SERVICE_TEAM_MEMBERS_KEY
            )
            team_members = (
                team_map.get(str(ticket.service_team_id))
                if isinstance(team_map, dict)
                else None
            )
            if isinstance(team_members, list):
                for member in team_members:
                    member_uuid = _coerce_uuid(str(member))
                    if member_uuid:
                        recipients.add(str(member_uuid))

        if actor_id:
            recipients.discard(str(actor_id))

        subject = f"Ticket assigned: {ticket.number or str(ticket.id)[:8]}"
        body = f"Ticket {ticket.number or ticket.id} assignment updated."
        for recipient in recipients:
            queue_staff_push(
                db,
                recipient=recipient,
                subject=subject,
                body=body,
            )

        # Email queue for service-team assignments.
        if ticket.service_team_id:
            for recipient in recipients:
                queue_staff_email(
                    db,
                    recipient=recipient,
                    subject=subject,
                    body=body,
                )

    @staticmethod
    def _queue_mention_notifications(
        db: Session, ticket: Ticket, body: str, actor_id: str | None
    ) -> None:
        if not Tickets._notifications_enabled(db):
            return

        mentioned_emails = _parse_mentions(body)
        if not mentioned_emails:
            return

        from app.services import subscriber as subscriber_service

        recipients = subscriber_service.subscribers.list_active_by_emails(
            db, mentioned_emails
        )
        subject = f"Mentioned in ticket {ticket.number or str(ticket.id)[:8]}"
        for subscriber in recipients:
            if actor_id and str(subscriber.id) == str(actor_id):
                continue
            notification_service.notifications.create_customer_notification(
                db,
                NotificationCreate(
                    channel=NotificationChannel.push,
                    recipient=str(subscriber.id),
                    subject=subject,
                    body=body,
                    status=NotificationStatus.delivered,
                    sent_at=_now(),
                ),
            )

    @staticmethod
    def _queue_customer_ticket_update(
        db: Session,
        ticket: Ticket,
        *,
        event_type: str,
        subject: str,
        body: str,
        dedupe_key: str,
        extra_metadata: dict[str, object] | None = None,
    ) -> None:
        """Tell the customer their ticket moved.

        Channels are NOT chosen here — the defaults below are a fallback and
        ``notification_channel_policy`` resolves the actual channels (see
        ``docs/designs/NOTIFICATION_CHANNEL_POLICY.md``). Silence on a ticket is
        the single largest driver of "any update?" contacts, so every
        customer-visible movement gets one message.
        """
        if not Tickets._notifications_enabled(db):
            return

        subscriber_id = ticket.subscriber_id or ticket.customer_account_id
        if not subscriber_id:
            return
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            return

        from app.services import customer_experience_communications

        customer_experience_communications.request_update(
            db,
            subscriber_id=subscriber.id,
            event_type=event_type,
            subject=subject,
            body=body,
            metadata={
                "type": "ticket",
                "ticket_id": str(ticket.id),
                "ticket_number": ticket.number,
                **(extra_metadata or {}),
            },
            dedupe_key=dedupe_key,
            default_channels=(
                NotificationChannel.email,
                NotificationChannel.whatsapp,
                NotificationChannel.push,
            ),
        )

    @staticmethod
    def _notify_customer_of_comment(
        db: Session, ticket: Ticket, comment: TicketComment
    ) -> None:
        """Notify the customer when staff reply visibly on their ticket.

        Internal notes and the customer's own comments are never echoed back.
        """
        if comment.is_internal:
            return
        if comment.author_type != TicketCommentAuthorType.staff.value:
            return

        ticket_ref = ticket.number or str(ticket.id)[:8]
        preview = " ".join((comment.body or "").split())[:400]
        Tickets._queue_customer_ticket_update(
            db,
            ticket,
            event_type="support_ticket_comment_added",
            subject=f"Update on support ticket {ticket_ref}",
            body=f"There is a new update on ticket {ticket_ref}:\n\n{preview}",
            dedupe_key=f"ticket-comment:{comment.id}",
            extra_metadata={"comment_id": str(comment.id)},
        )

    @staticmethod
    def _notify_customer_of_status_change(
        db: Session, ticket: Ticket, previous_status: str
    ) -> None:
        """Notify the customer when their ticket reaches a state they care about.

        Deliberately narrow: intermediate triage churn is noise to a customer,
        while "we're working on it", "we need you", and "it's closed" are not.
        """
        current = ticket.status
        if current == previous_status:
            return
        if current not in CUSTOMER_NOTIFIED_TICKET_STATUSES:
            return

        ticket_ref = ticket.number or str(ticket.id)[:8]
        message = CUSTOMER_NOTIFIED_TICKET_STATUSES[current]
        Tickets._queue_customer_ticket_update(
            db,
            ticket,
            event_type="support_ticket_status_changed",
            subject=f"Support ticket {ticket_ref}: {message['label']}",
            body=f"Ticket {ticket_ref} ({ticket.title}) {message['body']}",
            dedupe_key=Tickets._status_change_dedupe_key(
                ticket, previous_status, current
            ),
            extra_metadata={"from_status": previous_status, "to_status": current},
        )

    @staticmethod
    def _status_change_dedupe_key(
        ticket: Ticket, previous_status: str, current: str
    ) -> str:
        """Identify one *transition*, not one status.

        Intent dedupe is permanent and global (``communication_intents.submit``
        looks the key up with ``one_or_none()`` over all time), so keying on the
        status alone silences every later re-entry into it. A ticket parked
        on_hold is told "we'll let you know when it resumes" and then never
        hears the resume; a ticket that bounces back to waiting_on_customer asks
        for input once and then stalls silently. Both are the "any update?"
        contact this notification exists to prevent.

        The minute bucket keeps genuine idempotency — a retry or double submit
        of the same transition still collapses — while letting a later,
        distinct transition through.
        """
        stamp = _now().strftime("%Y%m%d%H%M")
        return f"ticket-status:{ticket.id}:{previous_status}->{current}:{stamp}"

    @staticmethod
    def _queue_resolution_confirmation_notifications(
        db: Session, ticket: Ticket, token_row: TicketAccessToken
    ) -> None:
        if not Tickets._notifications_enabled(db):
            return

        action_url = ticket_access_tokens.action_urls(token_row).get("confirm_url")
        if not action_url:
            return

        subscriber_id = ticket.subscriber_id or ticket.customer_account_id
        if not subscriber_id:
            return

        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            return

        ticket_ref = ticket.number or str(ticket.id)[:8]
        subject = f"Confirm support ticket {ticket_ref}"
        body = (
            f"We marked ticket {ticket_ref} as resolved. "
            f"Confirm or dispute it here: {action_url}"
        )

        from app.services import customer_experience_communications

        customer_experience_communications.request_update(
            db,
            subscriber_id=subscriber.id,
            event_type="support_ticket_resolution_confirmation",
            subject=subject,
            body=body,
            metadata={
                "type": "ticket",
                "ticket_id": str(ticket.id),
                "ticket_number": ticket.number,
                "confirmation_token_id": str(token_row.id),
            },
            dedupe_key=f"ticket-resolution-confirmation:{token_row.id}",
            default_channels=(
                NotificationChannel.email,
                NotificationChannel.whatsapp,
                NotificationChannel.push,
            ),
        )

    @staticmethod
    def _emit_ticket_event(
        db: Session, event_name: str, ticket: Ticket, actor_id: str | None = None
    ) -> None:
        payload = {
            "name": event_name,
            "ticket_id": str(ticket.id),
            "ticket_number": ticket.number,
            "status": ticket.status,
            "priority": ticket.priority,
            "channel": ticket.channel.value,
            "customer_account_id": str(ticket.customer_account_id)
            if ticket.customer_account_id
            else None,
            "subscriber_id": str(ticket.subscriber_id)
            if ticket.subscriber_id
            else None,
            "actor_id": actor_id,
        }
        emit_event(
            db,
            EventType.custom,
            payload,
            actor=actor_id,
            subscriber_id=ticket.subscriber_id,
            account_id=ticket.customer_account_id or ticket.subscriber_id,
        )

    @staticmethod
    @ticket_owner_command("create")
    def create(
        db: Session,
        payload: TicketCreate,
        actor_id: str | None = None,
        request=None,
        *,
        origin_conversation_id: UUID | None = None,
    ) -> Ticket:
        """Create a ticket inside the canonical Ticket transaction.

        ``origin_conversation_id`` is keyword-only and deliberately absent from
        ``TicketCreate``: only ``communications.conversation_ticket_handoff``
        may assert that a ticket originated from an inbox conversation, so the
        claim must not be settable by anything that can post a ticket payload.
        """
        ticket_validation.validate_ticket_creation(db, payload)
        data = payload.model_dump()
        data["status"] = data.get(
            "status"
        ) or support_ticket_settings_service.default_status(db)
        data["priority"] = data.get(
            "priority"
        ) or support_ticket_settings_service.default_priority(db)
        data["created_by_person_id"] = _coerce_subscriber_uuid(
            db, data.get("created_by_person_id")
        )
        _apply_inbound_identity_resolution(db, data)
        _validate_ticket_lead_alignment(db, data)

        ticket = Ticket(
            **{
                k: v
                for k, v in data.items()
                if k
                not in {
                    "assignee_person_ids",
                    "related_outage_ticket_id",
                    "inbound_sender",
                    "inbound_sender_type",
                }
            }
        )
        if origin_conversation_id is not None:
            ticket.origin_conversation_id = origin_conversation_id
        ticket.number = Tickets._resolve_ticket_number(db)

        db.add(ticket)
        db.flush()

        assignee_ids = payload.assignee_person_ids or []
        if assignee_ids:
            Tickets._replace_assignees(db, ticket, assignee_ids)

        if payload.related_outage_ticket_id:
            link = TicketLink(
                from_ticket_id=ticket.id,
                to_ticket_id=payload.related_outage_ticket_id,
                link_type="related_outage",
                created_by_person_id=_coerce_subscriber_uuid(db, actor_id),
            )
            db.add(link)

        if Tickets._auto_assignment_enabled(db):
            Tickets._apply_auto_assignment(ticket, db)

        Tickets._apply_sla_policy(
            db, ticket, explicit_due_at=payload.due_at is not None
        )
        from app.services import sla_assignment

        sla_assignment.create_sla_clock_for_ticket(db, ticket)
        Tickets._apply_status_timestamp_rules(ticket, data)
        Tickets._apply_ncc_categorisation(ticket, data)

        from app.models.support import AutomationTrigger

        Tickets._apply_automation_rules(db, ticket, AutomationTrigger.ticket_created)

        Tickets._queue_notifications_for_assignments(db, ticket, actor_id)

        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"number": ticket.number},
        )
        Tickets._emit_ticket_event(db, "ticket.created", ticket, actor_id)

        db.flush()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def get(db: Session, ticket_id: str) -> Ticket:
        ticket = db.get(Ticket, ticket_id)
        if not ticket or not ticket.is_active:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        return ticket

    @staticmethod
    @ticket_owner_command("set_satisfaction")
    def set_satisfaction(
        db: Session, ticket: Ticket, *, rating: int, comment: str | None = None
    ) -> Ticket:
        """Record a customer CSAT rating (1-5 + optional comment) on a resolved
        or closed ticket, stored under ``metadata.csat``. Re-rating overwrites.
        Rejects tickets that aren't resolved/closed so support is rated on the
        outcome, not mid-flight."""
        if ticket.status not in (
            TicketStatus.resolved.value,
            TicketStatus.closed.value,
        ):
            raise _ticket_error(
                "satisfaction_not_eligible",
                "You can rate support once the ticket is resolved.",
            )
        meta = dict(ticket.metadata_ or {})
        meta["csat"] = {
            "rating": int(rating),
            "comment": (comment or "").strip() or None,
            "at": datetime.now(UTC).isoformat(),
        }
        ticket.metadata_ = meta
        db.add(ticket)
        db.flush()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def _add_system_comment(
        db: Session,
        ticket: Ticket,
        body: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TicketComment:
        comment = TicketComment(
            ticket_id=ticket.id,
            author_type=TicketCommentAuthorType.system.value,
            body=body,
            is_internal=True,
            metadata_=metadata or None,
        )
        db.add(comment)
        return comment

    @staticmethod
    @ticket_owner_command("request_resolution_confirmation")
    def request_resolution_confirmation(
        db: Session,
        ticket_id: str,
        *,
        actor_id: str | None = None,
        grace_hours: int = 24,
        request=None,
    ) -> tuple[Ticket, TicketAccessToken]:
        ticket = Tickets.get(db, ticket_id)
        _ensure_not_merged_source(ticket)
        if ticket.status in _TICKET_TERMINAL_STATUSES:
            raise _ticket_error(
                "resolution_confirmation_not_eligible",
                "Cannot request confirmation for a closed ticket.",
            )

        now = _now()
        transition_ticket_status(
            ticket,
            TicketStatus.pending_confirmation,
            source="resolution_confirmation_request",
            allow_reopen=False,
        )
        ticket.resolved_at = now
        ticket.closed_at = None
        meta = dict(ticket.metadata_ or {})
        confirmation = dict(meta.get("resolution_confirmation") or {})
        confirmation.update(
            {
                "requested_at": now.isoformat(),
                "requested_by": actor_id,
                "grace_hours": int(grace_hours),
                "customer_confirmed_at": None,
                "customer_disputed_at": None,
                "customer_dispute_reason": None,
                "auto_confirmed_at": None,
            }
        )
        meta["resolution_confirmation"] = confirmation
        ticket.metadata_ = meta

        token_row = ticket_access_tokens.mint(
            db,
            ticket,
            purpose="resolution_confirm",
            ttl_days=max(1, int(grace_hours // 24) or 1),
        )
        Tickets._add_system_comment(
            db,
            ticket,
            "Resolution confirmation requested from the customer.",
            metadata={"source": "resolution_confirmation_request"},
        )
        log_audit_event(
            db=db,
            request=request,
            action="resolution_confirmation_request",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"token_id": str(token_row.id), "grace_hours": int(grace_hours)},
        )
        Tickets._queue_resolution_confirmation_notifications(db, ticket, token_row)
        db.flush()
        db.refresh(ticket)
        db.refresh(token_row)
        return ticket, token_row

    @staticmethod
    @ticket_owner_command("confirm_resolution")
    def confirm_resolution(
        db: Session,
        token_row: TicketAccessToken,
        *,
        auto: bool = False,
    ) -> Ticket:
        ticket = token_row.ticket or db.get(Ticket, token_row.ticket_id)
        if not ticket or not ticket.is_active:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        if ticket.status == TicketStatus.closed.value:
            return ticket
        _ensure_not_merged_source(ticket)
        if ticket.status in {TicketStatus.canceled.value, TicketStatus.merged.value}:
            raise _ticket_error(
                "resolution_confirmation_terminal",
                "Cannot confirm a terminal ticket.",
            )

        now = _now()
        meta = dict(ticket.metadata_ or {})
        confirmation = dict(meta.get("resolution_confirmation") or {})
        confirmation.update(
            {
                "customer_confirmed_at": None if auto else now.isoformat(),
                "auto_confirmed_at": now.isoformat() if auto else None,
                "auto_confirmed": bool(auto),
            }
        )
        meta["resolution_confirmation"] = confirmation
        ticket.metadata_ = meta
        transition_ticket_status(
            ticket,
            TicketStatus.closed,
            source="resolution_confirmation_auto" if auto else "resolution_confirmed",
            allow_reopen=False,
        )
        if ticket.resolved_at is None:
            ticket.resolved_at = now
        ticket.closed_at = now
        token_row.responded_at = now
        token_row.is_active = False

        Tickets._add_system_comment(
            db,
            ticket,
            "Resolution auto-confirmed after the customer grace period."
            if auto
            else "Customer confirmed the ticket resolution.",
            metadata={"source": "resolution_confirmation", "auto": bool(auto)},
        )
        log_audit_event(
            db=db,
            request=None,
            action="resolution_confirmed",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=None,
            metadata={"token_id": str(token_row.id), "auto": bool(auto)},
        )
        db.flush()
        db.refresh(ticket)
        return ticket

    @staticmethod
    @ticket_owner_command("respond_to_resolution_for_customer")
    def respond_to_resolution_for_customer(
        db: Session,
        ticket: Ticket,
        *,
        confirm: bool,
        reason: str | None = None,
    ) -> Ticket:
        """Authenticated self-care adapter into the resolution owner.

        The public magic link and signed-in clients converge on the same active
        confirmation capability and therefore the same mutation/audit path.
        """
        if ticket.status != TicketStatus.pending_confirmation.value:
            raise _ticket_error(
                "resolution_confirmation_not_pending",
                "This ticket is not awaiting resolution confirmation.",
            )
        token_row = (
            db.query(TicketAccessToken)
            .filter(TicketAccessToken.ticket_id == ticket.id)
            .filter(TicketAccessToken.purpose == "resolution_confirm")
            .filter(TicketAccessToken.is_active.is_(True))
            .filter(TicketAccessToken.responded_at.is_(None))
            .filter(
                or_(
                    TicketAccessToken.expires_at.is_(None),
                    TicketAccessToken.expires_at >= _now(),
                )
            )
            .order_by(TicketAccessToken.created_at.desc())
            .first()
        )
        if token_row is None:
            raise _ticket_error(
                "resolution_confirmation_inactive",
                "The resolution confirmation request is no longer active.",
            )
        token_row.ticket = ticket
        if confirm:
            return Tickets.confirm_resolution(db, token_row)
        return Tickets.dispute_resolution(db, token_row, reason=reason)

    @staticmethod
    @ticket_owner_command("dispute_resolution")
    def dispute_resolution(
        db: Session,
        token_row: TicketAccessToken,
        *,
        reason: str | None = None,
    ) -> Ticket:
        ticket = token_row.ticket or db.get(Ticket, token_row.ticket_id)
        if not ticket or not ticket.is_active:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        _ensure_not_merged_source(ticket)
        if ticket.status in _TICKET_TERMINAL_STATUSES:
            raise _ticket_error(
                "resolution_dispute_terminal",
                "Cannot dispute a terminal ticket.",
            )

        now = _now()
        clean_reason = (reason or "").strip() or None
        meta = dict(ticket.metadata_ or {})
        confirmation = dict(meta.get("resolution_confirmation") or {})
        confirmation.update(
            {
                "customer_disputed_at": now.isoformat(),
                "customer_dispute_reason": clean_reason,
                "auto_confirmed": False,
            }
        )
        meta["resolution_confirmation"] = confirmation
        ticket.metadata_ = meta
        transition_ticket_status(
            ticket,
            TicketStatus.open,
            source="resolution_disputed",
            allow_reopen=False,
        )
        ticket.resolved_at = None
        ticket.closed_at = None
        token_row.responded_at = now
        token_row.is_active = False

        body = "Customer disputed the ticket resolution."
        if clean_reason:
            body = f"{body}\n\nReason: {clean_reason}"
        Tickets._add_system_comment(
            db,
            ticket,
            body,
            metadata={"source": "resolution_dispute"},
        )
        log_audit_event(
            db=db,
            request=None,
            action="resolution_disputed",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=None,
            metadata={"token_id": str(token_row.id), "has_reason": bool(clean_reason)},
        )
        db.flush()
        db.refresh(ticket)
        return ticket

    @staticmethod
    @ticket_owner_command("auto_confirm_pending")
    def auto_confirm_pending(
        db: Session,
        *,
        default_grace_hours: int = 24,
        now: datetime | None = None,
    ) -> int:
        clock = _as_utc(now) or _now()
        candidates = (
            db.query(Ticket)
            .filter(Ticket.is_active.is_(True))
            .filter(Ticket.status == TicketStatus.pending_confirmation.value)
            .all()
        )
        confirmed = 0
        for ticket in candidates:
            meta = dict(ticket.metadata_ or {})
            confirmation = dict(meta.get("resolution_confirmation") or {})
            grace_hours = int(
                confirmation.get("grace_hours") or default_grace_hours or 24
            )
            resolved_at = _as_utc(ticket.resolved_at)
            if (
                resolved_at is None
                or resolved_at + timedelta(hours=grace_hours) > clock
            ):
                continue
            token_row = (
                db.query(TicketAccessToken)
                .filter(TicketAccessToken.ticket_id == ticket.id)
                .filter(TicketAccessToken.purpose == "resolution_confirm")
                .filter(TicketAccessToken.is_active.is_(True))
                .order_by(TicketAccessToken.created_at.desc())
                .first()
            )
            if token_row is None:
                token_row = ticket_access_tokens.mint(
                    db,
                    ticket,
                    purpose="resolution_confirm",
                    ttl_days=max(1, grace_hours // 24 or 1),
                )
                db.flush()
            try:
                Tickets.confirm_resolution(db, token_row, auto=True)
                confirmed += 1
            except SupportTicketError:
                logger.exception(
                    "ticket_auto_confirm_failed",
                    extra={
                        "event": "ticket_auto_confirm_failed",
                        "ticket_id": str(ticket.id),
                    },
                )
        return confirmed

    @staticmethod
    @ticket_owner_command("add_attachments")
    def add_attachments(
        db: Session, ticket_id: str, attachments: list[dict] | None
    ) -> Ticket:
        ticket = Tickets.get(db, ticket_id)
        ticket.attachments = _merge_attachment_dicts(ticket.attachments, attachments)
        db.add(ticket)
        db.flush()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def get_by_lookup(db: Session, ticket_lookup: str) -> Ticket:
        lookup = ticket_lookup.strip()
        ticket_uuid = _coerce_uuid(lookup)
        query = (
            db.query(Ticket)
            .options(selectinload(Ticket.assignees))
            .filter(Ticket.is_active.is_(True))
        )
        if ticket_uuid:
            query = query.filter(or_(Ticket.id == ticket_uuid, Ticket.number == lookup))
        else:
            query = query.filter(Ticket.number == lookup)
        ticket = query.first()
        if not ticket:
            raise _ticket_error("ticket_not_found", "Ticket not found")
        return ticket

    @staticmethod
    def query(
        db: Session,
        search: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        assigned_to_person_id: str | None = None,
        project_manager_person_id: str | None = None,
        site_coordinator_person_id: str | None = None,
        subscriber_id: str | None = None,
        priority: str | None = None,
        channel: str | None = None,
        created_by_person_id: str | None = None,
        is_active: bool | None = None,
        filters: str | None = None,
    ):
        """Build the canonical filtered ticket query before ordering/pagination."""

        query = db.query(Ticket).options(selectinload(Ticket.assignees))
        # Default (None) keeps the legacy active-only behavior.
        if is_active is None or is_active:
            query = query.filter(Ticket.is_active.is_(True))
        else:
            query = query.filter(Ticket.is_active.is_(False))
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    Ticket.number.ilike(like),
                    Ticket.title.ilike(like),
                    Ticket.description.ilike(like),
                )
            )
        if status:
            query = query.filter(Ticket.status == str(status).strip())
        if ticket_type:
            query = query.filter(Ticket.ticket_type == ticket_type)
        if priority:
            query = query.filter(Ticket.priority == str(priority).strip())
        if channel:
            query = query.filter(Ticket.channel == str(channel).strip())
        if created_by_person_id:
            query = query.filter(Ticket.created_by_person_id == created_by_person_id)
        if assigned_to_person_id:
            query = query.filter(
                or_(
                    Ticket.assigned_to_person_id == assigned_to_person_id,
                    Ticket.id.in_(
                        db.query(TicketAssignee.ticket_id).filter(
                            TicketAssignee.person_id == assigned_to_person_id
                        )
                    ),
                )
            )
        if project_manager_person_id:
            query = query.filter(
                Ticket.ticket_manager_person_id == project_manager_person_id
            )
        if site_coordinator_person_id:
            query = query.filter(
                Ticket.site_coordinator_person_id == site_coordinator_person_id
            )
        if subscriber_id:
            query = query.filter(ticket_customer_link_filter(Ticket, subscriber_id))
        if filters:
            try:
                filter_clause = support_ticket_filters.build_ticket_filter_clause(
                    filters
                )
            except FilterValidationError as exc:
                raise _ticket_error("invalid_ticket_filter", str(exc)) from exc
            if filter_clause is not None:
                query = query.filter(filter_clause)

        return query

    @staticmethod
    def count(
        db: Session,
        search: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        assigned_to_person_id: str | None = None,
        project_manager_person_id: str | None = None,
        site_coordinator_person_id: str | None = None,
        subscriber_id: str | None = None,
        priority: str | None = None,
        channel: str | None = None,
        created_by_person_id: str | None = None,
        is_active: bool | None = None,
        filters: str | None = None,
    ) -> int:
        """Count the same canonical ticket scope consumed by list projections."""

        return int(
            Tickets.query(
                db,
                search=search,
                status=status,
                ticket_type=ticket_type,
                assigned_to_person_id=assigned_to_person_id,
                project_manager_person_id=project_manager_person_id,
                site_coordinator_person_id=site_coordinator_person_id,
                subscriber_id=subscriber_id,
                priority=priority,
                channel=channel,
                created_by_person_id=created_by_person_id,
                is_active=is_active,
                filters=filters,
            )
            .order_by(None)
            .count()
        )

    @staticmethod
    def list(
        db: Session,
        search: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        assigned_to_person_id: str | None = None,
        project_manager_person_id: str | None = None,
        site_coordinator_person_id: str | None = None,
        subscriber_id: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int | None = 50,
        offset: int = 0,
        priority: str | None = None,
        channel: str | None = None,
        created_by_person_id: str | None = None,
        is_active: bool | None = None,
        filters: str | None = None,
    ) -> list[Ticket]:
        query = Tickets.query(
            db,
            search=search,
            status=status,
            ticket_type=ticket_type,
            assigned_to_person_id=assigned_to_person_id,
            project_manager_person_id=project_manager_person_id,
            site_coordinator_person_id=site_coordinator_person_id,
            subscriber_id=subscriber_id,
            priority=priority,
            channel=channel,
            created_by_person_id=created_by_person_id,
            is_active=is_active,
            filters=filters,
        )

        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Ticket.created_at,
                "updated_at": Ticket.updated_at,
                "due_at": Ticket.due_at,
                "priority": Ticket.priority,
                "status": Ticket.status,
                "number": Ticket.number,
            },
        )
        query = query.order_by(Ticket.id.asc())
        if limit is None:
            return query.all()
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    @ticket_owner_command("update")
    def update(
        db: Session,
        ticket_id: str,
        payload: TicketUpdate,
        actor_id: str | None = None,
        request=None,
    ) -> Ticket:
        ticket = Tickets.get(db, ticket_id)
        _ensure_not_merged_source(ticket)

        before = {
            "status": ticket.status,
            "priority": ticket.priority,
            "assigned_to_person_id": str(ticket.assigned_to_person_id)
            if ticket.assigned_to_person_id
            else None,
            "ticket_manager_person_id": str(ticket.ticket_manager_person_id)
            if ticket.ticket_manager_person_id
            else None,
            "site_coordinator_person_id": str(ticket.site_coordinator_person_id)
            if ticket.site_coordinator_person_id
            else None,
            "service_team_id": str(ticket.service_team_id)
            if ticket.service_team_id
            else None,
        }

        data = payload.model_dump(exclude_unset=True)
        assignee_person_ids = data.pop("assignee_person_ids", None)

        if "status" in data and data["status"] is None:
            data.pop("status")
        if "priority" in data and data["priority"] is None:
            data.pop("priority")

        _validate_ticket_lead_alignment(db, data, existing=ticket)

        previous_status = ticket.status
        if "status" in data:
            # Admin edit may legitimately reopen, but it's validated + audited.
            transition_ticket_status(
                ticket, data.pop("status"), source="admin_update", allow_reopen=True
            )
            Tickets._notify_customer_of_status_change(db, ticket, previous_status)

        for key, value in data.items():
            setattr(ticket, key, value)

        Tickets._apply_status_timestamp_rules(ticket, data)
        Tickets._apply_ncc_categorisation(ticket, data)

        if assignee_person_ids is not None:
            Tickets._replace_assignees(db, ticket, assignee_person_ids)

        if Tickets._auto_assignment_enabled(db):
            Tickets._apply_auto_assignment(ticket, db)

        Tickets._apply_sla_policy(db, ticket, explicit_due_at="due_at" in data)
        from app.services import sla_assignment

        sla_assignment.create_sla_clock_for_ticket(db, ticket)
        if before["status"] != ticket.status:
            sla_assignment.update_sla_clocks_for_status_change(
                db, ticket, before["status"], ticket.status
            )
        Tickets._queue_notifications_for_assignments(db, ticket, actor_id)

        after = {
            "status": ticket.status,
            "priority": ticket.priority,
            "assigned_to_person_id": str(ticket.assigned_to_person_id)
            if ticket.assigned_to_person_id
            else None,
            "ticket_manager_person_id": str(ticket.ticket_manager_person_id)
            if ticket.ticket_manager_person_id
            else None,
            "site_coordinator_person_id": str(ticket.site_coordinator_person_id)
            if ticket.site_coordinator_person_id
            else None,
            "service_team_id": str(ticket.service_team_id)
            if ticket.service_team_id
            else None,
        }

        changes = {
            field: {"from": before[field], "to": after[field]}
            for field in before.keys()
            if before[field] != after[field]
        }
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"changes": changes} if changes else None,
        )

        if before["status"] != after["status"]:
            log_audit_event(
                db=db,
                request=request,
                action="status_change",
                entity_type="support_ticket",
                entity_id=str(ticket.id),
                actor_id=actor_id,
                metadata={"from": before["status"], "to": after["status"]},
            )
            from app.models.support import AutomationTrigger

            Tickets._apply_automation_rules(
                db, ticket, AutomationTrigger.status_changed
            )
        if before["priority"] != after["priority"]:
            log_audit_event(
                db=db,
                request=request,
                action="priority_change",
                entity_type="support_ticket",
                entity_id=str(ticket.id),
                actor_id=actor_id,
                metadata={"from": before["priority"], "to": after["priority"]},
            )
            from app.models.support import AutomationTrigger

            Tickets._apply_automation_rules(
                db, ticket, AutomationTrigger.priority_changed
            )

        if changes and any(
            key in changes
            for key in [
                "assigned_to_person_id",
                "ticket_manager_person_id",
                "site_coordinator_person_id",
                "service_team_id",
            ]
        ):
            Tickets._emit_ticket_event(db, "ticket.assigned", ticket, actor_id)

        db.flush()
        db.refresh(ticket)
        previous_assignee = before.get("assigned_to_person_id")
        db.info["_support_previous_assignee_id"] = (
            UUID(previous_assignee) if previous_assignee else None
        )
        return ticket

    @staticmethod
    @ticket_owner_command("soft_delete")
    def soft_delete(
        db: Session, ticket_id: str, actor_id: str | None = None, request=None
    ) -> None:
        ticket = Tickets.get(db, ticket_id)
        _ensure_not_merged_source(ticket)
        ticket.is_active = False
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"soft_deleted": True},
        )
        db.flush()

    @staticmethod
    @ticket_owner_command("bulk_update")
    def bulk_update(
        db: Session,
        payload: TicketBulkUpdateRequest,
        actor_id: str | None = None,
        request=None,
    ) -> list[Ticket]:
        prepared: list[tuple[str, TicketUpdate]] = []
        for item in payload.items:
            ticket = Tickets.get(db, str(item.ticket_id))
            _ensure_not_merged_source(ticket)
            if item.status is not None and item.status not in _VALID_TICKET_STATUSES:
                raise ValueError(f"Invalid ticket status: {item.status!r}")
            update_data = {
                key: value
                for key, value in item.model_dump(
                    exclude={"ticket_id"}, exclude_unset=True
                ).items()
                if value is not None
            }
            prepared.append(
                (str(item.ticket_id), TicketUpdate.model_validate(update_data))
            )

        # The single-ticket owner applies SLA, automation, assignment,
        # work-order, notification, event, audit, and workqueue consequences.
        # Bulk adapters must not maintain a second mutation path.
        updated = [
            Tickets.update(
                db,
                ticket_id,
                update,
                actor_id=actor_id,
                request=request,
            )
            for ticket_id, update in prepared
        ]

        log_audit_event(
            db=db,
            request=request,
            action="bulk_update",
            entity_type="support_ticket",
            entity_id=None,
            actor_id=actor_id,
            metadata={"count": len(updated)},
        )
        db.flush()
        return updated

    @staticmethod
    @ticket_owner_command("manual_auto_assign")
    def manual_auto_assign(
        db: Session, ticket_id: str, actor_id: str | None = None, request=None
    ) -> dict[str, Any]:
        ticket = Tickets.get(db, ticket_id)
        _ensure_not_merged_source(ticket)
        result = Tickets._apply_auto_assignment(ticket, db)
        log_audit_event(
            db=db,
            request=request,
            action="auto_assignment",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"result": result},
        )
        Tickets._queue_notifications_for_assignments(db, ticket, actor_id)
        db.flush()
        return result

    @staticmethod
    @ticket_owner_command("create_comment")
    def create_comment(
        db: Session,
        ticket_id: str,
        payload: TicketCommentCreate,
        actor_id: str | None = None,
        request=None,
        mentioned_agent_ids: Sequence[UUID] = (),
    ) -> TicketComment:
        ticket = Tickets.get(db, ticket_id)
        comment = ticket_comments.create(
            db, ticket=ticket, payload=payload, actor_id=actor_id, request=request
        )
        Tickets._queue_mention_notifications(db, ticket, payload.body, actor_id)
        Tickets._notify_customer_of_comment(db, ticket, comment)
        if mentioned_agent_ids:
            from app.services import ticket_mentions

            ticket_mentions.notify_ticket_comment_mentions(
                db,
                ticket_id=str(ticket.id),
                ticket_number=ticket.number,
                ticket_title=ticket.title,
                comment_preview=payload.body[:300],
                mentioned_agent_ids=[str(item) for item in mentioned_agent_ids],
                actor_person_id=actor_id,
            )
        db.flush()
        db.refresh(comment)
        return comment

    @staticmethod
    @ticket_owner_command("bulk_create_comments")
    def bulk_create_comments(
        db: Session,
        ticket_id: str,
        payloads: list[TicketCommentCreate],
        actor_id: str | None = None,
        request=None,
    ) -> list[TicketComment]:
        ticket = Tickets.get(db, ticket_id)
        comments: list[TicketComment] = []
        for payload in payloads:
            comment = ticket_comments.create(
                db, ticket=ticket, payload=payload, actor_id=actor_id, request=request
            )
            Tickets._queue_mention_notifications(db, ticket, payload.body, actor_id)
            comments.append(comment)
        db.flush()
        for comment in comments:
            db.refresh(comment)
        return comments

    @staticmethod
    @ticket_owner_command("link_ticket")
    def link_ticket(
        db: Session,
        *,
        from_ticket_id: str,
        to_ticket_id: str,
        link_type: str,
        actor_id: str | None,
        request=None,
    ) -> TicketLink:
        source = Tickets.get(db, from_ticket_id)
        target = Tickets.get(db, to_ticket_id)
        _ensure_not_merged_source(source)
        _ensure_not_merged_source(target)

        existing = (
            db.query(TicketLink)
            .filter(TicketLink.from_ticket_id == source.id)
            .filter(TicketLink.to_ticket_id == target.id)
            .filter(TicketLink.link_type == link_type)
            .first()
        )
        if existing:
            return existing

        link = TicketLink(
            from_ticket_id=source.id,
            to_ticket_id=target.id,
            link_type=link_type,
            created_by_person_id=_coerce_subscriber_uuid(db, actor_id),
        )
        db.add(link)
        log_audit_event(
            db=db,
            request=request,
            action="link",
            entity_type="support_ticket",
            entity_id=str(source.id),
            actor_id=actor_id,
            metadata={"to_ticket_id": str(target.id), "link_type": link_type},
        )
        db.flush()
        db.refresh(link)
        return link

    @staticmethod
    def list_links(
        db: Session,
        ticket_id: str,
        *,
        limit: int = 100,
    ) -> list[TicketLink]:
        ticket = Tickets.get(db, ticket_id)
        return (
            db.query(TicketLink)
            .filter(
                or_(
                    TicketLink.from_ticket_id == ticket.id,
                    TicketLink.to_ticket_id == ticket.id,
                )
            )
            .order_by(TicketLink.created_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    @ticket_owner_command("merge")
    def merge(
        db: Session,
        source_ticket_id: str,
        payload: TicketMergeRequest,
        actor_id: str | None = None,
        request=None,
    ) -> Ticket:
        source = Tickets.get(db, source_ticket_id)
        target = Tickets.get(db, str(payload.target_ticket_id))
        if source.id == target.id:
            raise _ticket_error(
                "ticket_merge_self", "Cannot merge a ticket into itself"
            )
        _ensure_not_merged_source(source)
        _ensure_not_merged_source(target)

        # Merge comments and attachments.
        db.query(TicketComment).filter(TicketComment.ticket_id == source.id).update(
            {TicketComment.ticket_id: target.id}, synchronize_session=False
        )
        target.attachments = _merge_attachment_dicts(
            target.attachments, source.attachments
        )

        # Merge assignees.
        target_assignee_ids = {
            str(row.person_id)
            for row in db.query(TicketAssignee)
            .filter(TicketAssignee.ticket_id == target.id)
            .all()
        }
        source_assignees = (
            db.query(TicketAssignee).filter(TicketAssignee.ticket_id == source.id).all()
        )
        for row in source_assignees:
            if str(row.person_id) in target_assignee_ids:
                continue
            db.add(TicketAssignee(ticket_id=target.id, person_id=row.person_id))

        # Move/dedupe links.
        links = (
            db.query(TicketLink)
            .filter(
                or_(
                    TicketLink.from_ticket_id == source.id,
                    TicketLink.to_ticket_id == source.id,
                )
            )
            .all()
        )
        for link in links:
            from_id = (
                target.id if link.from_ticket_id == source.id else link.from_ticket_id
            )
            to_id = target.id if link.to_ticket_id == source.id else link.to_ticket_id
            if from_id == to_id:
                db.delete(link)
                continue
            duplicate = (
                db.query(TicketLink)
                .filter(TicketLink.id != link.id)
                .filter(TicketLink.from_ticket_id == from_id)
                .filter(TicketLink.to_ticket_id == to_id)
                .filter(TicketLink.link_type == link.link_type)
                .first()
            )
            if duplicate:
                db.delete(link)
            else:
                link.from_ticket_id = from_id
                link.to_ticket_id = to_id

        db.query(TicketAssignee).filter(TicketAssignee.ticket_id == source.id).delete()

        # Merge is an explicit admin action; a closed/resolved source can still
        # be merged (allow_reopen lets it leave a terminal state into merged).
        transition_ticket_status(
            source, TicketStatus.merged, source="merge", allow_reopen=True
        )
        source.merged_into_ticket_id = target.id

        merge_row = TicketMerge(
            source_ticket_id=source.id,
            target_ticket_id=target.id,
            reason=payload.reason,
            merged_by_person_id=_coerce_subscriber_uuid(db, actor_id),
        )
        db.add(merge_row)

        source_comment_text = (
            f"System: merged into ticket {target.number or target.id}."
        )
        target_comment_text = (
            f"System: merged ticket {source.number or source.id} into this ticket."
        )

        db.add(
            TicketComment(
                ticket_id=source.id,
                author_person_id=_coerce_subscriber_uuid(db, actor_id),
                body=source_comment_text,
                is_internal=True,
                attachments=[],
            )
        )
        db.add(
            TicketComment(
                ticket_id=target.id,
                author_person_id=_coerce_subscriber_uuid(db, actor_id),
                body=target_comment_text,
                is_internal=True,
                attachments=[],
            )
        )

        log_audit_event(
            db=db,
            request=request,
            action="merge",
            entity_type="support_ticket",
            entity_id=str(source.id),
            actor_id=actor_id,
            metadata={"target_ticket_id": str(target.id), "reason": payload.reason},
        )

        db.flush()
        db.refresh(target)
        return target


class TicketAccessTokens:
    _TTL_DAYS = 14

    @staticmethod
    def mint(
        db: Session,
        ticket: Ticket,
        *,
        purpose: str = "resolution_confirm",
        ttl_days: int | None = None,
    ) -> TicketAccessToken:
        db.query(TicketAccessToken).filter(
            TicketAccessToken.ticket_id == ticket.id,
            TicketAccessToken.purpose == purpose,
            TicketAccessToken.is_active.is_(True),
        ).update({"is_active": False, "responded_at": _now()})
        raw_token = secrets.token_urlsafe(32)
        token_row = TicketAccessToken(
            ticket_id=ticket.id,
            token_hash=TicketAccessTokens._hash(raw_token),
            purpose=purpose,
            expires_at=_now()
            + timedelta(days=ttl_days or TicketAccessTokens._TTL_DAYS),
            is_active=True,
        )
        db.add(token_row)
        db.flush()
        token_row.raw_token = raw_token
        return token_row

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def get_by_token(db: Session, token: str) -> TicketAccessToken | None:
        cleaned = str(token or "").strip()
        if not cleaned:
            return None
        return (
            db.query(TicketAccessToken)
            .options(selectinload(TicketAccessToken.ticket))
            .filter(TicketAccessToken.token_hash == TicketAccessTokens._hash(cleaned))
            .first()
        )

    @staticmethod
    def token_state(token_row: TicketAccessToken | None) -> str:
        if token_row is None:
            return "not_found"
        ticket = token_row.ticket
        if not token_row.is_active or token_row.responded_at is not None:
            return "closed"
        if ticket and ticket.status in _TICKET_TERMINAL_STATUSES:
            return "closed"
        expires_at = _as_utc(token_row.expires_at)
        if expires_at is not None and expires_at < _now():
            return "expired"
        return "ok"

    @staticmethod
    @ticket_owner_command("mark_access_token_used")
    def mark_accessed(db: Session, token_row: TicketAccessToken) -> None:
        token_row.accessed_at = _now()
        db.add(token_row)
        db.flush()

    @staticmethod
    def action_urls(token_row: TicketAccessToken) -> dict[str, str | None]:
        base_url = (
            (os.getenv("APP_URL") or os.getenv("PUBLIC_BASE_URL") or "")
            .strip()
            .rstrip("/")
        )
        if not base_url:
            return {"confirm_url": None, "dispute_url": None}
        token = token_row.raw_token
        if not token:
            return {"confirm_url": None, "dispute_url": None}
        page_url = f"{base_url}/ticket-confirm/{token}"
        return {
            "confirm_url": page_url,
            "dispute_url": page_url,
        }


# ---------------------------------------------------------------------------
# Ticket list-page helpers
# ---------------------------------------------------------------------------


def _person_option(row) -> dict[str, str]:
    full_name = " ".join(filter(None, [row.first_name, row.last_name])).strip()
    label = row.display_name or full_name or row.email or str(row.id)
    parts = service_address_service.address_parts(row)
    address = ", ".join(
        [p for p in [parts.address_line1, parts.city, parts.region] if p]
    )
    return {
        "id": str(row.id),
        "label": label,
        "email": row.email or "",
        "phone": row.phone or "",
        "organization": row.company_name or "",
        "address": address,
        "subscriber_number": row.subscriber_number or "",
        "account_number": row.account_number or "",
        "account_status": (row.status.value if row.status else "") or "",
        "plan": "",
        "service_address": address,
    }


def _system_user_option(row) -> dict[str, str]:
    full_name = " ".join(filter(None, [row.first_name, row.last_name])).strip()
    label = row.display_name or full_name or row.email or str(row.id)
    return {
        "id": str(row.id),
        "label": label,
        "email": row.email or "",
        "phone": row.phone or "",
        "organization": "Internal User",
        "address": "",
        "subscriber_number": "",
        "account_number": "",
        "account_status": "active" if row.is_active else "inactive",
        "plan": "",
        "service_address": "",
    }


def _append_included_options(
    options: list[dict[str, str]],
    *,
    include_ids: Sequence[str | UUID] | None,
    resolver,
) -> list[dict[str, str]]:
    if not include_ids:
        return options
    merged = list(options)
    seen = {item["id"] for item in merged if item.get("id")}
    for raw_id in include_ids:
        option = resolver(raw_id)
        if not option:
            continue
        option_id = option.get("id")
        if not option_id or option_id in seen:
            continue
        merged.append(option)
        seen.add(option_id)
    return merged


def person_option(
    db: Session, subscriber_id: str | UUID | None
) -> dict[str, str] | None:
    """Return one subscriber formatted for support form selectors."""
    from app.models.subscriber import Subscriber

    uid = _coerce_uuid(subscriber_id) if subscriber_id else None
    if not uid:
        return None
    row = db.get(Subscriber, uid)
    if not row:
        return None
    return _person_option(row)


def list_people(
    db: Session,
    *,
    limit: int = 500,
    include_ids: Sequence[str | UUID] | None = None,
) -> list[dict[str, str]]:
    """Return active subscribers formatted for ticket people selectors."""
    from app.models.subscriber import Subscriber

    rows = (
        db.query(Subscriber)
        .options(selectinload(Subscriber.addresses))
        .filter(Subscriber.is_active.is_(True))
        .order_by(Subscriber.first_name.asc(), Subscriber.last_name.asc())
        .limit(limit)
        .all()
    )
    return _append_included_options(
        [_person_option(row) for row in rows],
        include_ids=include_ids,
        resolver=lambda raw_id: person_option(db, raw_id),
    )


def staff_option(db: Session, user_id: str | UUID | None) -> dict[str, str] | None:
    """Return one internal user formatted for support assignment selectors."""
    from app.models.system_user import SystemUser

    uid = _coerce_uuid(user_id) if user_id else None
    if not uid:
        return None
    row = db.get(SystemUser, uid)
    if not row:
        return None
    return _system_user_option(row)


def assignment_person_option(
    db: Session, person_id: str | UUID | None
) -> dict[str, str] | None:
    """Resolve a support assignment actor, preferring system users with subscriber fallback."""
    return staff_option(db, person_id) or person_option(db, person_id)


def list_staff(
    db: Session,
    *,
    limit: int = 500,
    include_ids: Sequence[str | UUID] | None = None,
) -> list[dict[str, str]]:
    """Return active internal users for support assignment controls."""
    from app.models.system_user import SystemUser

    rows = (
        db.query(SystemUser)
        .filter(SystemUser.is_active.is_(True))
        .order_by(SystemUser.first_name.asc(), SystemUser.last_name.asc())
        .limit(limit)
        .all()
    )
    return _append_included_options(
        [_system_user_option(row) for row in rows],
        include_ids=include_ids,
        resolver=lambda raw_id: staff_option(db, raw_id),
    )


def list_assignment_people(
    db: Session,
    *,
    limit: int = 500,
    include_ids: Sequence[str | UUID] | None = None,
) -> list[dict[str, str]]:
    """Return assignment options with legacy subscriber fallback for existing tickets."""
    return _append_included_options(
        list_staff(db, limit=limit),
        include_ids=include_ids,
        resolver=lambda raw_id: assignment_person_option(db, raw_id),
    )


def status_totals(db: Session) -> dict[str, int]:
    """Return ticket counts grouped by status."""
    from sqlalchemy import func

    counts = dict.fromkeys(support_ticket_settings_service.list_status_options(db), 0)
    rows = (
        db.query(Ticket.status, func.count(Ticket.id))
        .filter(Ticket.is_active.is_(True))
        .group_by(Ticket.status)
        .all()
    )
    for status_value, count in rows:
        key = str(status_value or "").strip()
        if not key:
            continue
        counts[key] = int(count)
    return counts


def ticket_types(db: Session) -> list[str]:
    """Return configured ticket types for forms and filters."""
    return support_ticket_settings_service.list_ticket_type_options(db)


def regions(db: Session) -> list[str]:
    """Return distinct ticket regions with defaults."""
    rows = (
        db.query(Ticket.region)
        .filter(
            Ticket.is_active.is_(True), Ticket.region.isnot(None), Ticket.region != ""
        )
        .distinct()
        .order_by(Ticket.region.asc())
        .limit(200)
        .all()
    )
    discovered = [str(item[0]) for item in rows if item and item[0]]
    defaults = support_ticket_settings_service.list_region_options(db)
    return sorted(set(discovered + defaults))


tickets = Tickets()
ticket_access_tokens = TicketAccessTokens()
ticket_comments = TicketComments()
ticket_sla_events = TicketSlaEvents()
