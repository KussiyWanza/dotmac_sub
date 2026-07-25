"""Web helpers for native admin project routes.

Context builders + form handlers for the admin projects UI, ported from CRM's
``app/web/admin/projects.py`` onto sub's conventions: thin routes call into
this module (the ``web_support_tickets`` idiom), all persistence goes through
the merged native managers in ``app.services.projects``, list filters ride the
whitelisted dynamic-filter engine (``project_filters``), and staff/assignee
options come from the SystemUser staff directory.

Not ported from CRM (documented deviations):
* vendor ``installation_projects`` auto-create — the vendor wrapper is separate.
* ERP expense totals / material requests cards — ERP reads are not in sub.
* per-person saved filter preferences — sub keeps filter state in the URL.
* file attachments on projects/tasks/comments — comments are text-only here.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.project import (
    Project,
    ProjectComment,
    ProjectPriority,
    ProjectStatus,
    ProjectTask,
    ProjectTaskDependency,
    ProjectTaskDependencyType,
    ProjectTaskPriority,
    ProjectTaskStatus,
    ProjectTemplateTask,
    ProjectTemplateTaskDependency,
    ProjectType,
)
from app.models.ticket_workflow import SlaClock, SlaClockStatus, WorkflowEntityType
from app.schemas.project import (
    ProjectCommentCreate,
    ProjectCommentUpdate,
    ProjectCreate,
    ProjectTaskCommentCreate,
    ProjectTaskCreate,
    ProjectTaskUpdate,
    ProjectTemplateCreate,
    ProjectTemplateTaskCreate,
    ProjectTemplateTaskUpdate,
    ProjectTemplateUpdate,
    ProjectUpdate,
)
from app.services import (
    customer_experience_lifecycle,
    project_filters,
    work_order_views,
)
from app.services import projects as projects_service
from app.services import support as support_service
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services.audit_helpers import build_audit_activities, log_audit_event
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.dynamic_filters import FilterValidationError
from app.services.list_query import ListDefinition, ListFieldDefinition, ListQuery
from app.services.ui_contracts import Action

logger = logging.getLogger(__name__)


class ProjectProjectionError(DomainError):
    """Transport-neutral project query/projection error."""


def _projection_error(code: str, message: str) -> ProjectProjectionError:
    return ProjectProjectionError(
        code=f"ui.project_list_projection.{code}", message=message
    )


PROJECT_EXPORT_LIMIT = 10000

DEFAULT_PROJECT_EXPORT_COLUMNS = (
    "project",
    "customer",
    "status",
    "priority",
    "created",
)
REQUIRED_PROJECT_EXPORT_COLUMNS = ("project", "created")
PROJECT_EXPORT_COLUMNS = [
    {"key": "project", "label": "Project"},
    {"key": "customer", "label": "Customer"},
    {"key": "status", "label": "Status"},
    {"key": "priority", "label": "Priority"},
    {"key": "created", "label": "Created"},
    {"key": "code", "label": "Code"},
    {"key": "type", "label": "Project Type"},
    {"key": "region", "label": "Region"},
    {"key": "owner", "label": "Owner"},
    {"key": "manager", "label": "Manager"},
    {"key": "project_manager", "label": "Project Manager"},
    {"key": "start_at", "label": "Start Date"},
    {"key": "due_at", "label": "Due Date"},
]

_LIST_ORDER_COLUMNS = {"created_at", "name", "priority"}

PROJECT_LIST_DEFINITION = ListDefinition(
    key="projects",
    fields=(
        ListFieldDefinition("name", "Project", searchable=True, sortable=True),
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("project_type", "Type", filterable=True),
        ListFieldDefinition("priority", "Priority", filterable=True, sortable=True),
        ListFieldDefinition("region", "Region", filterable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)


@dataclass(frozen=True, slots=True)
class ProjectListProjectionQuery:
    subscriber_id: str | None = None
    status: str | None = None
    project_type: str | None = None
    priority: str | None = None
    owner_person_id: str | None = None
    manager_person_id: str | None = None
    project_manager_person_id: str | None = None
    assistant_manager_person_id: str | None = None
    is_active: bool | None = None
    filters: str | None = None
    search: str | None = None
    region: str | None = None
    order_by: str = "created_at"
    order_dir: str = "desc"
    limit: int = 50
    offset: int = 0
    permission_scope: str = "project:read"


@dataclass(frozen=True, slots=True)
class ProjectListProjectionPage:
    items: tuple[Project, ...]
    count: int
    limit: int
    offset: int
    has_next: bool
    allowed_actions: tuple[str, ...]

    def as_response(self) -> dict[str, object]:
        return {
            "items": list(self.items),
            "count": self.count,
            "limit": self.limit,
            "offset": self.offset,
        }


def query_project_list_projection(
    db: Session, query: ProjectListProjectionQuery
) -> ProjectListProjectionPage:
    """Execute the complete typed Project list contract for every adapter."""
    if query.permission_scope != "project:read":
        raise _projection_error("unauthorized", "Project read scope is required")
    if query.order_by not in PROJECT_LIST_DEFINITION.sortable_keys:
        raise _projection_error("invalid_sort", "Unsupported project sort field")
    order_dir = query.order_dir.strip().lower()
    if order_dir not in {"asc", "desc"}:
        raise _projection_error("invalid_sort", "Unsupported project sort direction")
    if query.limit < 1 or query.limit > PROJECT_EXPORT_LIMIT or query.offset < 0:
        raise _projection_error("invalid_page", "Invalid project pagination")
    filter_clause = _build_project_filter_clause(query.filters)
    rows = projects_service.projects.list(
        db,
        subscriber_id=query.subscriber_id,
        status=query.status,
        project_type=query.project_type,
        priority=query.priority,
        owner_person_id=query.owner_person_id,
        manager_person_id=query.manager_person_id,
        project_manager_person_id=query.project_manager_person_id,
        assistant_manager_person_id=query.assistant_manager_person_id,
        is_active=query.is_active,
        order_by=query.order_by,
        order_dir=order_dir,
        limit=query.limit + 1,
        offset=query.offset,
        search=query.search,
        filter_clause=filter_clause,
        region=query.region,
    )
    has_next = len(rows) > query.limit
    items = tuple(rows[: query.limit])
    return ProjectListProjectionPage(
        items=items,
        count=len(items),
        limit=query.limit,
        offset=query.offset,
        has_next=has_next,
        allowed_actions=("view", "create", "update", "archive"),
    )


def build_project_list_query(
    *,
    search: str | None = None,
    status: str | None = None,
    project_type: str | None = None,
    priority: str | None = None,
    region: str | None = None,
    order_by: str | None = None,
    order_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalize the admin project list through its declared capabilities.

    ui.project_list_projection owns the searchable/filterable/sortable fields and
    pagination; the route submits raw request values. The read is delegated to
    projects_service.projects.list (operations.project_lifecycle).
    """
    sort_by = (
        order_by
        if order_by in PROJECT_LIST_DEFINITION.sortable_keys
        else PROJECT_LIST_DEFINITION.default_sort
    )
    sort_dir = (order_dir or "").strip().lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = PROJECT_LIST_DEFINITION.default_sort_dir
    effective_per_page = per_page or PROJECT_LIST_DEFINITION.default_per_page
    if effective_per_page not in PROJECT_LIST_DEFINITION.per_page_options:
        effective_per_page = PROJECT_LIST_DEFINITION.default_per_page
    return PROJECT_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "status": status,
            "project_type": project_type,
            "priority": priority,
            "region": region,
        },
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=max(1, page),
        per_page=effective_per_page,
    )


# ── small parsers (web_support_tickets idiom) ────────────────────────────────


def parse_uuid_or_none(value: str | None) -> UUID | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError:
        return None


def parse_dt_or_none(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_int_or_none(value: str | None) -> int | None:
    text = (str(value) if value is not None else "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError("Effort hours must be a number.") from exc


def _fmt_dt_input(value: datetime | None) -> str:
    if not value:
        return ""
    return value.strftime("%Y-%m-%dT%H:%M")


def _fmt_dt_out(value: datetime | None) -> str:
    if not value:
        return ""
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _non_empty_ids(values: list[object | None]) -> list[str]:
    ids: list[str] = []
    for value in values:
        if value in (None, ""):
            continue
        text = str(value)
        if text not in ids:
            ids.append(text)
    return ids


def _label_lookup(options: list[dict[str, str]]) -> dict[str, str]:
    return {item["id"]: item["label"] for item in options if item.get("id")}


def normalize_order(order_by: str, order_dir: str) -> tuple[str, str]:
    if order_by not in _LIST_ORDER_COLUMNS:
        order_by = "created_at"
    if order_dir not in {"asc", "desc"}:
        order_dir = "desc"
    return order_by, order_dir


def _build_project_filter_clause(filters: str | None):
    try:
        return project_filters.build_project_filter_clause(filters)
    except FilterValidationError as exc:
        raise _projection_error("invalid_filter", str(exc)) from exc


def _build_task_filter_clause(filters: str | None):
    try:
        return project_filters.build_project_task_filter_clause(filters)
    except FilterValidationError as exc:
        raise _projection_error("invalid_filter", str(exc)) from exc


# ── reference resolvers (number first, UUID fallback — email deep links use
#    /admin/projects/{uuid} and must resolve for legacy notification links) ──


def resolve_project_reference(db: Session, project_ref: str) -> tuple[Project, bool]:
    """Resolve a project by number or UUID.

    Returns ``(project, should_redirect)`` — redirect to the canonical number
    URL when the lookup came in by UUID but the project has a number.
    """
    ref = (project_ref or "").strip()
    if not ref:
        raise _projection_error("not_found", "Project not found")
    project = db.query(Project).filter(Project.number == ref).first()
    if project:
        return project, False
    try:
        project_uuid = coerce_uuid(ref)
    except Exception as exc:
        raise _projection_error("not_found", "Project not found") from exc
    try:
        project = projects_service.projects.get(db, str(project_uuid))
    except DomainError as exc:
        raise _projection_error("not_found", "Project not found") from exc
    return project, bool(project.number)


def resolve_task_reference(db: Session, task_ref: str) -> tuple[ProjectTask, bool]:
    ref = (task_ref or "").strip()
    if not ref:
        raise _projection_error("not_found", "Project task not found")
    query = db.query(ProjectTask).options(selectinload(ProjectTask.assignees))
    task = query.filter(ProjectTask.number == ref).first()
    if task:
        return task, False
    try:
        task_uuid = coerce_uuid(ref)
    except Exception as exc:
        raise _projection_error("not_found", "Project task not found") from exc
    task = query.filter(ProjectTask.id == task_uuid).first()
    if not task:
        raise _projection_error("not_found", "Project task not found")
    return task, bool(task.number)


def project_url(project: Project) -> str:
    return f"/admin/projects/{project.number or project.id}"


def task_url(task: ProjectTask) -> str:
    return f"/admin/projects/tasks/{task.number or task.id}"


def _task_work_order_create_url(task: ProjectTask) -> str:
    return "/admin/dispatch/work-orders?" + urlencode({"project_task_id": str(task.id)})


def _work_order_url(public_id: str) -> str:
    return "/admin/dispatch/work-orders?" + urlencode({"q": public_id})


def _task_work_order_create_action(
    task: ProjectTask, project: Project
) -> tuple[Action, str | None]:
    if not task.is_active:
        allowed, reason = False, "Archived tasks cannot create field work"
    elif not project.is_active:
        allowed, reason = False, "Archived projects cannot create field work"
    elif project.subscriber_id is None:
        allowed = False
        reason = "Link a subscriber to the project before creating field work"
    else:
        allowed, reason = True, None
    action = Action(
        key="create_work_order",
        label="Create Work Order",
        allowed=allowed,
        reason=reason,
        permission="operations:dispatch:write",
    )
    return action, _task_work_order_create_url(task) if allowed else None


@dataclass(frozen=True, slots=True)
class ProjectTaskWorkOrderProjection:
    """Typed zero/one/many field-work projection for one project task."""

    task_id: UUID
    work_orders: tuple[work_order_views.LinkedWorkOrderSummary, ...]
    action: Action
    action_url: str | None


def _task_work_order_projection(
    task: ProjectTask,
    project: Project,
    work_orders: tuple[work_order_views.LinkedWorkOrderSummary, ...],
) -> ProjectTaskWorkOrderProjection:
    count = len(work_orders)
    if count == 0:
        action, action_url = _task_work_order_create_action(task, project)
    elif count == 1:
        action = Action(
            key="open_work_order",
            label="Open Work Order",
            allowed=True,
            permission="operations:dispatch:read",
            affected=1,
        )
        action_url = _work_order_url(work_orders[0].public_id)
    else:
        action = Action(
            key="view_work_orders",
            label=f"View {count} Work Orders",
            allowed=True,
            permission="operations:dispatch:read",
            affected=count,
        )
        action_url = _task_work_order_create_url(task)
    return ProjectTaskWorkOrderProjection(
        task_id=task.id,
        work_orders=work_orders,
        action=action,
        action_url=action_url,
    )


# ── shared option helpers ────────────────────────────────────────────────────


def staff_options(
    db: Session, include_ids: list[str] | None = None
) -> list[dict[str, str]]:
    return support_service.list_assignment_people(db, include_ids=include_ids or [])


def subscriber_options(
    db: Session, include_ids: list[str] | None = None
) -> list[dict[str, str]]:
    return support_service.list_people(db, include_ids=include_ids or [])


def region_options(db: Session) -> list[str]:
    rows = (
        db.query(Project.region)
        .filter(Project.is_active.is_(True))
        .filter(Project.region.isnot(None), Project.region != "")
        .distinct()
        .order_by(Project.region.asc())
        .limit(200)
        .all()
    )
    discovered = [str(item[0]) for item in rows if item and item[0]]
    defaults = support_ticket_settings_service.list_region_options(db)
    return sorted(set(discovered + defaults))


def template_options(db: Session) -> list:
    return projects_service.project_templates.list(
        db,
        project_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )


def _template_option_dicts(templates: list) -> list[dict[str, str]]:
    return [{"id": str(item.id), "label": item.name} for item in templates]


def _project_template_map(templates: list) -> dict[str, str]:
    """Project type → template id (template instantiation auto-pick)."""
    mapping: dict[str, str] = {}
    for item in templates:
        if item.project_type:
            mapping[str(item.project_type)] = str(item.id)
    return mapping


def project_options(db: Session, limit: int = 500) -> list:
    return projects_service.projects.list(
        db,
        subscriber_id=None,
        status=None,
        project_type=None,
        priority=None,
        owner_person_id=None,
        manager_person_id=None,
        project_manager_person_id=None,
        assistant_manager_person_id=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=limit,
        offset=0,
    )


def _project_option_dicts(projects: list) -> list[dict[str, str]]:
    return [
        {"id": str(item.id), "label": item.name or str(item.number or item.id)}
        for item in projects
    ]


def _subscriber_label(project: Project) -> str:
    subscriber = project.subscriber
    if not subscriber:
        return ""
    label = subscriber.display_name or (
        f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        or "Subscriber"
    )
    if getattr(subscriber, "subscriber_number", None):
        return f"{label} ({subscriber.subscriber_number})"
    return str(label)


def _status_summary_cards(db: Session) -> list[dict[str, str | int]]:
    rows = (
        db.query(Project.status, func.count(Project.id))
        .filter(Project.is_active.is_(True))
        .group_by(Project.status)
        .all()
    )
    counts = {str(status): int(count) for status, count in rows if status}
    colors = {
        "open": "sky",
        "planned": "violet",
        "active": "emerald",
        "on_hold": "amber",
        "completed": "slate",
        "canceled": "red",
    }
    return [
        {
            "value": status.value,
            "label": status.value.replace("_", " ").title(),
            "count": counts.get(status.value, 0),
            "href": f"/admin/projects?status={status.value}",
            "color": colors.get(status.value, "slate"),
        }
        for status in ProjectStatus
    ]


def _breached_task_ids(db: Session, task_ids: list) -> set[str]:
    if not task_ids:
        return set()
    rows = (
        db.query(SlaClock.entity_id)
        .filter(SlaClock.entity_type == WorkflowEntityType.project_task.value)
        .filter(SlaClock.entity_id.in_(task_ids))
        .filter(SlaClock.status == SlaClockStatus.breached.value)
        .all()
    )
    return {str(row[0]) for row in rows if row and row[0]}


# ── fiber-stage timeline (detail page; portal payload semantics) ─────────────


def build_fiber_stage_rows(tasks: list[ProjectTask]) -> list[dict]:
    """Stage timeline rows derived from the project's tasks (§2.1 engine)."""
    by_stage: dict[str, ProjectTask] = {}
    for task in tasks:
        stage_key = projects_service._resolve_fiber_stage_key(task)
        if stage_key and stage_key not in by_stage:
            by_stage[stage_key] = task
    if not by_stage:
        return []
    rows: list[dict] = []
    for stage_key in projects_service.FIBER_INSTALLATION_STAGE_ORDER:
        stage_task = by_stage.get(stage_key)
        metadata = (
            stage_task.metadata_
            if stage_task and isinstance(stage_task.metadata_, dict)
            else {}
        )
        rows.append(
            {
                "key": stage_key,
                "title": projects_service.FIBER_INSTALLATION_STAGE_TITLES[stage_key],
                "status": customer_experience_lifecycle.project_task_stage_state(
                    stage_task.status if stage_task else ProjectTaskStatus.todo.value
                ).value,
                "task_ref": (
                    (stage_task.number or str(stage_task.id)) if stage_task else None
                ),
                "task_url": task_url(stage_task) if stage_task else None,
                "due_at": _fmt_dt_out(stage_task.due_at) if stage_task else "",
                "completed_at": (
                    _fmt_dt_out(stage_task.completed_at) if stage_task else ""
                ),
                "sla_breached": bool(metadata.get("sla_breached")),
            }
        )
    return rows


# ── projects list / export ───────────────────────────────────────────────────


def project_list_url(
    query: ListQuery, *, page: int, dynamic_filters: str | None
) -> str:
    params: list[tuple[str, str]] = [
        ("order_by", query.sort_by),
        ("order_dir", query.sort_dir),
        ("page", str(max(1, page))),
        ("per_page", str(query.per_page)),
    ]
    if query.search:
        params.append(("search", query.search))
    params.extend(query.filters)
    if dynamic_filters:
        params.append(("filters", dynamic_filters))
    return f"/admin/projects?{urlencode(params)}"


def build_projects_list_context(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    project_type: str | None,
    priority: str | None,
    region: str | None,
    filters: str | None,
    order_by: str,
    order_dir: str,
    page: int,
    per_page: int,
) -> dict:
    list_query = build_project_list_query(
        search=search,
        status=status,
        project_type=project_type,
        priority=priority,
        region=region,
        order_by=order_by,
        order_dir=order_dir,
        page=page,
        per_page=per_page,
    )
    order_by, order_dir = list_query.sort_by, list_query.sort_dir
    page, per_page = list_query.page, list_query.per_page
    page_result = query_project_list_projection(
        db,
        ProjectListProjectionQuery(
            status=list_query.filter_value("status"),
            project_type=list_query.filter_value("project_type"),
            priority=list_query.filter_value("priority"),
            filters=filters,
            search=list_query.search,
            region=list_query.filter_value("region"),
            order_by=order_by,
            order_dir=order_dir,
            limit=per_page,
            offset=list_query.offset,
        ),
    )

    has_next_page = page_result.has_next
    rows = list(page_result.items)

    assignment_ids: list[object | None] = []
    for project in rows:
        assignment_ids.extend(
            [
                project.owner_person_id,
                project.manager_person_id,
                project.project_manager_person_id,
                project.assistant_manager_person_id,
            ]
        )
    staff = staff_options(db, include_ids=_non_empty_ids(assignment_ids))
    templates = template_options(db)
    status_options = [item.value for item in ProjectStatus]
    priority_options = [item.value for item in ProjectPriority]
    type_options = [item.value for item in ProjectType]
    return {
        "projects": rows,
        "search": search or "",
        "status": status or "",
        "project_type": project_type or "",
        "priority": priority or "",
        "region": region or "",
        "filters": filters or "",
        "order_by": order_by,
        "order_dir": order_dir,
        "page": page,
        "per_page": per_page,
        "has_next_page": has_next_page,
        "allowed_actions": page_result.allowed_actions,
        "previous_page_url": project_list_url(
            list_query, page=max(1, page - 1), dynamic_filters=filters
        ),
        "next_page_url": project_list_url(
            list_query, page=page + 1, dynamic_filters=filters
        ),
        "status_summary_cards": _status_summary_cards(db),
        "all_statuses": status_options,
        "all_priorities": priority_options,
        "project_type_options": type_options,
        "region_options": region_options(db),
        "staff_options": staff,
        "staff_lookup": _label_lookup(staff),
        "project_filter_schema": project_filters.serialize_project_filter_schema(
            status_options=status_options,
            priority_options=priority_options,
            project_type_options=type_options,
            staff_options=staff,
            template_options=_template_option_dicts(templates),
        ),
        "project_export_columns": PROJECT_EXPORT_COLUMNS,
    }


def visible_project_columns(raw: str | None) -> list[str]:
    valid = {item["key"] for item in PROJECT_EXPORT_COLUMNS}
    parsed: list[str] = []
    for token in (raw or "").split(","):
        key = token.strip()
        if key and key in valid and key not in parsed:
            parsed.append(key)
    normalized = parsed or list(DEFAULT_PROJECT_EXPORT_COLUMNS)
    for required in REQUIRED_PROJECT_EXPORT_COLUMNS:
        if required not in normalized:
            normalized.append(required)
    return normalized


def _project_csv_value(
    project: Project, key: str, *, staff_lookup: dict[str, str]
) -> str:
    def _person(value: object | None) -> str:
        text = str(value) if value else ""
        return staff_lookup.get(text, text)

    if key == "project":
        return project.name or ""
    if key == "customer":
        return _subscriber_label(project)
    if key == "status":
        return project.status or ""
    if key == "priority":
        return project.priority or ""
    if key == "created":
        return _fmt_dt_out(project.created_at)
    if key == "code":
        return str(project.number or project.code or project.id)
    if key == "type":
        return project.project_type or ""
    if key == "region":
        return project.region or ""
    if key == "owner":
        return _person(project.owner_person_id)
    if key == "manager":
        return _person(project.manager_person_id)
    if key == "project_manager":
        return _person(project.project_manager_person_id)
    if key == "start_at":
        return _fmt_dt_out(project.start_at)
    if key == "due_at":
        return _fmt_dt_out(project.due_at)
    return ""


def render_projects_csv(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    project_type: str | None,
    priority: str | None,
    region: str | None,
    filters: str | None,
    order_by: str,
    order_dir: str,
    columns: str | None,
) -> str:
    order_by, order_dir = normalize_order(order_by, order_dir)
    filter_clause = _build_project_filter_clause(filters)
    rows = projects_service.projects.list(
        db,
        subscriber_id=None,
        status=status or None,
        project_type=project_type or None,
        priority=priority or None,
        owner_person_id=None,
        manager_person_id=None,
        project_manager_person_id=None,
        assistant_manager_person_id=None,
        is_active=None,
        order_by=order_by,
        order_dir=order_dir,
        limit=PROJECT_EXPORT_LIMIT,
        offset=0,
        search=search,
        filter_clause=filter_clause,
        region=(region or "").strip() or None,
    )
    assignment_ids: list[object | None] = []
    for project in rows:
        assignment_ids.extend(
            [
                project.owner_person_id,
                project.manager_person_id,
                project.project_manager_person_id,
            ]
        )
    staff_lookup = _label_lookup(
        staff_options(db, include_ids=_non_empty_ids(assignment_ids))
    )
    export_columns = visible_project_columns(columns)
    labels = {item["key"]: item["label"] for item in PROJECT_EXPORT_COLUMNS}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([labels[key] for key in export_columns])
    for project in rows:
        writer.writerow(
            [
                _project_csv_value(project, key, staff_lookup=staff_lookup)
                for key in export_columns
            ]
        )
    return buffer.getvalue()


# ── project form (create / edit) ─────────────────────────────────────────────


def build_project_form_context(
    db: Session,
    *,
    project: Project | None = None,
    form: dict | None = None,
    error: str | None = None,
) -> dict:
    values = form or {}

    def _value(key: str, project_attr: str | None = None) -> str:
        if form is not None:
            return str(values.get(key, "") or "")
        if project is not None:
            attr = getattr(project, project_attr or key, None)
            if isinstance(attr, datetime):
                return _fmt_dt_input(attr)
            return str(attr) if attr not in (None, "") else ""
        return ""

    prefill = {
        "name": _value("name"),
        "code": _value("code"),
        "description": _value("description"),
        "customer_address": _value("customer_address"),
        "project_type": _value("project_type"),
        "project_template_id": _value("project_template_id"),
        "status": _value("status") or ProjectStatus.open.value,
        "priority": _value("priority") or ProjectPriority.normal.value,
        "subscriber_id": _value("subscriber_id"),
        "owner_person_id": _value("owner_person_id"),
        "manager_person_id": _value("manager_person_id"),
        "project_manager_person_id": _value("project_manager_person_id"),
        "assistant_manager_person_id": _value("assistant_manager_person_id"),
        "start_at": _value("start_at"),
        "due_at": _value("due_at"),
        "region": _value("region"),
        "is_active": (
            str(values.get("is_active", "true")).lower() in {"1", "true", "yes", "on"}
            if form is not None
            else (bool(project.is_active) if project is not None else True)
        ),
    }
    templates = template_options(db)
    staff = staff_options(
        db,
        include_ids=_non_empty_ids(
            [
                prefill["owner_person_id"],
                prefill["manager_person_id"],
                prefill["project_manager_person_id"],
                prefill["assistant_manager_person_id"],
            ]
        ),
    )
    context: dict[str, object] = {
        "prefill": prefill,
        "project": project,
        "project_templates": templates,
        "project_template_map": _project_template_map(templates),
        "project_types": [item.value for item in ProjectType],
        "project_statuses": [item.value for item in ProjectStatus],
        "project_priorities": [item.value for item in ProjectPriority],
        "region_options": region_options(db),
        "staff_options": staff,
        "subscriber_options": subscriber_options(
            db, include_ids=_non_empty_ids([prefill["subscriber_id"]])
        ),
    }
    if error:
        context["error"] = error
    return context


def _project_payload_data(*, actor_id: str | None = None, **form) -> dict:
    name = str(form.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required.")
    data: dict[str, object] = {
        "name": name,
        "status": str(form.get("status") or "").strip() or ProjectStatus.open.value,
        "priority": str(form.get("priority") or "").strip()
        or ProjectPriority.normal.value,
        "is_active": str(form.get("is_active", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
    }
    for key in ("code", "description", "customer_address", "project_type", "region"):
        value = str(form.get(key) or "").strip()
        if value:
            data[key] = value
    for key in (
        "subscriber_id",
        "owner_person_id",
        "manager_person_id",
        "project_manager_person_id",
        "assistant_manager_person_id",
    ):
        uuid_value = parse_uuid_or_none(form.get(key))
        if uuid_value:
            data[key] = uuid_value
    for key in ("start_at", "due_at"):
        dt_value = parse_dt_or_none(form.get(key))
        if dt_value:
            data[key] = dt_value
    template_id = parse_uuid_or_none(form.get("project_template_id"))
    if template_id:
        data["project_template_id"] = template_id
    if actor_id and parse_uuid_or_none(actor_id):
        data["created_by_person_id"] = parse_uuid_or_none(actor_id)
    return data


def create_project_from_form(
    db: Session, *, request, actor_id: str | None, **form
) -> Project:
    payload = ProjectCreate.model_validate(
        _project_payload_data(actor_id=actor_id, **form)
    )
    return projects_service.projects.create(db, payload)


def update_project_from_form(
    db: Session, *, request, project_id: str, actor_id: str | None, **form
) -> Project:
    data = _project_payload_data(**form)
    # Template can be cleared from the edit form (CRM parity).
    data["project_template_id"] = parse_uuid_or_none(form.get("project_template_id"))
    payload = ProjectUpdate.model_validate(data)
    return projects_service.projects.update(
        db,
        project_id,
        payload,
        actor_id=parse_uuid_or_none(actor_id),
    )


def quick_update_project(
    db: Session,
    *,
    request,
    project_id: str,
    actor_id: str | None,
    field: str,
    value: str,
) -> Project:
    if field not in {"status", "priority"}:
        raise _projection_error("invalid_filter", "Unsupported field")
    try:
        payload = ProjectUpdate.model_validate({field: str(value or "").strip()})
    except ValidationError as exc:
        raise _projection_error("invalid_filter", f"Invalid {field}") from exc
    return projects_service.projects.update(
        db,
        project_id,
        payload,
        actor_id=parse_uuid_or_none(actor_id),
    )


def delete_project(
    db: Session, *, request, project_id: str, actor_id: str | None
) -> None:
    projects_service.projects.delete(
        db,
        project_id,
        actor_id=parse_uuid_or_none(actor_id),
    )


# ── project detail ───────────────────────────────────────────────────────────


def build_project_detail_context(
    db: Session,
    *,
    project: Project,
    can_read_work_orders: bool = False,
) -> dict:
    tasks = projects_service.project_tasks.list(
        db,
        project_id=str(project.id),
        status=None,
        priority=None,
        assigned_to_person_id=None,
        parent_task_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=200,
        offset=0,
        include_assigned=True,
    )
    comments = projects_service.project_comments.list(
        db,
        project_id=str(project.id),
        order_by="created_at",
        order_dir="desc",
        limit=200,
        offset=0,
    )
    assignment_ids: list[object | None] = [
        project.owner_person_id,
        project.manager_person_id,
        project.project_manager_person_id,
        project.assistant_manager_person_id,
        project.created_by_person_id,
        *[comment.author_person_id for comment in comments],
    ]
    for task in tasks:
        assignment_ids.append(task.assigned_to_person_id)
        assignment_ids.extend(row.person_id for row in (task.assignees or []))
    staff = staff_options(db, include_ids=_non_empty_ids(assignment_ids))
    template = None
    if project.project_template_id:
        try:
            template = projects_service.project_templates.get(
                db, str(project.project_template_id)
            )
        except DomainError:
            template = None
    return {
        "project": project,
        "project_url": project_url(project),
        "tasks": tasks,
        "show_field_work": can_read_work_orders,
        "project_work_orders": (
            work_order_views.list_project_work_order_summaries(db, project.id)
            if can_read_work_orders
            else ()
        ),
        "comments": comments,
        "activities": build_audit_activities(db, "project", str(project.id), limit=20),
        "fiber_stages": build_fiber_stage_rows(tasks),
        "breached_task_ids": _breached_task_ids(db, [task.id for task in tasks]),
        "staff_options": staff,
        "staff_lookup": _label_lookup(staff),
        "subscriber_label": _subscriber_label(project),
        "template": template,
        "all_statuses": [item.value for item in ProjectStatus],
        "all_priorities": [item.value for item in ProjectPriority],
        "task_statuses": [item.value for item in ProjectTaskStatus],
    }


def add_project_comment_from_form(
    db: Session, *, request, project_id: str, actor_id: str | None, body: str
) -> ProjectComment:
    payload = ProjectCommentCreate(
        project_id=coerce_uuid(project_id),
        author_person_id=parse_uuid_or_none(actor_id),
        body=body,
    )
    comment = projects_service.project_comments.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="comment",
        entity_type="project",
        entity_id=str(project_id),
        actor_id=actor_id,
    )
    return comment


def update_project_comment_from_form(
    db: Session,
    *,
    request,
    project_id: str,
    comment_id: str,
    actor_id: str | None,
    body: str,
) -> ProjectComment:
    comment = db.get(ProjectComment, coerce_uuid(comment_id))
    if not comment or str(comment.project_id) != str(coerce_uuid(project_id)):
        raise _projection_error("not_found", "Comment not found")
    actor_uuid = parse_uuid_or_none(actor_id)
    if (
        not actor_uuid
        or not comment.author_person_id
        or str(comment.author_person_id) != str(actor_uuid)
    ):
        raise _projection_error("unauthorized", "You can only edit your own comments.")
    updated = projects_service.project_comments.update(
        db, str(comment.id), ProjectCommentUpdate(body=body)
    )
    log_audit_event(
        db=db,
        request=request,
        action="comment_edit",
        entity_type="project",
        entity_id=str(project_id),
        actor_id=actor_id,
    )
    return updated


# ── project tasks list / forms / detail ──────────────────────────────────────


def build_tasks_list_context(
    db: Session,
    *,
    project_id: str | None,
    status: str | None,
    priority: str | None,
    assigned_to_me: bool,
    actor_id: str | None,
    filters: str | None,
    page: int,
    per_page: int,
    can_read_work_orders: bool = False,
) -> dict:
    filter_clause = _build_task_filter_clause(filters)
    assigned_to_person_id = None
    if assigned_to_me:
        if not parse_uuid_or_none(actor_id):
            raise _projection_error(
                "invalid_filter",
                "Unable to resolve current user for assignment filter",
            )
        assigned_to_person_id = actor_id
    offset = (page - 1) * per_page
    rows_plus_one = projects_service.project_tasks.list(
        db,
        project_id=project_id or None,
        status=status or None,
        priority=priority or None,
        assigned_to_person_id=assigned_to_person_id,
        parent_task_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page + 1,
        offset=offset,
        include_assigned=True,
        filter_clause=filter_clause,
    )
    has_next_page = len(rows_plus_one) > per_page
    rows = rows_plus_one[:per_page]
    projects = project_options(db, limit=1000)
    project_ids = {task.project_id for task in rows if task.project_id}
    project_rows = (
        db.query(Project).filter(Project.id.in_(project_ids)).all()
        if project_ids
        else []
    )
    project_map = {project.id: project for project in project_rows}
    linked_work_orders = (
        work_order_views.list_task_work_order_summaries_bulk(
            db, [task.id for task in rows]
        )
        if can_read_work_orders
        else {}
    )
    task_work_order_projections = {
        str(task.id): _task_work_order_projection(
            task,
            project_map[task.project_id],
            linked_work_orders.get(task.id, ()),
        )
        for task in rows
        if can_read_work_orders and task.project_id in project_map
    }
    assignment_ids: list[object | None] = []
    for task in rows:
        assignment_ids.append(task.assigned_to_person_id)
        assignment_ids.extend(row.person_id for row in (task.assignees or []))
    staff = staff_options(db, include_ids=_non_empty_ids(assignment_ids + [actor_id]))
    status_options = [item.value for item in ProjectTaskStatus]
    priority_options = [item.value for item in ProjectTaskPriority]
    return {
        "tasks": rows,
        "projects": projects,
        "project_map": {str(project.id): project for project in project_rows},
        "show_field_work": can_read_work_orders,
        "task_work_order_projections": task_work_order_projections,
        "project_id": project_id or "",
        "status": status or "",
        "priority": priority or "",
        "assigned_to_me": assigned_to_me,
        "filters": filters or "",
        "page": page,
        "per_page": per_page,
        "has_next_page": has_next_page,
        "breached_task_ids": _breached_task_ids(db, [task.id for task in rows]),
        "staff_options": staff,
        "staff_lookup": _label_lookup(staff),
        "all_statuses": status_options,
        "all_priorities": priority_options,
        "task_filter_schema": project_filters.serialize_project_task_filter_schema(
            status_options=status_options,
            priority_options=priority_options,
            staff_options=staff,
            project_options=_project_option_dicts(projects),
        ),
    }


def build_task_form_context(
    db: Session,
    *,
    task: ProjectTask | None = None,
    form: dict | None = None,
    error: str | None = None,
) -> dict:
    values = form or {}

    def _value(key: str) -> str:
        if form is not None:
            return str(values.get(key, "") or "")
        if task is not None:
            attr = getattr(task, key, None)
            if isinstance(attr, datetime):
                return _fmt_dt_input(attr)
            return str(attr) if attr not in (None, "") else ""
        return ""

    if form is not None:
        assignee_ids = [str(item) for item in values.get("assigned_to_person_ids", [])]
    elif task is not None:
        assignee_ids = [str(row.person_id) for row in (task.assignees or [])] or (
            [str(task.assigned_to_person_id)] if task.assigned_to_person_id else []
        )
    else:
        assignee_ids = []

    prefill = {
        "project_id": _value("project_id"),
        "title": _value("title"),
        "description": _value("description"),
        "status": _value("status") or ProjectTaskStatus.todo.value,
        "priority": _value("priority") or ProjectTaskPriority.normal.value,
        "assigned_to_person_ids": assignee_ids,
        "start_at": _value("start_at"),
        "due_at": _value("due_at"),
        "effort_hours": _value("effort_hours"),
    }
    context: dict[str, object] = {
        "prefill": prefill,
        "task": task,
        "projects": project_options(db, limit=1000),
        "staff_options": staff_options(db, include_ids=list(assignee_ids)),
        "task_statuses": [item.value for item in ProjectTaskStatus],
        "task_priorities": [item.value for item in ProjectTaskPriority],
    }
    if error:
        context["error"] = error
    return context


def _task_payload_data(*, actor_id: str | None = None, **form) -> dict:
    project_id = parse_uuid_or_none(form.get("project_id"))
    if not project_id:
        raise ValueError("Project is required.")
    title = str(form.get("title") or "").strip()
    if not title:
        raise ValueError("Title is required.")
    data: dict[str, object] = {
        "project_id": project_id,
        "title": title,
        "status": str(form.get("status") or "").strip() or ProjectTaskStatus.todo.value,
        "priority": str(form.get("priority") or "").strip()
        or ProjectTaskPriority.normal.value,
    }
    description = str(form.get("description") or "").strip()
    if description:
        data["description"] = description
    assignee_ids = [
        uid
        for uid in (
            parse_uuid_or_none(item)
            for item in (form.get("assigned_to_person_ids") or [])
        )
        if uid
    ]
    data["assigned_to_person_ids"] = assignee_ids
    if assignee_ids:
        data["assigned_to_person_id"] = assignee_ids[0]
    for key in ("start_at", "due_at"):
        value = parse_dt_or_none(form.get(key))
        if value:
            data[key] = value
    effort = parse_int_or_none(form.get("effort_hours"))
    if effort is not None:
        data["effort_hours"] = effort
    if actor_id and parse_uuid_or_none(actor_id):
        data["created_by_person_id"] = parse_uuid_or_none(actor_id)
    return data


def create_task_from_form(
    db: Session, *, request, actor_id: str | None, **form
) -> ProjectTask:
    payload = ProjectTaskCreate.model_validate(
        _task_payload_data(actor_id=actor_id, **form)
    )
    return projects_service.project_tasks.create(db, payload)


def update_task_from_form(
    db: Session, *, request, task_id: str, actor_id: str | None, **form
) -> ProjectTask:
    data = _task_payload_data(**form)
    payload = ProjectTaskUpdate.model_validate(data)
    return projects_service.project_tasks.update(
        db,
        task_id,
        payload,
        actor_id=parse_uuid_or_none(actor_id),
    )


def quick_update_task_status(
    db: Session, *, request, task_id: str, actor_id: str | None, status: str
) -> ProjectTask:
    try:
        payload = ProjectTaskUpdate.model_validate(
            {"status": str(status or "").strip()}
        )
    except ValidationError as exc:
        raise _projection_error("invalid_filter", "Invalid status") from exc
    return projects_service.project_tasks.update(
        db,
        task_id,
        payload,
        actor_id=parse_uuid_or_none(actor_id),
    )


def delete_task(db: Session, *, request, task_id: str, actor_id: str | None) -> None:
    projects_service.project_tasks.delete(
        db,
        task_id,
        actor_id=parse_uuid_or_none(actor_id),
    )


def _task_dependency_rows(db: Session, task: ProjectTask) -> dict[str, list[dict]]:
    """Blocked-by / blocks rows for the task detail page (read-only; the
    dependency editor lives on the template — CRM parity)."""
    blocked_by_links = (
        db.query(ProjectTaskDependency)
        .filter(ProjectTaskDependency.task_id == task.id)
        .all()
    )
    blocks_links = (
        db.query(ProjectTaskDependency)
        .filter(ProjectTaskDependency.depends_on_task_id == task.id)
        .all()
    )
    related_ids = {link.depends_on_task_id for link in blocked_by_links} | {
        link.task_id for link in blocks_links
    }
    related = (
        db.query(ProjectTask).filter(ProjectTask.id.in_(related_ids)).all()
        if related_ids
        else []
    )
    related_map = {row.id: row for row in related}

    def _row(link, other_id) -> dict:
        other = related_map.get(other_id)
        return {
            "title": other.title if other else str(other_id),
            "ref": (other.number or str(other.id)) if other else str(other_id),
            "url": task_url(other) if other else None,
            "status": other.status if other else None,
            "dependency_type": link.dependency_type,
            "lag_days": link.lag_days,
        }

    return {
        "blocked_by": [
            _row(link, link.depends_on_task_id) for link in blocked_by_links
        ],
        "blocks": [_row(link, link.task_id) for link in blocks_links],
    }


def build_task_detail_context(
    db: Session,
    *,
    task: ProjectTask,
    can_read_work_orders: bool = False,
) -> dict:
    project = projects_service.projects.get(db, str(task.project_id))
    comments = projects_service.project_task_comments.list(
        db,
        task_id=str(task.id),
        order_by="created_at",
        order_dir="desc",
        limit=200,
        offset=0,
    )
    assignment_ids: list[object | None] = [
        task.assigned_to_person_id,
        task.created_by_person_id,
        *[row.person_id for row in (task.assignees or [])],
        *[comment.author_person_id for comment in comments],
    ]
    staff = staff_options(db, include_ids=_non_empty_ids(assignment_ids))
    metadata = task.metadata_ if isinstance(task.metadata_, dict) else {}
    create_work_order_action, work_order_create_url = _task_work_order_create_action(
        task, project
    )
    return {
        "task": task,
        "task_url": task_url(task),
        "project": project,
        "project_href": project_url(project),
        "show_field_work": can_read_work_orders,
        "task_work_orders": (
            work_order_views.list_task_work_order_summaries(db, task.id)
            if can_read_work_orders
            else ()
        ),
        "create_work_order_action": create_work_order_action,
        "work_order_create_url": work_order_create_url,
        "comments": comments,
        "activities": build_audit_activities(
            db, "project_task", str(task.id), limit=20
        ),
        "dependencies": _task_dependency_rows(db, task),
        "task_is_breached": bool(_breached_task_ids(db, [task.id])),
        "fiber_stage_title": metadata.get("fiber_stage_title"),
        "sla_breached_at": metadata.get("sla_breached_at"),
        "staff_options": staff,
        "staff_lookup": _label_lookup(staff),
        "task_statuses": [item.value for item in ProjectTaskStatus],
        "task_priorities": [item.value for item in ProjectTaskPriority],
    }


def add_task_comment_from_form(
    db: Session, *, request, task_id: str, actor_id: str | None, body: str
):
    payload = ProjectTaskCommentCreate(
        task_id=coerce_uuid(task_id),
        author_person_id=parse_uuid_or_none(actor_id),
        body=body,
    )
    comment = projects_service.project_task_comments.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="comment",
        entity_type="project_task",
        entity_id=str(task_id),
        actor_id=actor_id,
    )
    return comment


# ── project templates admin ──────────────────────────────────────────────────


def build_templates_list_context(db: Session) -> dict:
    templates = template_options(db)
    counts_rows = (
        db.query(ProjectTemplateTask.template_id, func.count(ProjectTemplateTask.id))
        .filter(ProjectTemplateTask.is_active.is_(True))
        .group_by(ProjectTemplateTask.template_id)
        .all()
    )
    task_counts = {str(template_id): int(count) for template_id, count in counts_rows}
    return {"templates": templates, "template_task_counts": task_counts}


def build_template_form_context(
    db: Session,
    *,
    template=None,
    form: dict | None = None,
    error: str | None = None,
) -> dict:
    values = form or {}

    def _value(key: str) -> str:
        if form is not None:
            return str(values.get(key, "") or "")
        if template is not None:
            attr = getattr(template, key, None)
            return str(attr) if attr not in (None, "") else ""
        return ""

    prefill = {
        "name": _value("name"),
        "project_type": _value("project_type"),
        "description": _value("description"),
        "is_active": (
            str(values.get("is_active", "true")).lower() in {"1", "true", "yes", "on"}
            if form is not None
            else (bool(template.is_active) if template is not None else True)
        ),
    }
    context = {
        "prefill": prefill,
        "template": template,
        "project_types": [item.value for item in ProjectType],
    }
    if error:
        context["error"] = error
    return context


def _template_payload_data(**form) -> dict:
    name = str(form.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required.")
    data: dict[str, object] = {
        "name": name,
        "is_active": str(form.get("is_active", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
    }
    project_type = str(form.get("project_type") or "").strip()
    data["project_type"] = project_type or None
    description = str(form.get("description") or "").strip()
    data["description"] = description or None
    return data


def create_template_from_form(db: Session, **form):
    payload = ProjectTemplateCreate.model_validate(_template_payload_data(**form))
    return projects_service.project_templates.create(db, payload)


def update_template_from_form(db: Session, *, template_id: str, **form):
    payload = ProjectTemplateUpdate.model_validate(_template_payload_data(**form))
    return projects_service.project_templates.update(db, template_id, payload)


def build_template_detail_context(db: Session, *, template_id: str) -> dict:
    template = projects_service.project_templates.get(db, template_id)
    tasks = projects_service.project_template_tasks.list(
        db,
        template_id=template_id,
        is_active=True,
        order_by="sort_order",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    task_ids = [task.id for task in tasks]
    dependency_labels: dict[str, list[str]] = {}
    if task_ids:
        titles = {str(task.id): task.title for task in tasks}
        links = (
            db.query(ProjectTemplateTaskDependency)
            .filter(ProjectTemplateTaskDependency.template_task_id.in_(task_ids))
            .all()
        )
        for link in links:
            dependency_labels.setdefault(str(link.template_task_id), []).append(
                titles.get(
                    str(link.depends_on_template_task_id),
                    str(link.depends_on_template_task_id),
                )
            )
    return {
        "template": template,
        "template_tasks": tasks,
        "dependency_labels": dependency_labels,
    }


def build_template_tasks_editor_payload(db: Session, template_id: str) -> list[dict]:
    tasks = projects_service.project_template_tasks.list(
        db,
        template_id=template_id,
        is_active=True,
        order_by="sort_order",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    task_ids = [task.id for task in tasks]
    dependencies_map: dict[str, list[str]] = {}
    if task_ids:
        links = (
            db.query(ProjectTemplateTaskDependency)
            .filter(ProjectTemplateTaskDependency.template_task_id.in_(task_ids))
            .all()
        )
        for link in links:
            dependencies_map.setdefault(str(link.template_task_id), []).append(
                str(link.depends_on_template_task_id)
            )
    return [
        {
            "client_id": str(task.id),
            "id": str(task.id),
            "title": task.title,
            "description": task.description or "",
            "effort_hours": task.effort_hours if task.effort_hours is not None else "",
            "dependencies": dependencies_map.get(str(task.id), []),
        }
        for task in tasks
    ]


class _TemplateTaskJSONItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_id: str
    title: str
    description: str = ""
    effort_hours: int | str | None = None
    dependencies: list[str] = Field(default_factory=list)


_TEMPLATE_TASKS_JSON_ADAPTER = TypeAdapter(list[_TemplateTaskJSONItem])


def save_template_tasks_from_editor(
    db: Session, *, template_id: str, tasks_json: str
) -> None:
    """Bulk task/dependency editor save — ported from CRM's editor POST.

    Upserts template tasks by ``client_id`` (existing task UUID or a fresh
    client-side id), soft-deletes removed tasks, and rebuilds the
    finish-to-start dependency links. Raises ``ValueError`` with a
    user-facing message on invalid input.
    """
    template = projects_service.project_templates.get(db, template_id)
    raw = (tasks_json or "").strip()
    try:
        items = _TEMPLATE_TASKS_JSON_ADAPTER.validate_json(raw) if raw else []
    except ValidationError as exc:
        errors = exc.errors()
        loc = ".".join(str(part) for part in errors[0].get("loc", ())) if errors else ""
        message = (
            str(errors[0].get("msg", "Invalid payload."))
            if errors
            else "Invalid payload."
        )
        raise ValueError(
            f"Tasks data is invalid: {loc}: {message}" if loc else message
        ) from exc

    seen_client_ids: set[str] = set()
    normalized: list[dict] = []
    for item in items:
        client_id = item.client_id.strip()
        title = item.title.strip()
        if not client_id:
            raise ValueError("Each task must have a client_id.")
        if not title:
            raise ValueError("Each task must have a title.")
        if client_id in seen_client_ids:
            raise ValueError("Duplicate task client_id found.")
        seen_client_ids.add(client_id)
        effort_raw = "" if item.effort_hours is None else str(item.effort_hours).strip()
        effort_hours: int | None = None
        if effort_raw:
            try:
                effort_hours = int(effort_raw)
            except ValueError as exc:
                raise ValueError(f"Invalid effort_hours for task '{title}'.") from exc
        normalized.append(
            {
                "client_id": client_id,
                "title": title,
                "description": item.description.strip(),
                "effort_hours": effort_hours,
                "dependencies": item.dependencies or [],
            }
        )

    template_uuid = template.id
    existing_tasks = (
        db.query(ProjectTemplateTask)
        .filter(ProjectTemplateTask.template_id == template_uuid)
        .all()
    )
    existing_map = {str(task.id): task for task in existing_tasks}
    client_id_to_task_id: dict[str, str] = {}
    kept_task_ids: set[str] = set()

    for index, task_data in enumerate(normalized):
        client_id = task_data["client_id"]
        if client_id in existing_map:
            task = existing_map[client_id]
            task.title = task_data["title"]
            task.description = task_data["description"] or None
            task.sort_order = index
            task.effort_hours = task_data["effort_hours"]
            task.is_active = True
        else:
            task = ProjectTemplateTask(
                template_id=template_uuid,
                title=task_data["title"],
                description=task_data["description"] or None,
                sort_order=index,
                effort_hours=task_data["effort_hours"],
                is_active=True,
            )
            db.add(task)
            db.flush()
        kept_task_ids.add(str(task.id))
        client_id_to_task_id[client_id] = str(task.id)

    for task in existing_tasks:
        if str(task.id) not in kept_task_ids:
            task.is_active = False

    all_task_ids = [coerce_uuid(task_id) for task_id in existing_map] + [
        coerce_uuid(task_id) for task_id in kept_task_ids if task_id not in existing_map
    ]
    if all_task_ids:
        db.query(ProjectTemplateTaskDependency).filter(
            ProjectTemplateTaskDependency.template_task_id.in_(all_task_ids)
        ).delete(synchronize_session=False)

    dependency_pairs: set[tuple[str, str]] = set()
    for task_data in normalized:
        task_id = client_id_to_task_id.get(task_data["client_id"])
        if not task_id:
            continue
        for depends_on_client_id in task_data["dependencies"]:
            depends_on_id = client_id_to_task_id.get(str(depends_on_client_id))
            if not depends_on_id or depends_on_id == task_id:
                continue
            key = (task_id, depends_on_id)
            if key in dependency_pairs:
                continue
            dependency_pairs.add(key)
            db.add(
                ProjectTemplateTaskDependency(
                    template_task_id=coerce_uuid(task_id),
                    depends_on_template_task_id=coerce_uuid(depends_on_id),
                    dependency_type=ProjectTaskDependencyType.finish_to_start.value,
                    lag_days=0,
                )
            )

    db.commit()


def build_template_task_form_context(
    db: Session,
    *,
    template,
    task=None,
    form: dict | None = None,
    error: str | None = None,
) -> dict:
    values = form or {}

    def _value(key: str) -> str:
        if form is not None:
            return str(values.get(key, "") or "")
        if task is not None:
            attr = getattr(task, key, None)
            return str(attr) if attr not in (None, "") else ""
        return ""

    context = {
        "template": template,
        "task": task,
        "prefill": {
            "title": _value("title"),
            "description": _value("description"),
            "sort_order": _value("sort_order"),
            "effort_hours": _value("effort_hours"),
        },
    }
    if error:
        context["error"] = error
    return context


def _template_task_payload_data(**form) -> dict:
    title = str(form.get("title") or "").strip()
    if not title:
        raise ValueError("Title is required.")
    data: dict[str, object] = {
        "title": title,
        "description": str(form.get("description") or "").strip() or None,
    }
    effort_raw = str(form.get("effort_hours") or "").strip()
    if effort_raw:
        try:
            data["effort_hours"] = int(effort_raw)
        except ValueError as exc:
            raise ValueError("Effort hours must be a number.") from exc
    sort_raw = str(form.get("sort_order") or "").strip()
    if sort_raw:
        try:
            data["sort_order"] = int(sort_raw)
        except ValueError as exc:
            raise ValueError("Sort order must be a number.") from exc
    return data


def create_template_task_from_form(db: Session, *, template_id: str, **form):
    data = _template_task_payload_data(**form)
    data["template_id"] = coerce_uuid(template_id)
    payload = ProjectTemplateTaskCreate.model_validate(data)
    return projects_service.project_template_tasks.create(db, payload)


def get_template_task_checked(db: Session, *, template_id: str, task_id: str):
    task = projects_service.project_template_tasks.get(db, task_id)
    if str(task.template_id) != str(coerce_uuid(template_id)):
        raise _projection_error("not_found", "Project template task not found")
    return task


def update_template_task_from_form(
    db: Session, *, template_id: str, task_id: str, **form
):
    get_template_task_checked(db, template_id=template_id, task_id=task_id)
    payload = ProjectTemplateTaskUpdate.model_validate(
        _template_task_payload_data(**form)
    )
    return projects_service.project_template_tasks.update(db, task_id, payload)


def delete_template_task(db: Session, *, template_id: str, task_id: str) -> None:
    get_template_task_checked(db, template_id=template_id, task_id=task_id)
    projects_service.project_template_tasks.delete(db, task_id)


def delete_project_hx_headers() -> dict[str, str]:
    return {
        "HX-Redirect": "/admin/projects",
        "HX-Trigger": json.dumps(
            {
                "showToast": {
                    "type": "success",
                    "title": "Project deleted",
                    "message": "Project was archived.",
                }
            }
        ),
    }
