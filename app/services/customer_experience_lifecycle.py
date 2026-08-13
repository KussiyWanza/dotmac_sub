"""Typed, read-only projection of the native customer work lifecycle.

This owner composes Project -> ProjectTask -> WorkOrder -> Ticket links for
customer and field presentation. It never writes domain state: projects,
work-order commands/field transitions, and support retain their own decisions.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.models.project import Project, ProjectStatus, ProjectTask, ProjectTaskStatus
from app.models.provisioning import ProvisioningReadinessDecision, ServiceOrder
from app.models.support import Ticket, TicketStatus
from app.models.work_order import WorkOrder
from app.schemas.portal import (
    CustomerActionKey,
    CustomerExperienceState,
    CustomerProvisioningCheck,
    CustomerProvisioningDecisionState,
    CustomerProvisioningReference,
    CustomerSelfCareAction,
    CustomerTicketReference,
    CustomerWorkOrderReference,
    MyProjectsResponse,
    MyWorkOrdersResponse,
    ProjectItem,
    ProjectStage,
    ProjectStageState,
    WorkOrderItem,
)
from app.services.common import coerce_uuid
from app.services.field.work_order_status import WORK_ORDER_TERMINAL_VALUES
from app.services.projects import (
    FIBER_INSTALLATION_STAGE_ORDER,
    FIBER_INSTALLATION_STAGE_TITLES,
)
from app.services.status_presentation import (
    project_status_presentation,
    project_task_status_presentation,
    ticket_status_presentation,
    work_order_status_presentation,
)

_PROJECT_INACTIVE = frozenset(
    {ProjectStatus.completed.value, ProjectStatus.canceled.value}
)
_TASK_DONE = frozenset({ProjectTaskStatus.done.value, ProjectTaskStatus.canceled.value})


def ticket_actions(ticket: Any) -> list[CustomerSelfCareAction]:
    actions = [
        CustomerSelfCareAction(
            key=CustomerActionKey.view_ticket,
            label="View support ticket",
            api_path=f"/me/support/tickets/{ticket.id}",
        )
    ]
    if ticket.status == TicketStatus.pending_confirmation.value:
        actions.extend(
            [
                CustomerSelfCareAction(
                    key=CustomerActionKey.confirm_resolution,
                    label="Confirm resolution",
                    method="POST",
                    api_path=f"/me/support/tickets/{ticket.id}/confirm-resolution",
                ),
                CustomerSelfCareAction(
                    key=CustomerActionKey.dispute_resolution,
                    label="Report issue persists",
                    method="POST",
                    api_path=f"/me/support/tickets/{ticket.id}/dispute-resolution",
                ),
            ]
        )
    if ticket.status == TicketStatus.closed.value and ticket.csat_rating is None:
        actions.append(
            CustomerSelfCareAction(
                key=CustomerActionKey.rate_support,
                label="Rate support",
                method="POST",
                api_path=f"/me/support/tickets/{ticket.id}/rate",
            )
        )
    return actions


def ticket_reference(ticket: Ticket) -> CustomerTicketReference:
    return CustomerTicketReference(
        id=ticket.id,
        number=ticket.number,
        title=ticket.title,
        status=ticket.status,
        status_presentation=ticket_status_presentation(ticket.status),
        resolved_at=ticket.resolved_at,
        closed_at=ticket.closed_at,
        actions=ticket_actions(ticket),
    )


#: A visit whose technician may already be travelling. ``dispatched`` is the
#: status an ``en_route`` departure sets, and the chat is opened by that same
#: departure; the later statuses keep it reachable until the visit ends.
_CHATTABLE_STATUSES = frozenset({"dispatched", "in_progress", "paused"})


def _technician_rating(row: WorkOrder) -> int | None:
    return row.technician_rating


def _has_job_chat(row: WorkOrder) -> bool:
    """Whether this visit has a chat to open.

    The chat starts at departure rather than at ``in_progress``, because a
    wrong address is worth correcting while the technician is still driving.
    Tracking stays narrower — the map only means something once the visit has
    actually started.
    """
    return row.status in _CHATTABLE_STATUSES


def _work_order_actions(row: WorkOrder) -> list[CustomerSelfCareAction]:
    actions = [
        CustomerSelfCareAction(
            key=CustomerActionKey.view_work_order,
            label="View technician visit",
            api_path=f"/me/work-orders/{row.public_id}",
        )
    ]
    if row.status == "in_progress":
        actions.append(
            CustomerSelfCareAction(
                key=CustomerActionKey.track_technician,
                label="Track technician",
                api_path=f"/me/work-orders/{row.public_id}/technician-location",
            )
        )
    if _has_job_chat(row):
        actions.append(
            CustomerSelfCareAction(
                key=CustomerActionKey.chat_technician,
                label="Message technician",
                api_path=f"/me/work-orders/{row.public_id}/chat",
            )
        )
    if row.status == "completed" and _technician_rating(row) is None:
        actions.append(
            CustomerSelfCareAction(
                key=CustomerActionKey.rate_technician,
                label="Rate technician",
                method="POST",
                api_path=f"/me/work-orders/{row.public_id}/rate-technician",
            )
        )
    return actions


def work_order_reference(row: WorkOrder) -> CustomerWorkOrderReference:
    return CustomerWorkOrderReference(
        id=row.id,
        public_id=row.public_id,
        project_id=row.project_id,
        project_task_id=row.project_task_id,
        origin_ticket_id=row.origin_ticket_id,
        title=row.title,
        status=row.status,
        status_presentation=work_order_status_presentation(row.status),
        scheduled_start=row.scheduled_start,
        scheduled_end=row.scheduled_end,
        estimated_arrival_at=row.estimated_arrival_at,
        completed_at=row.completed_at,
        technician_name=row.technician_name or row.assigned_to_name,
        technician_phone=row.technician_phone,
        technician_rating=_technician_rating(row),
        actions=_work_order_actions(row),
    )


def project_task_stage_state(status: str) -> ProjectStageState:
    """Map authoritative task status to the shared typed stage projection."""
    return {
        ProjectTaskStatus.done.value: ProjectStageState.done,
        ProjectTaskStatus.canceled.value: ProjectStageState.canceled,
        ProjectTaskStatus.blocked.value: ProjectStageState.blocked,
        ProjectTaskStatus.in_progress.value: ProjectStageState.in_progress,
    }.get(status, ProjectStageState.pending)


def _experience_state(
    project: Project,
    work_orders: list[WorkOrder],
    tickets: list[Ticket],
) -> CustomerExperienceState:
    if project.status == ProjectStatus.canceled.value:
        return CustomerExperienceState.canceled
    if any(
        ticket.status == TicketStatus.pending_confirmation.value for ticket in tickets
    ):
        return CustomerExperienceState.waiting_on_customer
    if project.status == ProjectStatus.completed.value:
        return CustomerExperienceState.resolved
    if project.status == ProjectStatus.on_hold.value:
        return CustomerExperienceState.on_hold
    if any(row.status not in WORK_ORDER_TERMINAL_VALUES for row in work_orders):
        return CustomerExperienceState.field_work
    if project.status in {ProjectStatus.open.value, ProjectStatus.planned.value}:
        return CustomerExperienceState.planned
    return CustomerExperienceState.in_progress


def _project_actions(project: Project) -> list[CustomerSelfCareAction]:
    return [
        CustomerSelfCareAction(
            key=CustomerActionKey.view_project,
            label="View installation",
            api_path=f"/me/projects/{project.id}",
        ),
        CustomerSelfCareAction(
            key=CustomerActionKey.contact_support,
            label="Contact support",
            api_path=f"/me/chat/session?project_id={project.id}",
        ),
    ]


def _project_item(
    project: Project,
    *,
    work_orders: list[WorkOrder],
    service_orders: list[ServiceOrder],
    tickets_by_id: dict[UUID, Ticket],
) -> ProjectItem:
    task_work_orders: dict[UUID, list[WorkOrder]] = defaultdict(list)
    for row in work_orders:
        if row.project_task_id is not None:
            task_work_orders[row.project_task_id].append(row)

    tasks = sorted(
        (task for task in project.tasks if task.is_active),
        key=lambda task: (task.created_at, task.id),
    )
    tasks_by_stage = {
        str((task.metadata_ or {}).get("fiber_stage_key")): task
        for task in tasks
        if (task.metadata_ or {}).get("fiber_stage_key")
    }
    stage_rows: list[tuple[str | None, str, ProjectTask | None]]
    if project.project_type == "fiber_optics_installation":
        stage_rows = [
            (
                key,
                FIBER_INSTALLATION_STAGE_TITLES[key],
                tasks_by_stage.get(key),
            )
            for key in FIBER_INSTALLATION_STAGE_ORDER
        ]
    else:
        stage_rows = [(None, task.title, task) for task in tasks]

    stages: list[ProjectStage] = []
    for key, title, task in stage_rows:
        task_status = task.status if task is not None else ProjectTaskStatus.todo.value
        ticket = (
            tickets_by_id.get(task.ticket_id)
            if task is not None and task.ticket_id is not None
            else None
        )
        linked_work_orders = task_work_orders.get(task.id, []) if task else []
        stages.append(
            ProjectStage(
                task_id=task.id if task else None,
                key=key,
                title=title,
                status=project_task_stage_state(task_status),
                status_presentation=project_task_status_presentation(task_status),
                completed_at=task.completed_at if task else None,
                ticket=ticket_reference(ticket) if ticket else None,
                work_orders=[work_order_reference(row) for row in linked_work_orders],
            )
        )

    done = sum(
        stage.status in {ProjectStageState.done, ProjectStageState.canceled}
        for stage in stages
    )
    completed = project.status == ProjectStatus.completed.value
    progress_pct = (
        100 if completed else round(done / len(stages) * 100) if stages else 0
    )
    current_stage = next(
        (
            stage.title
            for stage in stages
            if stage.status not in {ProjectStageState.done, ProjectStageState.canceled}
        ),
        None,
    )
    ticket_ids = {task.ticket_id for task in tasks if task.ticket_id is not None} | {
        row.origin_ticket_id for row in work_orders if row.origin_ticket_id is not None
    }
    related_tickets = [
        tickets_by_id[ticket_id]
        for ticket_id in ticket_ids
        if ticket_id in tickets_by_id
    ]
    unscoped_work_orders = [row for row in work_orders if row.project_task_id is None]
    provisioning = []
    for order in service_orders:
        latest = max(
            order.readiness_decisions,
            key=lambda item: (item.decided_at, item.created_at),
            default=None,
        )
        provisioning.append(
            CustomerProvisioningReference(
                service_order_id=order.id,
                subscription_id=order.subscription_id,
                activation_task_id=order.activation_project_task_id,
                order_status=order.status.value,
                decision=(
                    CustomerProvisioningDecisionState(latest.status.value)
                    if latest is not None
                    else CustomerProvisioningDecisionState.not_evaluated
                ),
                reason_code=latest.reason_code if latest else None,
                checks=(
                    [
                        CustomerProvisioningCheck(
                            kind=check.kind.value,
                            result=check.result.value,
                            reason_code=check.reason_code,
                        )
                        for check in latest.checks
                    ]
                    if latest
                    else []
                ),
                decided_at=latest.decided_at if latest else None,
            )
        )
    return ProjectItem(
        id=project.id,
        name=project.name,
        status=project.status,
        status_presentation=project_status_presentation(project.status),
        experience_state=_experience_state(project, work_orders, related_tickets),
        project_type=project.project_type,
        progress_pct=progress_pct,
        current_stage=None if completed else current_stage,
        stages=stages,
        work_orders=[work_order_reference(row) for row in unscoped_work_orders],
        related_tickets=[ticket_reference(ticket) for ticket in related_tickets],
        provisioning=provisioning,
        actions=_project_actions(project),
        customer_address=project.customer_address,
        region=project.region,
        start_at=project.start_at,
        due_at=project.due_at,
        completed_at=project.completed_at,
        created_at=project.created_at,
    )


def projects_for_subscriber(db: Session, subscriber_id: str) -> MyProjectsResponse:
    subscriber_uuid = coerce_uuid(subscriber_id)
    projects = (
        db.query(Project)
        .options(selectinload(Project.tasks))
        .filter(Project.subscriber_id == subscriber_uuid)
        .filter(Project.is_active.is_(True))
        .order_by(Project.created_at.desc())
        .all()
    )
    project_ids = [project.id for project in projects]
    work_orders = (
        db.query(WorkOrder)
        .filter(WorkOrder.project_id.in_(project_ids))
        .filter(WorkOrder.is_active.is_(True))
        .order_by(WorkOrder.created_at.asc(), WorkOrder.id.asc())
        .all()
        if project_ids
        else []
    )
    work_orders_by_project: dict[UUID, list[WorkOrder]] = defaultdict(list)
    for row in work_orders:
        if row.project_id is not None:
            work_orders_by_project[row.project_id].append(row)
    service_orders = (
        db.query(ServiceOrder)
        .options(
            selectinload(ServiceOrder.readiness_decisions).selectinload(
                ProvisioningReadinessDecision.checks
            )
        )
        .filter(ServiceOrder.project_id.in_(project_ids))
        .order_by(ServiceOrder.created_at.asc())
        .all()
        if project_ids
        else []
    )
    service_orders_by_project: dict[UUID, list[ServiceOrder]] = defaultdict(list)
    for order in service_orders:
        if order.project_id is not None:
            service_orders_by_project[order.project_id].append(order)
    ticket_ids = {
        task.ticket_id
        for project in projects
        for task in project.tasks
        if task.ticket_id is not None
    } | {
        row.origin_ticket_id for row in work_orders if row.origin_ticket_id is not None
    }
    tickets = (
        db.query(Ticket).filter(Ticket.id.in_(ticket_ids)).all() if ticket_ids else []
    )
    tickets_by_id = {ticket.id: ticket for ticket in tickets}
    items = [
        _project_item(
            project,
            work_orders=work_orders_by_project.get(project.id, []),
            service_orders=service_orders_by_project.get(project.id, []),
            tickets_by_id=tickets_by_id,
        )
        for project in projects
    ]
    return MyProjectsResponse(
        projects=items,
        total=len(items),
        active=sum(item.status not in _PROJECT_INACTIVE for item in items),
    )


def project_for_subscriber(
    db: Session, subscriber_id: str, project_id: UUID
) -> ProjectItem | None:
    response = projects_for_subscriber(db, subscriber_id)
    return next((item for item in response.projects if item.id == project_id), None)


def work_orders_for_subscriber(db: Session, subscriber_id: str) -> MyWorkOrdersResponse:
    subscriber_uuid = coerce_uuid(subscriber_id)
    rows = (
        db.query(WorkOrder)
        .options(
            selectinload(WorkOrder.project),
            selectinload(WorkOrder.project_task),
            selectinload(WorkOrder.origin_ticket),
        )
        .filter(WorkOrder.subscriber_id == subscriber_uuid)
        .filter(WorkOrder.is_active.is_(True))
        .order_by(WorkOrder.created_at.desc())
        .all()
    )
    items = [
        WorkOrderItem(
            **work_order_reference(row).model_dump(),
            work_type=row.work_type,
            priority=row.priority,
            address=row.address,
            estimated_duration_minutes=row.estimated_duration_minutes,
            started_at=row.started_at,
            paused_at=row.paused_at,
            resumed_at=row.resumed_at,
            total_active_seconds=row.total_active_seconds,
            created_at=row.created_at,
            project_name=row.project.name if row.project else None,
            project_task_title=row.project_task.title if row.project_task else None,
            origin_ticket=ticket_reference(row.origin_ticket)
            if row.origin_ticket
            else None,
        )
        for row in rows
    ]
    return MyWorkOrdersResponse(
        work_orders=items,
        total=len(items),
        upcoming=sum(
            row.status not in {*WORK_ORDER_TERMINAL_VALUES, "draft"} for row in rows
        ),
    )


def work_order_for_subscriber(
    db: Session, subscriber_id: str, public_id: str
) -> WorkOrderItem | None:
    response = work_orders_for_subscriber(db, subscriber_id)
    return next(
        (item for item in response.work_orders if item.public_id == public_id), None
    )
