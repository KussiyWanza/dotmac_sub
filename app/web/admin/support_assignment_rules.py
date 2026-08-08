"""Admin web routes for CRM-style ticket assignment rules."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.support import TicketChannel
from app.models.ticket_workflow import (
    TicketAssignmentStrategy,
    WorkflowEntityType,
)
from app.services import support as support_service
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.ticket_assignment import admin as assignment_admin_service

router = APIRouter(
    prefix="/support/assignment-rules", tags=["web-admin-support-assignment-rules"]
)
templates = Jinja2Templates(directory="templates")

ASSIGNMENT_TARGETS: tuple[str, ...] = (
    "technician",
    "technical_supervisor",
)


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "support-assignment-rules",
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "strategies": [item.value for item in TicketAssignmentStrategy],
        "assignment_targets": list(ASSIGNMENT_TARGETS),
        "entity_types": [item.value for item in WorkflowEntityType],
        "priorities": support_ticket_settings_service.list_priority_options(db),
        "ticket_types": support_ticket_settings_service.list_ticket_type_options(db),
        "regions": support_ticket_settings_service.list_region_options(db),
        "sources": [item.value for item in TicketChannel],
        "team_options": assignment_admin_service.list_team_options(db),
        "staff_options": support_service.list_assignment_people(db),
    }


def _clean_multi(values: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _csv_values(raw: str | None) -> list[str]:
    cleaned: list[str] = []
    for item in str(raw or "").split(","):
        text = item.strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _validate_team_id(db: Session, team_id: str | None) -> str | None:
    cleaned = str(team_id or "").strip()
    if not cleaned:
        return None
    try:
        UUID(cleaned)
    except ValueError as exc:
        raise ValueError("team must be a valid UUID.") from exc
    configured = {item["id"] for item in assignment_admin_service.list_team_options(db)}
    if cleaned not in configured:
        raise ValueError("team must match a configured service team.")
    return cleaned


def _build_match_config(
    *,
    entity_types: list[str],
    priorities: list[str],
    ticket_types: list[str],
    project_types_csv: str | None,
    regions: list[str],
    sources: list[str],
    service_team_ids: list[str],
    tags_any_csv: str | None,
    assignment_target: str | None,
    assignee_person_id: str | None,
) -> dict:
    """Build a TicketAssignmentRule.match_config dict from structured form input."""
    config: dict[str, object] = {}
    for key, values in (
        ("entity_types", _clean_multi(entity_types)),
        ("priorities", _clean_multi(priorities)),
        ("ticket_types", _clean_multi(ticket_types)),
        ("project_types", _csv_values(project_types_csv)),
        ("regions", _clean_multi(regions)),
        ("sources", _clean_multi(sources)),
        ("tags_any", _csv_values(tags_any_csv)),
    ):
        if values:
            config[key] = values

    team_ids = _clean_multi(service_team_ids)
    for team_id in team_ids:
        try:
            UUID(team_id)
        except ValueError as exc:
            raise ValueError("service_team_ids must contain valid UUIDs.") from exc
    if team_ids:
        config["service_team_ids"] = team_ids

    assignee = str(assignee_person_id or "").strip()
    if assignee:
        try:
            UUID(assignee)
        except ValueError as exc:
            raise ValueError("assignee_person_id must be a valid UUID.") from exc
        target = str(assignment_target or "technician").strip() or "technician"
        if target not in ASSIGNMENT_TARGETS:
            raise ValueError("assignment_target is invalid.")
        config["assignee_person_id"] = assignee
        config["assignment_target"] = target
    return config


def _typed_match_config(config: dict[str, object]):
    def strings(key: str) -> tuple[str, ...]:
        values = config.get(key)
        return tuple(str(value) for value in values) if isinstance(values, list) else ()

    assignee = str(config.get("assignee_person_id") or "").strip()
    target = str(config.get("assignment_target") or "").strip()
    return assignment_admin_service.TicketAssignmentRuleMatch(
        entity_types=strings("entity_types"),
        priorities=strings("priorities"),
        ticket_types=strings("ticket_types"),
        project_types=strings("project_types"),
        regions=strings("regions"),
        sources=strings("sources"),
        service_team_ids=tuple(UUID(value) for value in strings("service_team_ids")),
        tags_any=strings("tags_any"),
        assignee_person_id=UUID(assignee) if assignee else None,
        assignment_target=(
            assignment_admin_service.TicketAssignmentTarget(target) if target else None
        ),
    )


def _rule_lookups(db: Session, rules: list) -> dict:
    """Resolve team and assignee labels used by the rules table."""
    team_lookup = {
        item["id"]: item["label"]
        for item in assignment_admin_service.list_team_options(db)
    }
    assignee_ids = []
    for rule in rules:
        config = rule.match_config if isinstance(rule.match_config, dict) else {}
        assignee = str(config.get("assignee_person_id") or "").strip()
        if assignee:
            assignee_ids.append(assignee)
    staff = support_service.list_assignment_people(db, include_ids=assignee_ids)
    return {
        "team_lookup": team_lookup,
        "staff_lookup": {item["id"]: item["label"] for item in staff if item.get("id")},
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:read"))],
)
def assignment_rules_list(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    rules = assignment_admin_service.list_rules(db)
    context["rules"] = rules
    context.update(_rule_lookups(db, rules))
    return templates.TemplateResponse(
        "admin/support/assignment_rules/index.html", context
    )


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:write"))],
)
def assignment_rule_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context.update({"rule": None, "form_mode": "create", "error": None})
    return templates.TemplateResponse(
        "admin/support/assignment_rules/form.html", context
    )


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:write"))],
)
def assignment_rule_create(
    request: Request,
    name: str = Form(...),
    priority: int = Form(0),
    strategy: str = Form(TicketAssignmentStrategy.round_robin.value),
    team_id: str | None = Form(default=None),
    assign_manager: bool = Form(False),
    assign_spc: bool = Form(False),
    is_active: bool = Form(False),
    entity_types: list[str] = Form(default=[]),
    priorities: list[str] = Form(default=[]),
    ticket_types: list[str] = Form(default=[]),
    project_types_csv: str | None = Form(default=None),
    regions: list[str] = Form(default=[]),
    sources: list[str] = Form(default=[]),
    service_team_ids: list[str] = Form(default=[]),
    tags_any_csv: str | None = Form(default=None),
    assignment_target: str = Form("technician"),
    assignee_person_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        match_config = _build_match_config(
            entity_types=entity_types,
            priorities=priorities,
            ticket_types=ticket_types,
            project_types_csv=project_types_csv,
            regions=regions,
            sources=sources,
            service_team_ids=service_team_ids,
            tags_any_csv=tags_any_csv,
            assignment_target=assignment_target,
            assignee_person_id=assignee_person_id,
        )
        validated_team_id = _validate_team_id(db, team_id)
        db_session_adapter.release_read_transaction(db)
        assignment_admin_service.create_rule(
            db,
            name=name,
            priority=priority,
            strategy=strategy,
            match_config=_typed_match_config(match_config),
            team_id=validated_team_id,
            assign_manager=assign_manager,
            assign_spc=assign_spc,
            is_active=is_active,
        )
    except (ValueError, KeyError, DomainError) as exc:
        context = _ctx(request, db)
        context.update(
            {
                "rule": None,
                "form_mode": "create",
                "error": str(exc),
                "form_values": {
                    "name": name,
                    "priority": priority,
                    "strategy": strategy,
                    "team_id": team_id,
                    "assign_manager": assign_manager,
                    "assign_spc": assign_spc,
                    "is_active": is_active,
                    "entity_types": _clean_multi(entity_types),
                    "priorities": _clean_multi(priorities),
                    "ticket_types": _clean_multi(ticket_types),
                    "project_types_csv": project_types_csv,
                    "regions": _clean_multi(regions),
                    "sources": _clean_multi(sources),
                    "service_team_ids": _clean_multi(service_team_ids),
                    "tags_any_csv": tags_any_csv,
                    "assignment_target": assignment_target,
                    "assignee_person_id": assignee_person_id,
                },
            }
        )
        return templates.TemplateResponse(
            "admin/support/assignment_rules/form.html", context, status_code=400
        )
    return RedirectResponse(url="/admin/support/assignment-rules", status_code=303)


@router.get(
    "/{rule_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:write"))],
)
def assignment_rule_edit_page(
    request: Request, rule_id: UUID, db: Session = Depends(get_db)
):
    rule = assignment_admin_service.get_rule(db, str(rule_id))
    context = _ctx(request, db)
    context.update({"rule": rule, "form_mode": "edit", "error": None})
    return templates.TemplateResponse(
        "admin/support/assignment_rules/form.html", context
    )


@router.post(
    "/{rule_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:write"))],
)
def assignment_rule_update(
    request: Request,
    rule_id: UUID,
    name: str = Form(...),
    priority: int = Form(0),
    strategy: str = Form(TicketAssignmentStrategy.round_robin.value),
    team_id: str | None = Form(default=None),
    assign_manager: bool = Form(False),
    assign_spc: bool = Form(False),
    is_active: bool = Form(False),
    entity_types: list[str] = Form(default=[]),
    priorities: list[str] = Form(default=[]),
    ticket_types: list[str] = Form(default=[]),
    project_types_csv: str | None = Form(default=None),
    regions: list[str] = Form(default=[]),
    sources: list[str] = Form(default=[]),
    service_team_ids: list[str] = Form(default=[]),
    tags_any_csv: str | None = Form(default=None),
    assignment_target: str = Form("technician"),
    assignee_person_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        match_config = _build_match_config(
            entity_types=entity_types,
            priorities=priorities,
            ticket_types=ticket_types,
            project_types_csv=project_types_csv,
            regions=regions,
            sources=sources,
            service_team_ids=service_team_ids,
            tags_any_csv=tags_any_csv,
            assignment_target=assignment_target,
            assignee_person_id=assignee_person_id,
        )
        validated_team_id = _validate_team_id(db, team_id)
        db_session_adapter.release_read_transaction(db)
        assignment_admin_service.update_rule(
            db,
            str(rule_id),
            name=name,
            priority=priority,
            strategy=strategy,
            match_config=_typed_match_config(match_config),
            team_id=validated_team_id,
            assign_manager=assign_manager,
            assign_spc=assign_spc,
            is_active=is_active,
        )
    except (ValueError, KeyError, DomainError) as exc:
        rule = assignment_admin_service.get_rule(db, str(rule_id))
        context = _ctx(request, db)
        context.update({"rule": rule, "form_mode": "edit", "error": str(exc)})
        return templates.TemplateResponse(
            "admin/support/assignment_rules/form.html", context, status_code=400
        )
    return RedirectResponse(url="/admin/support/assignment-rules", status_code=303)


@router.post(
    "/{rule_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:write"))],
)
def assignment_rule_delete(rule_id: UUID, db: Session = Depends(get_db)):
    db_session_adapter.release_read_transaction(db)
    assignment_admin_service.delete_rule(db, str(rule_id))
    return RedirectResponse(url="/admin/support/assignment-rules", status_code=303)


@router.post(
    "/{rule_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:write"))],
)
def assignment_rule_toggle(
    rule_id: UUID,
    target: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Toggle a rule's active state.

    If `target=on` or `target=off` is supplied, the call is idempotent
    (double-click won't flip back). Otherwise falls back to legacy flip.
    """
    db_session_adapter.release_read_transaction(db)
    if target in ("on", "off"):
        assignment_admin_service.set_rule_active(
            db, str(rule_id), is_active=(target == "on")
        )
    else:
        assignment_admin_service.toggle_rule(db, str(rule_id))
    return RedirectResponse(url="/admin/support/assignment-rules", status_code=303)
