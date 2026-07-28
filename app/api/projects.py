"""Native projects API ported from CRM ``api/projects.py``.

Same paths and payloads as the CRM router; sub conventions applied:

* Permission guards per §2.4: ``project:{create,read,update,delete}`` +
  ``project:task:{read,write}`` (already seeded in sub RBAC).
* The `filters` JSON param goes through sub's whitelisted dynamic-filter
  engine (`project_filters`) instead of CRM's `filter_engine`.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.project import (
    ProjectCostSummary,
    ProjectCreate,
    ProjectRead,
    ProjectTaskCreate,
    ProjectTaskRead,
    ProjectTaskUpdate,
    ProjectUpdate,
)
from app.services import project_costs, project_filters
from app.services import projects as projects_service
from app.services.auth_dependencies import require_permission
from app.services.domain_errors import DomainError
from app.services.dynamic_filters import FilterValidationError
from app.services.web_projects import (
    ProjectListProjectionQuery,
    query_project_list_projection,
)


def _project_error_status(exc: DomainError) -> int:
    if exc.code.endswith(".not_found"):
        return status.HTTP_404_NOT_FOUND
    if exc.code.endswith(".unauthorized"):
        return status.HTTP_403_FORBIDDEN
    if exc.code.endswith(
        (".stale_state", ".relationship_conflict", ".idempotency_conflict")
    ):
        return status.HTTP_409_CONFLICT
    return status.HTTP_400_BAD_REQUEST


class ProjectDomainRoute(APIRoute):
    """Map transport-neutral Projects errors at the API adapter boundary."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except DomainError as exc:
                raise HTTPException(
                    status_code=_project_error_status(exc), detail=exc.message
                ) from exc

        return handler


router = APIRouter(route_class=ProjectDomainRoute)


@router.post(
    "/projects",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
    dependencies=[Depends(require_permission("project:create"))],
)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    return projects_service.projects.create(db, payload)


@router.get(
    "/projects",
    response_model=ListResponse[ProjectRead],
    tags=["projects"],
    dependencies=[Depends(require_permission("project:read"))],
)
def list_projects(
    subscriber_id: str | None = None,
    status: str | None = None,
    project_type: str | None = None,
    priority: str | None = None,
    owner_person_id: str | None = None,
    manager_person_id: str | None = None,
    project_manager_person_id: str | None = None,
    assistant_manager_person_id: str | None = None,
    is_active: bool | None = None,
    filters: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return query_project_list_projection(
        db,
        ProjectListProjectionQuery(
            subscriber_id=subscriber_id,
            status=status,
            project_type=project_type,
            priority=priority,
            owner_person_id=owner_person_id,
            manager_person_id=manager_person_id,
            project_manager_person_id=project_manager_person_id,
            assistant_manager_person_id=assistant_manager_person_id,
            is_active=is_active,
            filters=filters,
            order_by=order_by,
            order_dir=order_dir,
            limit=limit,
            offset=offset,
        ),
    )


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectRead,
    tags=["projects"],
    dependencies=[Depends(require_permission("project:update"))],
)
def update_project(
    project_id: str, payload: ProjectUpdate, db: Session = Depends(get_db)
):
    return projects_service.projects.update(db, project_id, payload)


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["projects"],
    dependencies=[Depends(require_permission("project:delete"))],
)
def delete_project(project_id: str, db: Session = Depends(get_db)):
    projects_service.projects.delete(db, project_id)


@router.get(
    "/projects/charts/summary",
    tags=["projects"],
    dependencies=[Depends(require_permission("project:read"))],
)
def projects_chart_summary(db: Session = Depends(get_db)):
    return projects_service.projects.chart_summary(db)


@router.get(
    "/projects/kanban",
    tags=["projects"],
    dependencies=[Depends(require_permission("project:read"))],
)
def projects_kanban(db: Session = Depends(get_db)):
    return projects_service.projects.kanban_view(db)


@router.get(
    "/projects/gantt",
    tags=["projects"],
    dependencies=[Depends(require_permission("project:read"))],
)
def projects_gantt(db: Session = Depends(get_db)):
    return projects_service.projects.gantt_view(db)


class ProjectGanttUpdate(BaseModel):
    id: str
    field: str
    value: str


@router.post(
    "/projects/gantt/due-date",
    tags=["projects"],
    dependencies=[Depends(require_permission("project:update"))],
)
def projects_gantt_due_date(payload: ProjectGanttUpdate, db: Session = Depends(get_db)):
    return projects_service.projects.update_gantt_date(
        db, payload.id, payload.field, payload.value
    )


class ProjectKanbanMove(BaseModel):
    id: str
    from_: str | None = Field(default=None, alias="from")
    to: str
    position: int | None = None

    model_config = ConfigDict(populate_by_name=True)


@router.post(
    "/projects/kanban/move",
    tags=["projects"],
    dependencies=[Depends(require_permission("project:update"))],
)
def projects_kanban_move(payload: ProjectKanbanMove, db: Session = Depends(get_db)):
    return projects_service.projects.update_status(db, payload.id, payload.to)


@router.get(
    "/projects/{project_id}/cost-summary",
    response_model=ProjectCostSummary,
    tags=["projects"],
    dependencies=[Depends(require_permission("project:read"))],
)
def get_project_cost_summary(project_id: str, db: Session = Depends(get_db)):
    return project_costs.project_cost_summary(db, project_id)


@router.get(
    "/projects/{project_id}",
    response_model=ProjectRead,
    tags=["projects"],
    dependencies=[Depends(require_permission("project:read"))],
)
def get_project(project_id: str, db: Session = Depends(get_db)):
    return projects_service.projects.get(db, project_id)


@router.post(
    "/project-tasks",
    response_model=ProjectTaskRead,
    status_code=status.HTTP_201_CREATED,
    tags=["project-tasks"],
    dependencies=[Depends(require_permission("project:task:write"))],
)
def create_project_task(payload: ProjectTaskCreate, db: Session = Depends(get_db)):
    return projects_service.project_tasks.create(db, payload)


@router.get(
    "/project-tasks/{task_id}",
    response_model=ProjectTaskRead,
    tags=["project-tasks"],
    dependencies=[Depends(require_permission("project:task:read"))],
)
def get_project_task(task_id: str, db: Session = Depends(get_db)):
    return projects_service.project_tasks.get(db, task_id)


@router.get(
    "/project-tasks",
    response_model=ListResponse[ProjectTaskRead],
    tags=["project-tasks"],
    dependencies=[Depends(require_permission("project:task:read"))],
)
def list_project_tasks(
    project_id: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    assigned_to_person_id: str | None = None,
    parent_task_id: str | None = None,
    is_active: bool | None = None,
    filters: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    try:
        filter_clause = project_filters.build_project_task_filter_clause(filters)
    except FilterValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    args = (
        project_id,
        status,
        priority,
        assigned_to_person_id,
        parent_task_id,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )
    if filter_clause is None:
        return projects_service.project_tasks.list_response(db, *args)
    return projects_service.project_tasks.list_response(
        db, *args, filter_clause=filter_clause
    )


@router.patch(
    "/project-tasks/{task_id}",
    response_model=ProjectTaskRead,
    tags=["project-tasks"],
    dependencies=[Depends(require_permission("project:task:write"))],
)
def update_project_task(
    task_id: str, payload: ProjectTaskUpdate, db: Session = Depends(get_db)
):
    return projects_service.project_tasks.update(db, task_id, payload)


@router.delete(
    "/project-tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["project-tasks"],
    dependencies=[Depends(require_permission("project:task:write"))],
)
def delete_project_task(task_id: str, db: Session = Depends(get_db)):
    projects_service.project_tasks.delete(db, task_id)
