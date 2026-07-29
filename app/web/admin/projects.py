"""Native admin project routes ported from CRM.

Thin routes over ``app.services.web_projects`` context builders, following
the ``support_tickets``/``dispatch_work_orders`` house idiom. Static paths
(``/tasks``, ``/templates``, ``/new``, ``/export.csv``) are declared before
the ``/{project_ref}`` detail routes so they match first.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_projects as projects_web_service
from app.services.auth_dependencies import can, require_permission
from app.services.domain_errors import DomainError


class ProjectDomainRoute(APIRoute):
    """Map uncaught Projects domain errors at the web adapter boundary."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except DomainError as exc:
                code = exc.code
                status_code = (
                    404
                    if code.endswith(".not_found")
                    else 403
                    if code.endswith(".unauthorized")
                    else 409
                    if code.endswith((".stale_state", ".relationship_conflict"))
                    else 400
                )
                raise HTTPException(
                    status_code=status_code, detail=exc.message
                ) from exc

        return handler


router = APIRouter(
    prefix="/projects", tags=["web-admin-projects"], route_class=ProjectDomainRoute
)
templates = Jinja2Templates(directory="templates")


def _ctx(request: Request, db: Session, active_page: str = "projects") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    value = (
        current_user.get("actor_id") or current_user.get("subscriber_id")
        if current_user
        else None
    )
    return str(value) if value else None


def _form_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            loc = errors[0].get("loc") or ()
            field = str(loc[-1]) if loc else ""
            label = field.replace("_", " ").strip().capitalize() or "Value"
            return f"{label}: {errors[0].get('msg', 'is invalid')}"
        return "Invalid input."
    detail = getattr(exc, "detail", None)
    return str(detail or exc)


# ── projects list / export ───────────────────────────────────────────────────


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:read"))],
)
def projects_list(
    request: Request,
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    project_type: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    region: str | None = Query(default=None),
    filters: str | None = Query(default=None),
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db)
    context.update(
        projects_web_service.build_projects_list_context(
            db,
            search=search,
            status=status,
            project_type=project_type,
            priority=priority,
            region=region,
            filters=filters,
            order_by=order_by,
            order_dir=order_dir,
            page=page,
            per_page=per_page,
        )
    )
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("admin/projects/_table.html", context)
    return templates.TemplateResponse("admin/projects/index.html", context)


@router.get(
    "/export.csv",
    dependencies=[Depends(require_permission("project:read"))],
)
def projects_export_csv(
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    project_type: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    region: str | None = Query(default=None),
    filters: str | None = Query(default=None),
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    columns: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    content = projects_web_service.render_projects_csv(
        db,
        search=search,
        status=status,
        project_type=project_type,
        priority=priority,
        region=region,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        columns=columns,
    )
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="projects_export.csv"'},
    )


# ── project create ───────────────────────────────────────────────────────────


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:create"))],
)
def project_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context.update(projects_web_service.build_project_form_context(db))
    context.update({"page_title": "New Project", "form_mode": "create"})
    return templates.TemplateResponse("admin/projects/project_form.html", context)


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:create"))],
)
async def project_create(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    try:
        project = projects_web_service.create_project_from_form(
            db, request=request, actor_id=_actor_id(request), **form
        )
    except (HTTPException, DomainError, ValidationError, ValueError) as exc:
        db.rollback()
        context = _ctx(request, db)
        context.update(
            projects_web_service.build_project_form_context(
                db, form=form, error=_form_error(exc)
            )
        )
        context.update({"page_title": "New Project", "form_mode": "create"})
        return templates.TemplateResponse(
            "admin/projects/project_form.html", context, status_code=400
        )
    return RedirectResponse(
        url=projects_web_service.project_url(project), status_code=303
    )


# ── project tasks (static paths before /{project_ref}) ──────────────────────


@router.get(
    "/tasks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:task:read"))],
)
def project_tasks_list(
    request: Request,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    assigned: str | None = Query(default=None),
    filters: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, active_page="project-tasks")
    context.update(
        projects_web_service.build_tasks_list_context(
            db,
            project_id=project_id,
            status=status,
            priority=priority,
            assigned_to_me=(assigned == "me"),
            actor_id=_actor_id(request),
            filters=filters,
            page=page,
            per_page=per_page,
            can_read_work_orders=can(request, "operations:dispatch:read"),
        )
    )
    context["assigned"] = assigned or ""
    return templates.TemplateResponse("admin/projects/tasks.html", context)


@router.get(
    "/tasks/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:task:write"))],
)
def project_task_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, active_page="project-tasks")
    context.update(
        projects_web_service.build_task_form_context(
            db, form=dict(request.query_params) if request.query_params else None
        )
    )
    context.update({"page_title": "New Task", "form_mode": "create"})
    return templates.TemplateResponse("admin/projects/project_task_form.html", context)


@router.post(
    "/tasks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:task:write"))],
)
async def project_task_create(request: Request, db: Session = Depends(get_db)):
    raw_form = await request.form()
    form: dict[str, object] = dict(raw_form)
    form["assigned_to_person_ids"] = [
        item
        for item in (
            raw_form.getlist("assigned_to_person_ids[]")
            or raw_form.getlist("assigned_to_person_ids")
        )
        if isinstance(item, str) and item
    ]
    try:
        task = projects_web_service.create_task_from_form(
            db, request=request, actor_id=_actor_id(request), **form
        )
    except (HTTPException, DomainError, ValidationError, ValueError) as exc:
        db.rollback()
        context = _ctx(request, db, active_page="project-tasks")
        context.update(
            projects_web_service.build_task_form_context(
                db, form=form, error=_form_error(exc)
            )
        )
        context.update({"page_title": "New Task", "form_mode": "create"})
        return templates.TemplateResponse(
            "admin/projects/project_task_form.html", context, status_code=400
        )
    return RedirectResponse(url=projects_web_service.task_url(task), status_code=303)


@router.get(
    "/tasks/{task_ref}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:task:read"))],
)
def project_task_detail(request: Request, task_ref: str, db: Session = Depends(get_db)):
    task, should_redirect = projects_web_service.resolve_task_reference(db, task_ref)
    if should_redirect:
        return RedirectResponse(
            url=f"/admin/projects/tasks/{task.number}", status_code=302
        )
    context = _ctx(request, db, active_page="project-tasks")
    context.update(
        projects_web_service.build_task_detail_context(
            db,
            task=task,
            can_read_work_orders=can(request, "operations:dispatch:read"),
        )
    )
    return templates.TemplateResponse(
        "admin/projects/project_task_detail.html", context
    )


@router.get(
    "/tasks/{task_ref}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:task:write"))],
)
def project_task_edit(request: Request, task_ref: str, db: Session = Depends(get_db)):
    task, should_redirect = projects_web_service.resolve_task_reference(db, task_ref)
    if should_redirect:
        return RedirectResponse(
            url=f"/admin/projects/tasks/{task.number}/edit", status_code=302
        )
    context = _ctx(request, db, active_page="project-tasks")
    context.update(projects_web_service.build_task_form_context(db, task=task))
    context.update({"page_title": "Edit Task", "form_mode": "edit"})
    return templates.TemplateResponse("admin/projects/project_task_form.html", context)


@router.post(
    "/tasks/{task_ref}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:task:write"))],
)
async def project_task_update(
    request: Request, task_ref: str, db: Session = Depends(get_db)
):
    task, _ = projects_web_service.resolve_task_reference(db, task_ref)
    raw_form = await request.form()
    form: dict[str, object] = dict(raw_form)
    form["assigned_to_person_ids"] = [
        item
        for item in (
            raw_form.getlist("assigned_to_person_ids[]")
            or raw_form.getlist("assigned_to_person_ids")
        )
        if isinstance(item, str) and item
    ]
    try:
        task = projects_web_service.update_task_from_form(
            db,
            request=request,
            task_id=str(task.id),
            actor_id=_actor_id(request),
            **form,
        )
    except (HTTPException, DomainError, ValidationError, ValueError) as exc:
        db.rollback()
        context = _ctx(request, db, active_page="project-tasks")
        context.update(
            projects_web_service.build_task_form_context(
                db, task=task, form=form, error=_form_error(exc)
            )
        )
        context.update({"page_title": "Edit Task", "form_mode": "edit"})
        return templates.TemplateResponse(
            "admin/projects/project_task_form.html", context, status_code=400
        )
    return RedirectResponse(url=projects_web_service.task_url(task), status_code=303)


@router.post(
    "/tasks/{task_ref}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:task:write"))],
)
def project_task_quick_status(
    request: Request,
    task_ref: str,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    task, _ = projects_web_service.resolve_task_reference(db, task_ref)
    projects_web_service.quick_update_task_status(
        db,
        request=request,
        task_id=str(task.id),
        actor_id=_actor_id(request),
        status=status,
    )
    return RedirectResponse(url=projects_web_service.task_url(task), status_code=303)


@router.post(
    "/tasks/{task_ref}/comments",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:task:write"))],
)
async def project_task_comment_create(
    request: Request, task_ref: str, db: Session = Depends(get_db)
):
    task, _ = projects_web_service.resolve_task_reference(db, task_ref)
    form = await request.form()
    body = str(form.get("body") or "").strip()
    if body:
        projects_web_service.add_task_comment_from_form(
            db,
            request=request,
            task_id=str(task.id),
            actor_id=_actor_id(request),
            body=body,
            attachments=form.getlist("attachments"),
            mentions=str(form.get("mentions") or ""),
        )
    return RedirectResponse(url=projects_web_service.task_url(task), status_code=303)


@router.post(
    "/tasks/{task_ref}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:task:write"))],
)
def project_task_delete(request: Request, task_ref: str, db: Session = Depends(get_db)):
    task, _ = projects_web_service.resolve_task_reference(db, task_ref)
    projects_web_service.delete_task(
        db, request=request, task_id=str(task.id), actor_id=_actor_id(request)
    )
    return RedirectResponse(url="/admin/projects/tasks", status_code=303)


# ── project templates admin ──────────────────────────────────────────────────


@router.get(
    "/templates",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:read"))],
)
def project_templates_list(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, active_page="project-templates")
    context.update(projects_web_service.build_templates_list_context(db))
    return templates.TemplateResponse("admin/projects/project_templates.html", context)


@router.get(
    "/templates/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_template_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, active_page="project-templates")
    context.update(projects_web_service.build_template_form_context(db))
    context.update({"page_title": "New Template", "form_mode": "create"})
    return templates.TemplateResponse(
        "admin/projects/project_template_form.html", context
    )


@router.post(
    "/templates",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
async def project_template_create(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    try:
        template = projects_web_service.create_template_from_form(db, **form)
    except (HTTPException, DomainError, ValidationError, ValueError) as exc:
        db.rollback()
        context = _ctx(request, db, active_page="project-templates")
        context.update(
            projects_web_service.build_template_form_context(
                db, form=form, error=_form_error(exc)
            )
        )
        context.update({"page_title": "New Template", "form_mode": "create"})
        return templates.TemplateResponse(
            "admin/projects/project_template_form.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/projects/templates/{template.id}", status_code=303
    )


@router.get(
    "/templates/{template_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:read"))],
)
def project_template_detail(
    request: Request, template_id: str, db: Session = Depends(get_db)
):
    context = _ctx(request, db, active_page="project-templates")
    context.update(
        projects_web_service.build_template_detail_context(db, template_id=template_id)
    )
    return templates.TemplateResponse(
        "admin/projects/project_template_detail.html", context
    )


@router.get(
    "/templates/{template_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_template_edit(
    request: Request, template_id: str, db: Session = Depends(get_db)
):
    from app.services import projects as projects_service

    template = projects_service.project_templates.get(db, template_id)
    context = _ctx(request, db, active_page="project-templates")
    context.update(
        projects_web_service.build_template_form_context(db, template=template)
    )
    context.update({"page_title": "Edit Template", "form_mode": "edit"})
    return templates.TemplateResponse(
        "admin/projects/project_template_form.html", context
    )


@router.post(
    "/templates/{template_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
async def project_template_update(
    request: Request, template_id: str, db: Session = Depends(get_db)
):
    from app.services import projects as projects_service

    form = dict(await request.form())
    try:
        projects_web_service.update_template_from_form(
            db, template_id=template_id, **form
        )
    except (HTTPException, DomainError, ValidationError, ValueError) as exc:
        db.rollback()
        template = projects_service.project_templates.get(db, template_id)
        context = _ctx(request, db, active_page="project-templates")
        context.update(
            projects_web_service.build_template_form_context(
                db, template=template, form=form, error=_form_error(exc)
            )
        )
        context.update({"page_title": "Edit Template", "form_mode": "edit"})
        return templates.TemplateResponse(
            "admin/projects/project_template_form.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/projects/templates/{template_id}", status_code=303
    )


@router.post(
    "/templates/{template_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_template_delete(template_id: str, db: Session = Depends(get_db)):
    from app.services import projects as projects_service

    projects_service.project_templates.delete(db, template_id)
    return RedirectResponse(url="/admin/projects/templates", status_code=303)


@router.get(
    "/templates/{template_id}/tasks/editor",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_template_tasks_editor(
    request: Request, template_id: str, db: Session = Depends(get_db)
):
    from app.services import projects as projects_service

    template = projects_service.project_templates.get(db, template_id)
    context = _ctx(request, db, active_page="project-templates")
    context.update(
        {
            "template": template,
            "tasks_payload": projects_web_service.build_template_tasks_editor_payload(
                db, template_id
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/projects/project_template_tasks_editor.html", context
    )


@router.post(
    "/templates/{template_id}/tasks/editor",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
async def project_template_tasks_editor_update(
    request: Request, template_id: str, db: Session = Depends(get_db)
):
    from app.services import projects as projects_service

    form = await request.form()
    tasks_json = form.get("tasks_json")
    try:
        projects_web_service.save_template_tasks_from_editor(
            db,
            template_id=template_id,
            tasks_json=tasks_json if isinstance(tasks_json, str) else "",
        )
    except (ValidationError, ValueError) as exc:
        db.rollback()
        template = projects_service.project_templates.get(db, template_id)
        context = _ctx(request, db, active_page="project-templates")
        context.update(
            {
                "template": template,
                "tasks_payload": (
                    projects_web_service.build_template_tasks_editor_payload(
                        db, template_id
                    )
                ),
                "error": _form_error(exc),
            }
        )
        return templates.TemplateResponse(
            "admin/projects/project_template_tasks_editor.html",
            context,
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/projects/templates/{template_id}", status_code=303
    )


@router.get(
    "/templates/{template_id}/tasks/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_template_task_new(
    request: Request, template_id: str, db: Session = Depends(get_db)
):
    from app.services import projects as projects_service

    template = projects_service.project_templates.get(db, template_id)
    context = _ctx(request, db, active_page="project-templates")
    context.update(
        projects_web_service.build_template_task_form_context(db, template=template)
    )
    context.update({"page_title": "New Template Task", "form_mode": "create"})
    return templates.TemplateResponse(
        "admin/projects/project_template_task_form.html", context
    )


@router.post(
    "/templates/{template_id}/tasks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
async def project_template_task_create(
    request: Request, template_id: str, db: Session = Depends(get_db)
):
    from app.services import projects as projects_service

    form = dict(await request.form())
    try:
        projects_web_service.create_template_task_from_form(
            db, template_id=template_id, **form
        )
    except (HTTPException, DomainError, ValidationError, ValueError) as exc:
        db.rollback()
        template = projects_service.project_templates.get(db, template_id)
        context = _ctx(request, db, active_page="project-templates")
        context.update(
            projects_web_service.build_template_task_form_context(
                db, template=template, form=form, error=_form_error(exc)
            )
        )
        context.update({"page_title": "New Template Task", "form_mode": "create"})
        return templates.TemplateResponse(
            "admin/projects/project_template_task_form.html",
            context,
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/projects/templates/{template_id}", status_code=303
    )


@router.get(
    "/templates/{template_id}/tasks/{task_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_template_task_edit(
    request: Request, template_id: str, task_id: str, db: Session = Depends(get_db)
):
    from app.services import projects as projects_service

    template = projects_service.project_templates.get(db, template_id)
    task = projects_web_service.get_template_task_checked(
        db, template_id=template_id, task_id=task_id
    )
    context = _ctx(request, db, active_page="project-templates")
    context.update(
        projects_web_service.build_template_task_form_context(
            db, template=template, task=task
        )
    )
    context.update({"page_title": "Edit Template Task", "form_mode": "edit"})
    return templates.TemplateResponse(
        "admin/projects/project_template_task_form.html", context
    )


@router.post(
    "/templates/{template_id}/tasks/{task_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
async def project_template_task_update(
    request: Request, template_id: str, task_id: str, db: Session = Depends(get_db)
):
    from app.services import projects as projects_service

    form = dict(await request.form())
    try:
        projects_web_service.update_template_task_from_form(
            db, template_id=template_id, task_id=task_id, **form
        )
    except (ValidationError, ValueError) as exc:
        db.rollback()
        template = projects_service.project_templates.get(db, template_id)
        task = projects_web_service.get_template_task_checked(
            db, template_id=template_id, task_id=task_id
        )
        context = _ctx(request, db, active_page="project-templates")
        context.update(
            projects_web_service.build_template_task_form_context(
                db, template=template, task=task, form=form, error=_form_error(exc)
            )
        )
        context.update({"page_title": "Edit Template Task", "form_mode": "edit"})
        return templates.TemplateResponse(
            "admin/projects/project_template_task_form.html",
            context,
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/projects/templates/{template_id}", status_code=303
    )


@router.post(
    "/templates/{template_id}/tasks/{task_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_template_task_delete(
    template_id: str, task_id: str, db: Session = Depends(get_db)
):
    projects_web_service.delete_template_task(
        db, template_id=template_id, task_id=task_id
    )
    return RedirectResponse(
        url=f"/admin/projects/templates/{template_id}", status_code=303
    )


# ── project detail / edit / actions (dynamic ref — declared last) ────────────


@router.get(
    "/{project_ref}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:read"))],
)
def project_detail(request: Request, project_ref: str, db: Session = Depends(get_db)):
    project, should_redirect = projects_web_service.resolve_project_reference(
        db, project_ref
    )
    if should_redirect:
        # Canonical number URL; legacy email deep links arrive with the UUID.
        return RedirectResponse(
            url=f"/admin/projects/{project.number}", status_code=302
        )
    context = _ctx(request, db)
    context.update(
        projects_web_service.build_project_detail_context(
            db,
            project=project,
            can_read_work_orders=can(request, "operations:dispatch:read"),
            can_read_vendor_operations=can(request, "inventory:read"),
            can_read_vendor_routes=can(request, "network:fiber:read"),
            can_read_vendor_financials=can(request, "finance:ap:read"),
        )
    )
    return templates.TemplateResponse("admin/projects/project_detail.html", context)


@router.get(
    "/{project_ref}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_edit(request: Request, project_ref: str, db: Session = Depends(get_db)):
    project, should_redirect = projects_web_service.resolve_project_reference(
        db, project_ref
    )
    if should_redirect:
        return RedirectResponse(
            url=f"/admin/projects/{project.number}/edit", status_code=302
        )
    context = _ctx(request, db)
    context.update(projects_web_service.build_project_form_context(db, project=project))
    context.update({"page_title": "Edit Project", "form_mode": "edit"})
    return templates.TemplateResponse("admin/projects/project_form.html", context)


@router.post(
    "/{project_ref}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
async def project_update(
    request: Request, project_ref: str, db: Session = Depends(get_db)
):
    project, _ = projects_web_service.resolve_project_reference(db, project_ref)
    form = dict(await request.form())
    try:
        project = projects_web_service.update_project_from_form(
            db,
            request=request,
            project_id=str(project.id),
            actor_id=_actor_id(request),
            **form,
        )
    except (HTTPException, DomainError, ValidationError, ValueError) as exc:
        db.rollback()
        context = _ctx(request, db)
        context.update(
            projects_web_service.build_project_form_context(
                db, project=project, form=form, error=_form_error(exc)
            )
        )
        context.update({"page_title": "Edit Project", "form_mode": "edit"})
        return templates.TemplateResponse(
            "admin/projects/project_form.html", context, status_code=400
        )
    return RedirectResponse(
        url=projects_web_service.project_url(project), status_code=303
    )


@router.post(
    "/{project_ref}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_quick_status(
    request: Request,
    project_ref: str,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    project, _ = projects_web_service.resolve_project_reference(db, project_ref)
    projects_web_service.quick_update_project(
        db,
        request=request,
        project_id=str(project.id),
        actor_id=_actor_id(request),
        field="status",
        value=status,
    )
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            content="",
            headers={"HX-Redirect": projects_web_service.project_url(project)},
        )
    return RedirectResponse(
        url=projects_web_service.project_url(project), status_code=303
    )


@router.post(
    "/{project_ref}/priority",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_quick_priority(
    request: Request,
    project_ref: str,
    priority: str = Form(...),
    db: Session = Depends(get_db),
):
    project, _ = projects_web_service.resolve_project_reference(db, project_ref)
    projects_web_service.quick_update_project(
        db,
        request=request,
        project_id=str(project.id),
        actor_id=_actor_id(request),
        field="priority",
        value=priority,
    )
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            content="",
            headers={"HX-Redirect": projects_web_service.project_url(project)},
        )
    return RedirectResponse(
        url=projects_web_service.project_url(project), status_code=303
    )


@router.post(
    "/{project_ref}/comments",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
async def project_comment_create(
    request: Request, project_ref: str, db: Session = Depends(get_db)
):
    project, _ = projects_web_service.resolve_project_reference(db, project_ref)
    form = await request.form()
    body = str(form.get("body") or "").strip()
    if body:
        projects_web_service.add_project_comment_from_form(
            db,
            request=request,
            project_id=str(project.id),
            actor_id=_actor_id(request),
            body=body,
            attachments=form.getlist("attachments"),
            mentions=str(form.get("mentions") or ""),
        )
    return RedirectResponse(
        url=projects_web_service.project_url(project), status_code=303
    )


@router.post(
    "/{project_ref}/comments/{comment_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:update"))],
)
def project_comment_edit(
    request: Request,
    project_ref: str,
    comment_id: str,
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    project, _ = projects_web_service.resolve_project_reference(db, project_ref)
    if body.strip():
        projects_web_service.update_project_comment_from_form(
            db,
            request=request,
            project_id=str(project.id),
            comment_id=comment_id,
            actor_id=_actor_id(request),
            body=body.strip(),
        )
    return RedirectResponse(
        url=projects_web_service.project_url(project), status_code=303
    )


@router.post(
    "/{project_ref}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("project:delete"))],
)
def project_delete(request: Request, project_ref: str, db: Session = Depends(get_db)):
    project, _ = projects_web_service.resolve_project_reference(db, project_ref)
    projects_web_service.delete_project(
        db, request=request, project_id=str(project.id), actor_id=_actor_id(request)
    )
    if request.headers.get("HX-Request"):
        return Response(
            status_code=204,
            headers=projects_web_service.delete_project_hx_headers(),
        )
    return RedirectResponse(url="/admin/projects", status_code=303)
