"""Administrator API for Inbox SLA policy configuration."""

from __future__ import annotations

from datetime import date, time
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import inbox_sla
from app.services.audit_adapter import AuditActor
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.inbox_sla import SlaPolicyInput, SlaPolicyView, SlaRuleInput
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/inbox/sla", tags=["admin-inbox-sla"])


class SlaRuleRequest(BaseModel):
    first_response_minutes: int = Field(gt=0)
    resolution_minutes: int = Field(gt=0)
    warning_minutes: int = Field(default=0, ge=0)
    next_response_minutes: int | None = Field(default=None, gt=0)
    service_team_id: UUID | None = None
    channel_type: str | None = None
    priority: int | None = None


class SlaPolicyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    rules: tuple[SlaRuleRequest, ...] = Field(min_length=1)
    timezone: str = "Africa/Lagos"
    working_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    workday_start: time = time(9)
    workday_end: time = time(17)
    holidays: tuple[date, ...] = ()
    is_default: bool = False


def _serialize(policy: SlaPolicyView) -> dict[str, object]:
    return {
        "id": str(policy.id),
        "name": policy.name,
        "description": policy.description,
        "timezone": policy.timezone,
        "working_days": list(policy.working_days),
        "workday_start": policy.workday_start.isoformat(),
        "workday_end": policy.workday_end.isoformat(),
        "holidays": [day.isoformat() for day in policy.holidays],
        "is_active": policy.is_active,
        "is_default": policy.is_default,
        "rules": [
            {
                "id": str(rule.id),
                "service_team_id": str(rule.service_team_id)
                if rule.service_team_id
                else None,
                "channel_type": rule.channel_type,
                "priority": rule.priority,
                "first_response_minutes": rule.first_response_minutes,
                "next_response_minutes": rule.next_response_minutes,
                "resolution_minutes": rule.resolution_minutes,
                "warning_minutes": rule.warning_minutes,
                "is_active": rule.is_active,
            }
            for rule in policy.rules
        ],
    }


def _context(principal_id: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=principal_id,
        scope="support:ticket:update",
        reason="Administrator configured Inbox SLA policy",
    )


def _http_error(error: inbox_sla.InboxSlaError) -> HTTPException:
    return HTTPException(
        status_code=404 if error.code.endswith(".not_found") else 400,
        detail=error.message,
    )


@router.get(
    "/policies", dependencies=[Depends(require_permission("support:ticket:read"))]
)
def list_policies(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [
        _serialize(policy)
        for policy in inbox_sla.query_policies(db, query=inbox_sla.SlaPolicyQuery())
    ]


@router.post("/policies")
def add_policy(
    request: SlaPolicyRequest,
    auth: dict[str, object] = Depends(require_permission("support:ticket:update")),
) -> dict[str, object]:
    principal_id = str(auth["principal_id"])
    command = inbox_sla.SaveSlaPolicyCommand(
        context=_context(principal_id),
        actor=AuditActor.user(principal_id),
        policy=SlaPolicyInput(
            name=request.name,
            description=request.description,
            rules=tuple(
                SlaRuleInput(
                    first_response_minutes=rule.first_response_minutes,
                    resolution_minutes=rule.resolution_minutes,
                    warning_minutes=rule.warning_minutes,
                    next_response_minutes=rule.next_response_minutes,
                    service_team_id=rule.service_team_id,
                    channel_type=rule.channel_type,
                    priority=rule.priority,
                )
                for rule in request.rules
            ),
            timezone=request.timezone,
            working_days=request.working_days,
            workday_start=request.workday_start,
            workday_end=request.workday_end,
            holidays=request.holidays,
            is_default=request.is_default,
        ),
    )
    try:
        with db_session_adapter.owner_command_session() as db:
            return _serialize(inbox_sla.save_policy(db, command=command))
    except inbox_sla.InboxSlaError as error:
        raise _http_error(error) from error


@router.get(
    "/policies/{policy_id}",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_policy(policy_id: UUID, db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        return _serialize(
            inbox_sla.query_policies(
                db, query=inbox_sla.SlaPolicyQuery(policy_id=policy_id)
            )[0]
        )
    except inbox_sla.InboxSlaError as error:
        raise _http_error(error) from error


@router.post("/policies/{policy_id}/active")
def set_policy_active(
    policy_id: UUID,
    active: bool,
    auth: dict[str, object] = Depends(require_permission("support:ticket:update")),
) -> dict[str, object]:
    principal_id = str(auth["principal_id"])
    command = inbox_sla.ActivateSlaPolicyCommand(
        context=_context(principal_id),
        actor=AuditActor.user(principal_id),
        policy_id=policy_id,
        active=active,
    )
    try:
        with db_session_adapter.owner_command_session() as db:
            return _serialize(inbox_sla.activate_policy(db, command=command))
    except inbox_sla.InboxSlaError as error:
        raise _http_error(error) from error
