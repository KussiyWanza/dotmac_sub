"""Ticket comment @mention notifications for staff users."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.services.branding_config import get_brand
from app.services.common import coerce_uuid
from app.services.owner_commands import execute_owner_savepoint
from app.services.staff_notifications import queue_staff_email, queue_staff_push

logger = logging.getLogger(__name__)

_TICKET_MENTION_USERS_TTL_SECONDS = 30.0
_TICKET_MENTION_USERS_CACHE: tuple[datetime, list[dict[str, str]]] | None = None


@dataclass(frozen=True, slots=True)
class TicketMentionMessageInput:
    """Typed inputs used to render a staff ticket-mention message."""

    ticket_id: UUID
    ticket_number: str | None
    ticket_title: str | None
    comment_preview: str | None
    public_base_url: str


@dataclass(frozen=True, slots=True)
class TicketMentionMessage:
    """Rendered ticket-mention content and its optional safe target."""

    subject: str
    body: str
    target_url: str | None


def _absolute_ticket_url(*, public_base_url: str, ticket_id: UUID) -> str | None:
    base_url = public_base_url.strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"{base_url}/admin/support/tickets/{ticket_id}"


def render_ticket_mention_message(
    message_input: TicketMentionMessageInput,
) -> TicketMentionMessage:
    """Render a ticket mention with a safe absolute link when configured."""
    ref = message_input.ticket_number or str(message_input.ticket_id)
    subject = f"Mentioned in ticket {ref}"
    if message_input.ticket_title:
        subject = f"{subject}: {message_input.ticket_title}"[:200]

    target_url = _absolute_ticket_url(
        public_base_url=message_input.public_base_url,
        ticket_id=message_input.ticket_id,
    )
    body_lines = [
        "You were mentioned in a support ticket comment.",
        f"Ticket: {ref}",
    ]
    if target_url:
        body_lines.append(f"Open: {target_url}")
    body_lines.append(f"Comment: {message_input.comment_preview or ''}")
    return TicketMentionMessage(
        subject=subject,
        body="\n".join(body_lines),
        target_url=target_url,
    )


def list_ticket_mention_users(db: Session, *, limit: int = 200) -> list[dict[str, str]]:
    """Return active staff and group options for ticket comment mentions."""
    global _TICKET_MENTION_USERS_CACHE
    now = datetime.now(UTC)
    cached = _TICKET_MENTION_USERS_CACHE
    if cached and (now - cached[0]).total_seconds() < _TICKET_MENTION_USERS_TTL_SECONDS:
        return list(cached[1])

    safe_limit = max(int(limit or 200), 1)
    users = (
        db.query(SystemUser)
        .filter(SystemUser.is_active.is_(True))
        .order_by(SystemUser.last_name.asc(), SystemUser.first_name.asc())
        .limit(safe_limit)
        .all()
    )
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for user in users:
        user_id = str(user.id)
        if user_id in seen:
            continue
        seen.add(user_id)
        label = (
            user.display_name
            or " ".join([user.first_name or "", user.last_name or ""]).strip()
            or user.email
            or "User"
        )
        items.append(
            {
                "id": f"person:{user_id}",
                "label": label,
                "email": user.email or "",
                "kind": "person",
            }
        )

    groups = (
        db.query(
            ServiceTeam.id,
            ServiceTeam.name,
            func.count(ServiceTeamMember.person_id).label("member_count"),
        )
        .join(ServiceTeamMember, ServiceTeamMember.team_id == ServiceTeam.id)
        .filter(ServiceTeam.is_active.is_(True))
        .filter(ServiceTeamMember.is_active.is_(True))
        .group_by(ServiceTeam.id, ServiceTeam.name)
        .order_by(ServiceTeam.name.asc())
        .limit(safe_limit)
        .all()
    )
    for team_id, team_name, member_count in groups:
        items.append(
            {
                "id": f"group:{team_id}",
                "label": f"{team_name or 'Group'} (Group)"
                if int(member_count or 0) > 0
                else team_name or "Group",
                "email": "",
                "kind": "group",
            }
        )

    _TICKET_MENTION_USERS_CACHE = (now, list(items))
    return items


def resolve_mentioned_person_ids(
    db: Session, mentioned_agent_ids: list[str] | None
) -> list[str]:
    """Resolve CRM-style mention tokens into system user UUID strings."""
    if not mentioned_agent_ids:
        return []

    person_ids: list[UUID] = []
    group_ids: list[UUID] = []
    for raw in mentioned_agent_ids:
        token = str(raw or "").strip()
        if not token:
            continue
        kind, _, value = token.partition(":")
        if value and kind == "person":
            try:
                person_ids.append(coerce_uuid(value))
            except ValueError:
                continue
        elif value and kind == "group":
            try:
                group_ids.append(coerce_uuid(value))
            except ValueError:
                continue
        else:
            try:
                person_ids.append(coerce_uuid(token))
            except ValueError:
                continue

    if group_ids:
        rows = (
            db.query(SystemUser.id)
            .select_from(ServiceTeamMember)
            .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
            .join(
                SystemUser,
                SystemUser.person_party_id == ServiceTeamMember.person_id,
            )
            .filter(ServiceTeam.is_active.is_(True))
            .filter(ServiceTeamMember.is_active.is_(True))
            .filter(ServiceTeamMember.team_id.in_(group_ids))
            .filter(SystemUser.is_active.is_(True))
            .all()
        )
        person_ids.extend(row[0] for row in rows if row[0])

    active = (
        db.query(SystemUser.id)
        .filter(SystemUser.is_active.is_(True))
        .filter(SystemUser.id.in_(person_ids))
        .all()
        if person_ids
        else []
    )
    deduped: list[str] = []
    seen: set[str] = set()
    for row in active:
        person_id = str(row[0])
        if person_id in seen:
            continue
        seen.add(person_id)
        deduped.append(person_id)
    return deduped


def notify_ticket_comment_mentions(
    db: Session,
    *,
    ticket_id: str,
    ticket_number: str | None,
    ticket_title: str | None,
    comment_preview: str | None,
    mentioned_agent_ids: list[str] | None,
    actor_person_id: str | None,
    source_event_id: UUID | None = None,
) -> None:
    """Queue staff notifications for explicit ticket comment mentions."""
    recipient_ids = resolve_mentioned_person_ids(db, mentioned_agent_ids)
    if actor_person_id:
        recipient_ids = [pid for pid in recipient_ids if pid != str(actor_person_id)]
    if not recipient_ids:
        return

    message = render_ticket_mention_message(
        TicketMentionMessageInput(
            ticket_id=coerce_uuid(ticket_id),
            ticket_number=ticket_number,
            ticket_title=ticket_title,
            comment_preview=comment_preview,
            public_base_url=get_brand()["app_url"],
        )
    )

    users = (
        db.query(SystemUser)
        .filter(SystemUser.is_active.is_(True))
        .filter(SystemUser.id.in_([coerce_uuid(pid) for pid in recipient_ids]))
        .all()
    )
    for user in users:
        queue_staff_push(
            db,
            recipient=str(user.id),
            subject=message.subject,
            body=message.body,
        )
        if user.email:
            queue_staff_email(
                db,
                recipient=user.email,
                subject=message.subject,
                body=message.body,
            )
        if source_event_id is not None:
            from app.services.nextcloud_talk_staff import (
                StaffTalkEventType,
                StageStaffTalkNotification,
                stage_staff_talk_notification,
            )

            talk_command = StageStaffTalkNotification(
                system_user_id=user.id,
                source_event_id=source_event_id,
                event_type=StaffTalkEventType.ticket_comment_mention,
                subject=message.subject,
                body=message.body,
                target_url=(
                    message.target_url or f"/admin/support/tickets/{ticket_id}"
                ),
                source_entity_type="support_ticket_comment",
                source_entity_id=source_event_id,
            )
            try:
                execute_owner_savepoint(
                    db,
                    partial(stage_staff_talk_notification, db, talk_command),
                )
            except Exception:  # noqa: BLE001 - comment remains authoritative
                logger.exception(
                    "ticket_comment_talk_staging_failed comment_id=%s user_id=%s",
                    source_event_id,
                    user.id,
                )
