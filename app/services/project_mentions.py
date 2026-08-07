"""Project and project-task comment @mention notifications."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.system_user import SystemUser
from app.services.common import coerce_uuid
from app.services.staff_notifications import queue_staff_email, queue_staff_push
from app.services.ticket_mentions import (
    list_ticket_mention_users,
    resolve_mentioned_person_ids,
)


def list_project_mention_users(
    db: Session, *, limit: int = 200
) -> list[dict[str, str]]:
    return list_ticket_mention_users(db, limit=limit)


def notify_project_comment_mentions(
    db: Session,
    *,
    target_kind: str,
    target_ref: str,
    target_title: str | None,
    comment_preview: str | None,
    mentioned_agent_ids: list[str] | None,
    actor_person_id: str | None,
    source_event_id: UUID,
) -> None:
    recipient_ids = resolve_mentioned_person_ids(db, mentioned_agent_ids)
    if actor_person_id:
        recipient_ids = [pid for pid in recipient_ids if pid != str(actor_person_id)]
    if not recipient_ids:
        return
    is_task = target_kind == "project_task"
    label = "task" if is_task else "project"
    target_url = (
        f"/admin/projects/tasks/{target_ref}"
        if is_task
        else f"/admin/projects/{target_ref}"
    )
    subject = f"Mentioned in {label} {target_ref}"
    if target_title:
        subject = f"{subject}: {target_title}"[:200]
    body = "\n".join(
        [
            f"You were mentioned in a {label} comment.",
            f"{label.title()}: {target_ref}",
            f"Open: {target_url}",
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
        queue_staff_push(db, recipient=str(user.id), subject=subject, body=body)
        if user.email:
            queue_staff_email(db, recipient=user.email, subject=subject, body=body)
        from app.services.nextcloud_talk_staff import (
            StaffTalkEventType,
            StageStaffTalkNotification,
            stage_staff_talk_notification,
        )

        stage_staff_talk_notification(
            db,
            StageStaffTalkNotification(
                system_user_id=user.id,
                source_event_id=source_event_id,
                event_type=(
                    StaffTalkEventType.project_task_comment_mention
                    if is_task
                    else StaffTalkEventType.project_comment_mention
                ),
                subject=subject,
                body=body,
                target_url=target_url,
                source_entity_type=(
                    "project_task_comment" if is_task else "project_comment"
                ),
                source_entity_id=source_event_id,
            ),
        )
