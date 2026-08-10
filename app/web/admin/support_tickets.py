"""Admin support tickets routes."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.stored_file import StoredFile
from app.services import web_support_ticket_bulk as support_ticket_bulk_service
from app.services import (
    web_support_ticket_bulk_actions as support_ticket_bulk_actions_service,
)
from app.services import web_support_tickets as support_web_service
from app.services.auth_dependencies import can, require_permission
from app.services.file_storage import build_content_disposition, file_uploads
from app.services.list_query import ListQuery
from app.services.object_storage import ObjectNotFoundError
from app.web.request_parsing import parse_json_body

router = APIRouter(prefix="/support/tickets", tags=["web-admin-support-tickets"])
templates = Jinja2Templates(directory="templates")


def _ctx(request: Request, db: Session, active_page: str = "support-tickets") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _ticket_form_error(exc: Exception) -> str:
    """Turn a validation failure into a clean, user-facing form message."""
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            loc = errors[0].get("loc") or ()
            field = str(loc[-1]) if loc else ""
            if field == "title":
                return "A ticket title is required."
            label = field.replace("_", " ").strip().capitalize() or "Value"
            return f"{label}: {errors[0].get('msg', 'is invalid')}"
        return "Invalid input."
    return str(exc)


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    value = (
        current_user.get("actor_id") or current_user.get("subscriber_id")
        if current_user
        else None
    )
    return str(value) if value else None


def _ticket_attachment_response(record: StoredFile) -> StreamingResponse:
    try:
        stream = file_uploads.stream_file(record)
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Attachment not found") from exc
    disposition = build_content_disposition(record.original_filename)
    if (record.content_type or "").startswith(
        "image/"
    ) or record.content_type == "application/pdf":
        disposition = disposition.replace("attachment;", "inline;", 1)
    headers = {"Content-Disposition": disposition}
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers=headers,
    )


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def tickets_list(
    request: Request,
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    ticket_type: str | None = Query(default=None),
    region: str | None = Query(default=None),
    assigned_to_me: bool = Query(default=False),
    project_manager_person_id: str | None = Query(default=None),
    site_coordinator_person_id: str | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
    filters: str | None = Query(default=None),
    sort: Literal["created_at", "updated_at", "due_at", "priority", "status", "number"]
    | None = Query(default=None),
    direction: Literal["asc", "desc"] | None = Query(default=None, alias="dir"),
    order_by: str | None = Query(default=None),
    order_dir: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: str | None = Query(default="25"),
    db: Session = Depends(get_db),
):
    if sort and order_by and sort != order_by:
        raise HTTPException(status_code=422, detail="Conflicting ticket sort fields")
    if direction and order_dir and direction != order_dir:
        raise HTTPException(
            status_code=422, detail="Conflicting ticket sort directions"
        )
    try:
        list_query = support_web_service.build_ticket_list_query(
            search=search,
            status=status,
            ticket_type=ticket_type,
            region=region,
            assigned_to_me=assigned_to_me,
            project_manager_person_id=project_manager_person_id,
            site_coordinator_person_id=site_coordinator_person_id,
            subscriber_id=subscriber_id,
            filters=filters,
            sort_by=sort or order_by or "created_at",
            sort_dir=direction or order_dir or "desc",
            page=page,
            per_page=per_page,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    context = _ctx(request, db)
    state = support_web_service.build_tickets_list_context(
        db,
        list_query=list_query,
        actor_id=_actor_id(request),
        visible_columns_cookie=request.cookies.get("ticket_columns"),
    )
    state["support_ticket_bulk_action_contract"] = (
        support_ticket_bulk_actions_service.build_support_ticket_bulk_action_contract(
            db,
            auth=getattr(request.state, "auth", None) or {},
            tickets=state["tickets"],
        )
    )
    context.update(state)
    effective_query = state["list_query"]
    assert isinstance(effective_query, ListQuery)
    canonicalization_needed = (
        effective_query.page != page
        or order_by is not None
        or order_dir is not None
        or str(per_page or "") != str(effective_query.per_page)
    )

    if request.headers.get("HX-Request"):
        partial_name = (
            "admin/support/tickets/_results.html"
            if request.headers.get("HX-Target") == "tickets-table"
            else "admin/support/tickets/_list.html"
        )
        response = templates.TemplateResponse(partial_name, context)
        if canonicalization_needed:
            response.headers["HX-Replace-Url"] = effective_query.url(
                "/admin/support/tickets"
            )
        return response
    if canonicalization_needed:
        return RedirectResponse(
            url=effective_query.url("/admin/support/tickets"), status_code=307
        )
    return templates.TemplateResponse("admin/support/tickets/index.html", context)


@router.get(
    "/export.csv",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def tickets_export_csv(
    request: Request,
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    ticket_type: str | None = Query(default=None),
    region: str | None = Query(default=None),
    assigned_to_me: bool = Query(default=False),
    project_manager_person_id: str | None = Query(default=None),
    site_coordinator_person_id: str | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
    filters: str | None = Query(default=None),
    sort: Literal["created_at", "updated_at", "due_at", "priority", "status", "number"]
    | None = Query(default=None),
    direction: Literal["asc", "desc"] | None = Query(default=None, alias="dir"),
    order_by: str | None = Query(default=None),
    order_dir: str | None = Query(default=None),
    columns: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if sort and order_by and sort != order_by:
        raise HTTPException(status_code=422, detail="Conflicting ticket sort fields")
    if direction and order_dir and direction != order_dir:
        raise HTTPException(
            status_code=422, detail="Conflicting ticket sort directions"
        )
    try:
        list_query = support_web_service.build_ticket_list_query(
            search=search,
            status=status,
            ticket_type=ticket_type,
            region=region,
            assigned_to_me=assigned_to_me,
            project_manager_person_id=project_manager_person_id,
            site_coordinator_person_id=site_coordinator_person_id,
            subscriber_id=subscriber_id,
            filters=filters,
            sort_by=sort or order_by or "created_at",
            sort_dir=direction or order_dir or "desc",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    content = support_web_service.render_tickets_csv(
        db,
        list_query=list_query,
        actor_id=_actor_id(request),
        visible_columns_cookie=columns or request.cookies.get("ticket_columns"),
    )
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="tickets_export.csv"'},
    )


@router.post(
    "/bulk/preview",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def tickets_bulk_preview(
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    try:
        preview = support_ticket_bulk_service.preview_support_ticket_bulk_update(
            db, data
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return preview.as_response()


@router.post(
    "/bulk/update",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def tickets_bulk_update(
    request: Request,
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    try:
        return support_ticket_bulk_service.execute_support_ticket_bulk_update(
            db,
            data,
            actor_id=_actor_id(request),
            request=request,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:create"))],
)
def ticket_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context.update(
        support_web_service.build_ticket_form_context(
            db, query_params=request.query_params
        )
    )
    context.update({"page_title": "New Ticket", "form_mode": "create", "ticket": None})
    return templates.TemplateResponse("admin/support/tickets/new.html", context)


@router.get(
    "/{ticket_lookup}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_edit_page(
    request: Request, ticket_lookup: str, db: Session = Depends(get_db)
):
    context = _ctx(request, db)
    context.update(
        support_web_service.build_ticket_edit_page_context(
            db,
            query_params=request.query_params,
            ticket_lookup=ticket_lookup,
        )
    )
    return templates.TemplateResponse("admin/support/tickets/new.html", context)


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:create"))],
)
def ticket_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    publish_description: bool = Form(False),
    subscriber_id: str | None = Form(default=None),
    customer_account_id: str | None = Form(default=None),
    customer_person_id: str | None = Form(default=None),
    region: str | None = Form(default=None),
    technician_person_id: str | None = Form(default=None),
    ticket_manager_person_id: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    ticket_type: str | None = Form(default=None),
    priority: str = Form("normal"),
    channel: str = Form("web"),
    status: str = Form("open"),
    due_at: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    related_outage_ticket_id: str | None = Form(default=None),
    assignee_person_ids: list[str] = Form(default=[]),
    attachments: list[UploadFile] = File(default=[]),
    duplicate_override: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    actor_id = _actor_id(request)
    duplicate_confirmed = str(duplicate_override or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        ticket = support_web_service.create_ticket_from_form(
            db,
            request=request,
            actor_id=actor_id,
            attachments=attachments,
            duplicate_override=duplicate_confirmed,
            title=title,
            description=description,
            publish_description=publish_description,
            subscriber_id=subscriber_id,
            customer_account_id=customer_account_id,
            customer_person_id=customer_person_id,
            region=region,
            technician_person_id=technician_person_id,
            ticket_manager_person_id=ticket_manager_person_id,
            site_coordinator_person_id=None,
            service_team_id=service_team_id,
            ticket_type=ticket_type,
            priority=priority,
            channel=channel,
            status=status,
            due_at=due_at,
            tags=tags,
            related_outage_ticket_id=related_outage_ticket_id,
            assignee_person_ids=assignee_person_ids,
        )
    except support_web_service.DuplicateTicketWarningError as exc:
        # Similar open tickets exist and the operator has not confirmed the
        # override — re-render the form with the duplicate warning (409, like
        # CRM's admin create flow) so they can review or tick "Create anyway".
        db.rollback()
        context = _ctx(request, db)
        context.update(
            support_web_service.build_ticket_form_context(
                db,
                query_params={
                    "title": title,
                    "description": description,
                    "publish_description": publish_description,
                    "subscriber_id": subscriber_id or "",
                    "customer_account_id": customer_account_id or "",
                    "customer_person_id": customer_person_id or "",
                    "region": region or "",
                    "technician_person_id": technician_person_id or "",
                    "ticket_manager_person_id": ticket_manager_person_id or "",
                    "service_team_id": service_team_id or "",
                    "ticket_type": ticket_type or "",
                    "priority": priority,
                    "channel": channel,
                    "status": status,
                    "tags": tags or "",
                    "related_outage_ticket_id": related_outage_ticket_id or "",
                },
            )
        )
        context.update(
            {
                "page_title": "New Ticket",
                "form_mode": "create",
                "ticket": None,
                "error": (
                    "A similar ticket already exists. Review the warning below, "
                    "then open the existing ticket or tick Create anyway."
                ),
                "duplicate_warning": exc.result.as_dict(),
            }
        )
        return templates.TemplateResponse(
            "admin/support/tickets/new.html", context, status_code=409
        )
    except (ValidationError, ValueError) as exc:
        # Re-render the form with a clean message instead of a 500 (e.g. a
        # blank/whitespace-only title that fails schema validation).
        db.rollback()
        context = _ctx(request, db)
        context.update(
            support_web_service.build_ticket_form_context(
                db,
                query_params={
                    "title": title,
                    "description": description,
                    "publish_description": publish_description,
                    "subscriber_id": subscriber_id or "",
                    "customer_account_id": customer_account_id or "",
                    "customer_person_id": customer_person_id or "",
                    "region": region or "",
                    "technician_person_id": technician_person_id or "",
                    "ticket_manager_person_id": ticket_manager_person_id or "",
                    "service_team_id": service_team_id or "",
                    "ticket_type": ticket_type or "",
                    "priority": priority,
                    "channel": channel,
                    "status": status,
                    "due_at": due_at or "",
                    "tags": tags or "",
                    "related_outage_ticket_id": related_outage_ticket_id or "",
                },
            )
        )
        context.update(
            {
                "page_title": "New Ticket",
                "form_mode": "create",
                "ticket": None,
                "error": _ticket_form_error(exc),
                "prefill": {
                    **(context.get("prefill") or {}),
                    "title": title,
                    "description": description,
                    "description_is_internal": not publish_description,
                },
            }
        )
        return templates.TemplateResponse(
            "admin/support/tickets/new.html", context, status_code=400
        )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket.id}", status_code=303)


@router.get(
    "/{ticket_lookup}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def ticket_detail(request: Request, ticket_lookup: str, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context.update(
        support_web_service.build_ticket_detail_context(
            db,
            ticket_lookup=ticket_lookup,
            actor_id=_actor_id(request),
            can_read_material_requests=can(request, "operations:material_request:read"),
        )
    )
    context["handoff_notice"] = request.query_params.get("handoff_notice")
    context["handoff_error"] = request.query_params.get("handoff_error")
    return templates.TemplateResponse("admin/support/tickets/detail.html", context)


@router.get(
    "/{ticket_id}/attachments/{file_id}",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def ticket_attachment_download(
    ticket_id: UUID, file_id: UUID, db: Session = Depends(get_db)
) -> StreamingResponse:
    record = support_web_service.get_ticket_attachment_file(
        db, ticket_id=ticket_id, file_id=file_id
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return _ticket_attachment_response(record)


@router.post(
    "/{ticket_id}/work-orders",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("support:ticket:update")),
        Depends(require_permission("operations:dispatch:write")),
    ],
)
def issue_ticket_work_order(
    request: Request,
    ticket_id: UUID,
    reason: str = Form(...),
    idempotency_key: str = Form(...),
    title: str | None = Form(default=None),
    description: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
    project_task_id: str | None = Form(default=None),
    priority: str | None = Form(default=None),
    work_type: str = Form(default="repair"),
    address: str | None = Form(default=None),
    scheduled_start: str | None = Form(default=None),
    scheduled_end: str | None = Form(default=None),
    estimated_duration_minutes: str | None = Form(default=None),
    required_skills: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    access_notes: str | None = Form(default=None),
    requires_as_built_evidence: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    from app.schemas.support import TicketWorkOrderIssueRequest
    from app.services import ticket_work_order_handoff
    from app.services.owner_commands import CommandContext

    def _clean(value: str | None) -> str | None:
        cleaned = (value or "").strip()
        return cleaned or None

    try:
        payload = TicketWorkOrderIssueRequest(
            reason=reason,
            title=_clean(title),
            description=_clean(description),
            project_id=_clean(project_id),
            project_task_id=_clean(project_task_id),
            priority=_clean(priority),
            work_type=work_type,
            address=_clean(address),
            scheduled_start=_clean(scheduled_start),
            scheduled_end=_clean(scheduled_end),
            estimated_duration_minutes=_clean(estimated_duration_minutes),
            required_skills=[
                item.strip()
                for item in (required_skills or "").split(",")
                if item.strip()
            ],
            tags=[item.strip() for item in (tags or "").split(",") if item.strip()],
            access_notes=_clean(access_notes),
            requires_as_built_evidence=str(requires_as_built_evidence or "").lower()
            in {"1", "true", "yes", "on"},
        )
        actor_id = UUID(str(_actor_id(request)))
        auth = getattr(request.state, "auth", None) or {}
        result = ticket_work_order_handoff.issue_work_order(
            db,
            ticket_work_order_handoff.TicketWorkOrderIssueCommand(
                ticket_id=ticket_id,
                request=payload,
                actor_id=actor_id,
                actor_type=ticket_work_order_handoff.HandoffActorType(
                    str(auth.get("principal_type") or "system_user")
                ),
                permissions=frozenset(
                    {"support:ticket:update", "operations:dispatch:write"}
                ),
                context=CommandContext.system(
                    actor=str(actor_id),
                    scope=ticket_work_order_handoff.WORK_ORDER_ISSUE_SCOPE,
                    reason=payload.reason,
                    idempotency_key=idempotency_key,
                ),
                request_id=getattr(request.state, "request_id", None),
            ),
        )
    except (
        ValidationError,
        ValueError,
        ticket_work_order_handoff.TicketWorkOrderHandoffError,
    ) as exc:
        message = (
            exc.message
            if isinstance(exc, ticket_work_order_handoff.TicketWorkOrderHandoffError)
            else _ticket_form_error(exc)
        )
        query = urlencode({"handoff_error": message})
        return RedirectResponse(
            url=f"/admin/support/tickets/{ticket_id}?{query}", status_code=303
        )
    verb = "replayed" if result.replayed else "issued"
    query = urlencode(
        {"handoff_notice": f"Work order {result.work_order.public_id} {verb}"}
    )
    return RedirectResponse(
        url=f"/admin/support/tickets/{ticket_id}?{query}", status_code=303
    )


@router.post(
    "/{ticket_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_edit(
    request: Request,
    ticket_id: UUID,
    title: str = Form(...),
    description: str = Form(""),
    publish_description: bool = Form(False),
    subscriber_id: str | None = Form(default=None),
    customer_account_id: str | None = Form(default=None),
    customer_person_id: str | None = Form(default=None),
    region: str | None = Form(default=None),
    status: str = Form("open"),
    priority: str = Form("normal"),
    channel: str = Form("web"),
    ticket_type: str | None = Form(default=None),
    due_at: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    technician_person_id: str | None = Form(default=None),
    ticket_manager_person_id: str | None = Form(default=None),
    site_coordinator_person_id: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    assignee_person_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    support_web_service.update_ticket_from_form(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=_actor_id(request),
        title=title,
        description=description,
        publish_description=publish_description,
        subscriber_id=subscriber_id,
        customer_account_id=customer_account_id,
        customer_person_id=customer_person_id,
        region=region,
        status=status,
        priority=priority,
        channel=channel,
        ticket_type=ticket_type,
        due_at=due_at,
        tags=tags,
        technician_person_id=technician_person_id,
        ticket_manager_person_id=ticket_manager_person_id,
        site_coordinator_person_id=site_coordinator_person_id,
        service_team_id=service_team_id,
        assignee_person_ids=assignee_person_ids,
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/comment",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_add_comment(
    request: Request,
    ticket_id: UUID,
    body: str = Form(...),
    reply_to_customer: bool = Form(False),
    mentions: str | None = Form(default=None),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    actor_id = _actor_id(request)
    support_web_service.add_ticket_comment_from_form(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=actor_id,
        body=body,
        is_internal=not reply_to_customer,
        mentions=mentions,
        attachments=attachments,
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/comments/{comment_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_edit_comment(
    request: Request,
    ticket_id: UUID,
    comment_id: UUID,
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    if body.strip():
        support_web_service.update_ticket_comment_from_form(
            db,
            request=request,
            ticket_id=str(ticket_id),
            comment_id=str(comment_id),
            actor_id=_actor_id(request),
            body=body,
        )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/auto-assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_auto_assign(
    request: Request, ticket_id: UUID, db: Session = Depends(get_db)
):
    result = support_web_service.auto_assign_ticket(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=_actor_id(request),
    )
    changes = result.get("changes") if isinstance(result, dict) else None
    if result.get("matched") and changes:
        message = f"Auto-assign applied {len(changes)} field(s)."
        status = "success"
    elif result.get("matched"):
        message = "Auto-assign matched, but no empty assignment fields changed."
        status = "info"
    else:
        reason = str(result.get("reason") or "no matching rule").replace("_", " ")
        message = f"Auto-assign did not run: {reason}."
        status = "warning"
    query = urlencode({"auto_assign_status": status, "auto_assign_message": message})
    return RedirectResponse(
        url=f"/admin/support/tickets/{ticket_id}?{query}", status_code=303
    )


@router.post(
    "/{ticket_id}/link",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_link(
    request: Request,
    ticket_id: UUID,
    to_ticket_id: str = Form(...),
    link_type: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        support_web_service.link_ticket_from_form(
            db,
            request=request,
            ticket_id=str(ticket_id),
            to_ticket_id=to_ticket_id,
            link_type=link_type,
            actor_id=_actor_id(request),
        )
    except ValueError as exc:
        db.rollback()
        context = _ctx(request, db)
        context.update(
            support_web_service.build_ticket_detail_context(
                db, ticket_lookup=str(ticket_id)
            )
        )
        context["action_error"] = str(exc)
        return templates.TemplateResponse(
            "admin/support/tickets/detail.html", context, status_code=400
        )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/merge",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_merge(
    request: Request,
    ticket_id: UUID,
    target_ticket_id: str = Form(...),
    reason: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        target = support_web_service.merge_ticket_from_form(
            db,
            request=request,
            ticket_id=str(ticket_id),
            target_ticket_id=target_ticket_id,
            reason=reason,
            actor_id=_actor_id(request),
        )
    except ValueError as exc:
        db.rollback()
        context = _ctx(request, db)
        context.update(
            support_web_service.build_ticket_detail_context(
                db, ticket_lookup=str(ticket_id)
            )
        )
        context["action_error"] = str(exc)
        return templates.TemplateResponse(
            "admin/support/tickets/detail.html", context, status_code=400
        )
    return RedirectResponse(url=f"/admin/support/tickets/{target.id}", status_code=303)


@router.post(
    "/{ticket_id}/quick-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_quick_status(
    request: Request,
    ticket_id: UUID,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    support_web_service.quick_update_ticket(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=_actor_id(request),
        fields={"status": status},
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/quick-assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_quick_assign(
    request: Request,
    ticket_id: UUID,
    technician_person_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    fields: dict[str, str | None] = {
        "technician_person_id": technician_person_id or None,
    }
    support_web_service.quick_update_ticket(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=_actor_id(request),
        fields=fields,
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:delete"))],
)
def ticket_delete(request: Request, ticket_id: UUID, db: Session = Depends(get_db)):
    support_web_service.delete_ticket(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=_actor_id(request),
    )
    if request.headers.get("HX-Request"):
        return Response(
            status_code=204,
            headers=support_web_service.delete_ticket_hx_headers(),
        )
    return RedirectResponse(url="/admin/support/tickets", status_code=303)


# Legacy path compatibility used by existing smoke tests.
legacy_router = APIRouter(prefix="/tickets", tags=["web-admin-support-tickets-legacy"])


@legacy_router.get("", response_class=HTMLResponse)
def legacy_ticket_index():
    return RedirectResponse(url="/admin/support/tickets", status_code=307)


@legacy_router.get("/{ticket_lookup}", response_class=HTMLResponse)
def legacy_ticket_detail(ticket_lookup: str):
    return RedirectResponse(
        url=f"/admin/support/tickets/{ticket_lookup}", status_code=307
    )
