"""Ticket comment @mention notifications for staff users."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.services.common import coerce_uuid
from app.services.staff_notifications import queue_staff_email, queue_staff_push

logger = logging.getLogger(__name__)

_TICKET_MENTION_USERS_TTL_SECONDS = 30.0
_TICKET_MENTION_USERS_CACHE: tuple[datetime, list[dict[str, str]]] | None = None


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
) -> None:
    """Queue staff notifications for explicit ticket comment mentions."""
    recipient_ids = resolve_mentioned_person_ids(db, mentioned_agent_ids)
    if actor_person_id:
        recipient_ids = [pid for pid in recipient_ids if pid != str(actor_person_id)]
    if not recipient_ids:
        return

    ref = ticket_number or ticket_id
    subject = f"Mentioned in ticket {ref}"
    if ticket_title:
        subject = f"{subject}: {ticket_title}"[:200]
    body = "\n".join(
        [
            "You were mentioned in a support ticket comment.",
            f"Ticket: {ref}",
            f"Open: /admin/support/tickets/{ticket_id}",
            f"Comment: {comment_preview or ''}",
        ]
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
            subject=subject,
            body=body,
        )
        if user.email:
            queue_staff_email(
                db,
                recipient=user.email,
                subject=subject,
                body=body,
            )
