"""Project provider for Service Delivery SLA visibility."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.project import Project, ProjectPriority, ProjectStatus
from app.models.ticket_workflow import SlaClock, SlaClockStatus, WorkflowEntityType
from app.services.workqueue.providers import register
from app.services.workqueue.providers.common import (
    as_utc,
    legacy_priority,
    score_item,
    seconds_until,
)
from app.services.workqueue.scope import WorkqueueScope
from app.services.workqueue.scoring_config import WorkqueueScoringConfig
from app.services.workqueue.types import ActionKind, ItemKind, WorkqueueItem

CLOSED_PROJECT_STATUSES = (
    ProjectStatus.completed.value,
    ProjectStatus.canceled.value,
)


class ProjectProvider:
    kind = ItemKind.project

    def fetch(
        self,
        db: Session,
        *,
        scope: WorkqueueScope,
        config: WorkqueueScoringConfig,
        snoozed_ids: set[UUID],
        now: datetime,
        limit: int,
    ) -> list[WorkqueueItem]:
        query = (
            db.query(Project)
            .filter(Project.is_active.is_(True))
            .filter(Project.status.notin_(CLOSED_PROJECT_STATUSES))
        )
        if not scope.is_org_wide:
            team_ids = scope.team_ids_for_query()
            visibility = [
                Project.project_manager_person_id == scope.person_id,
                Project.manager_person_id == scope.person_id,
                Project.assistant_manager_person_id == scope.person_id,
            ]
            if team_ids:
                visibility.append(Project.service_team_id.in_(team_ids))
            query = query.filter(or_(*visibility))
        elif scope.service_team_filter is not None:
            query = query.filter(Project.service_team_id == scope.service_team_filter)

        if snoozed_ids:
            query = query.filter(Project.id.notin_(snoozed_ids))

        rows = (
            query.order_by(Project.due_at.asc().nullslast(), Project.updated_at.desc())
            .limit(limit)
            .all()
        )
        if not rows:
            return []

        sla_due = _sla_due_by_project(db, [project.id for project in rows])
        return [self._to_item(project, sla_due, config, now, scope) for project in rows]

    def _to_item(
        self,
        project: Project,
        sla_due: dict[UUID, tuple[datetime | None, bool]],
        config: WorkqueueScoringConfig,
        now: datetime,
        scope: WorkqueueScope,
    ) -> WorkqueueItem:
        clock_due, breached = sla_due.get(project.id, (None, False))
        due_at = clock_due or as_utc(project.due_at)
        candidates: list[tuple[int, str]] = [(30, "in_queue")]
        if breached:
            candidates.append((config.project_sla.breach_score, "sla_breach"))
        remaining = seconds_until(due_at, now)
        if remaining is not None:
            band = config.project_sla.band(remaining)
            if band is not None:
                reason, score = band
                candidates.append((score, reason))

        priority = str(project.priority or "").lower()
        if priority == ProjectPriority.urgent.value:
            candidates.append((80, "priority_urgent"))
        elif priority == ProjectPriority.high.value:
            candidates.append((60, "priority_high"))

        score, reason, urgency = score_item(candidates, config)
        last_activity = as_utc(project.updated_at) or as_utc(project.created_at)

        return WorkqueueItem(
            item_kind=ItemKind.project,
            item_id=project.id,
            title=project.name,
            subtitle=project.number,
            status=project.status,
            priority=legacy_priority(project.priority),
            score=score,
            reason=reason,
            urgency=urgency,
            happened_at=last_activity or now,
            due_at=due_at,
            last_activity_at=last_activity,
            subscriber_id=project.subscriber_id,
            service_team_id=project.service_team_id,
            assigned_person_id=(
                project.project_manager_person_id or project.manager_person_id
            ),
            url=f"/admin/projects/{project.id}",
            actions=(ActionKind.open, ActionKind.snooze),
            metadata={
                "project_type": project.project_type,
                "audience": scope.audience.value,
                "sla_due_at": due_at.isoformat() if due_at else None,
            },
        )


def _sla_due_by_project(
    db: Session, project_ids: list[UUID]
) -> dict[UUID, tuple[datetime | None, bool]]:
    if not project_ids:
        return {}
    rows = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.project.value)
        .filter(SlaClock.entity_id.in_(project_ids))
        .filter(
            SlaClock.status.in_(
                (SlaClockStatus.running.value, SlaClockStatus.breached.value)
            )
        )
        .order_by(SlaClock.entity_id.asc(), SlaClock.due_at.asc())
        .all()
    )
    due: dict[UUID, tuple[datetime | None, bool]] = {}
    for clock in rows:
        clock_due = as_utc(clock.due_at)
        breached = (
            clock.status == SlaClockStatus.breached.value
            or clock.breached_at is not None
        )
        current = due.get(clock.entity_id)
        if current is None:
            due[clock.entity_id] = (clock_due, breached)
            continue
        existing_due, existing_breached = current
        if existing_due is None:
            tighter = clock_due
        elif clock_due is None:
            tighter = existing_due
        else:
            tighter = min(existing_due, clock_due)
        due[clock.entity_id] = (tighter, existing_breached or breached)
    return due


register(ProjectProvider())
