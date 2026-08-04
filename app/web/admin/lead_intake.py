"""Sales administration adapters for governed Inbox Lead intake templates."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.schemas.lead_intake import LeadIntakePartyType, LeadIntakeTemplateDraft
from app.services.auth_dependencies import require_permission
from app.services.owner_commands import CommandContext
from app.services.sales import lead_intake
from app.web.templates import templates

router = APIRouter(prefix="/sales/lead-intake", tags=["web-admin-lead-intake"])


def _actor(request: Request) -> UUID:
    value = str(getattr(getattr(request.state, "user", None), "id", "") or "")
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=403, detail="An authenticated staff user is required."
        ) from exc


def _context(request: Request, db: Session, **values):
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "lead-intake",
        "active_menu": "sales",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **values,
    }


def _options(db: Session) -> dict[str, object]:
    options = lead_intake.template_form_options(db)
    return {
        "teams": options.teams,
        "owners": options.owners,
        "pipelines": options.pipelines,
        "stages": options.stages,
    }


def _command_context(
    request: Request, template_id: UUID, action: str
) -> CommandContext:
    return CommandContext.system(
        actor=f"system_user:{_actor(request)}",
        scope="sales.lead_intake:write",
        reason=f"{action} Lead intake template",
        idempotency_key=f"lead-intake-template:{action}:{template_id}",
    )


def _draft(**values) -> LeadIntakeTemplateDraft:
    for key in ("owner_system_user_id", "pipeline_id", "stage_id"):
        if values.get(key) == "":
            values[key] = None
    return LeadIntakeTemplateDraft.model_validate(values)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def template_list(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "admin/sales/lead_intake/index.html",
        _context(
            request,
            db,
            rows=lead_intake.list_templates(db),
            rollout=lead_intake.automatic_rollout_status(db),
        ),
    )


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def template_new(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "admin/sales/lead_intake/form.html",
        _context(
            request,
            db,
            template=None,
            values={"template_id": str(uuid4())},
            error=None,
            **_options(db),
        ),
    )


@router.get(
    "/{template_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def template_edit(
    template_id: UUID,
    request: Request,
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    row = lead_intake.get_template(db, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return templates.TemplateResponse(
        "admin/sales/lead_intake/form.html",
        _context(request, db, template=row, values={}, error=error, **_options(db)),
    )


@router.post("", dependencies=[Depends(require_permission("crm:lead:write"))])
@router.post(
    "/{template_id}", dependencies=[Depends(require_permission("crm:lead:write"))]
)
def template_save(
    request: Request,
    template_id: UUID | None = None,
    template_key: str = Form(""),
    party_type: str = Form(...),
    name: str = Form(...),
    heading: str = Form(...),
    introduction: str = Form(""),
    privacy_notice: str = Form(...),
    invitation_message: str = Form(...),
    confirmation_message: str = Form(...),
    thank_you_message: str = Form(...),
    target_service_team_id: str = Form(...),
    owner_system_user_id: str = Form(""),
    pipeline_id: str = Form(""),
    stage_id: str = Form(""),
    db: Session = Depends(get_db),
):
    resolved_id = template_id or UUID(template_key)
    action = (
        lead_intake.TemplateAction.update
        if template_id
        else lead_intake.TemplateAction.create
    )
    values = locals().copy()
    try:
        draft = _draft(
            party_type=LeadIntakePartyType(party_type),
            name=name,
            heading=heading,
            introduction=introduction or None,
            privacy_notice=privacy_notice,
            invitation_message=invitation_message,
            confirmation_message=confirmation_message,
            thank_you_message=thank_you_message,
            target_service_team_id=target_service_team_id,
            owner_system_user_id=owner_system_user_id,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
        )
        finish_read_transaction(db)
        lead_intake.mutate_template(
            db,
            lead_intake.TemplateCommand(
                context=_command_context(request, resolved_id, action.value),
                action=action,
                actor_system_user_id=_actor(request),
                template_id=resolved_id,
                draft=draft,
            ),
        )
    except (ValidationError, ValueError, lead_intake.LeadIntakeError) as exc:
        row = lead_intake.get_template(db, resolved_id) if template_id else None
        return templates.TemplateResponse(
            "admin/sales/lead_intake/form.html",
            _context(
                request,
                db,
                template=row,
                values=values,
                error=getattr(exc, "message", str(exc)),
                **_options(db),
            ),
            status_code=400,
        )
    return RedirectResponse("/admin/sales/lead-intake", status_code=303)


@router.post(
    "/{template_id}/{action}",
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def template_transition(
    template_id: UUID,
    action: lead_intake.TemplateAction,
    request: Request,
    db: Session = Depends(get_db),
):
    if action not in {
        lead_intake.TemplateAction.publish,
        lead_intake.TemplateAction.retire,
    }:
        raise HTTPException(status_code=400, detail="Unsupported action")
    finish_read_transaction(db)
    try:
        lead_intake.mutate_template(
            db,
            lead_intake.TemplateCommand(
                context=_command_context(request, template_id, action.value),
                action=action,
                actor_system_user_id=_actor(request),
                template_id=template_id,
            ),
        )
    except lead_intake.LeadIntakeError as exc:
        return RedirectResponse(
            f"/admin/sales/lead-intake/{template_id}?error={exc.code}", status_code=303
        )
    return RedirectResponse("/admin/sales/lead-intake", status_code=303)
