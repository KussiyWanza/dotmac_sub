"""Typed owner commands for Ticket automation-rule configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
    TicketAutomationRule,
    TicketStatus,
    parse_ticket_status,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)

TICKET_CONDITION_FIELDS: tuple[str, ...] = (
    "status",
    "priority",
    "channel",
    "ticket_type",
    "region",
)
_OWNER = "support.ticket_automation_rule_configuration"
_CONCERN = "ticket automation-rule configuration"


class TicketAutomationRuleError(DomainError):
    """Transport-neutral automation-rule configuration error."""


@dataclass(frozen=True)
class TicketAutomationConditions:
    status: TicketStatus | None = None
    priority: str | None = None
    channel: str | None = None
    ticket_type: str | None = None
    region: str | None = None

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, object] | None
    ) -> TicketAutomationConditions:
        values = raw or {}
        status_value = values.get("status")
        return cls(
            status=(
                parse_ticket_status(str(status_value).strip())
                if status_value not in (None, "")
                else None
            ),
            priority=_optional_text(values.get("priority")),
            channel=_optional_text(values.get("channel")),
            ticket_type=_optional_text(values.get("ticket_type")),
            region=_optional_text(values.get("region")),
        )

    def as_dict(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for field in TICKET_CONDITION_FIELDS:
            value = getattr(self, field)
            if value is None:
                continue
            result[field] = (
                parse_ticket_status(value).value if field == "status" else str(value)
            )
        return result


@dataclass(frozen=True)
class TicketAutomationAction:
    service_team_id: UUID | None = None
    technician_person_id: UUID | None = None
    priority: str | None = None
    status: TicketStatus | None = None
    hours: int | None = None
    tag: str | None = None

    @classmethod
    def from_mapping(
        cls,
        action_type: AutomationActionType,
        raw: Mapping[str, object] | None,
    ) -> TicketAutomationAction:
        values = raw or {}
        if action_type is AutomationActionType.assign_team:
            return cls(service_team_id=_optional_uuid(values.get("service_team_id")))
        if action_type is AutomationActionType.assign_technician:
            return cls(
                technician_person_id=_optional_uuid(values.get("technician_person_id"))
            )
        if action_type is AutomationActionType.set_priority:
            return cls(priority=_optional_text(values.get("priority")))
        if action_type is AutomationActionType.set_status:
            status = _optional_text(values.get("status"))
            return cls(status=parse_ticket_status(status) if status else None)
        if action_type is AutomationActionType.set_due_in_hours:
            return cls(hours=_optional_hours(values.get("hours")))
        if action_type is AutomationActionType.add_tag:
            return cls(tag=_optional_text(values.get("tag")))
        raise TicketAutomationRuleError(
            code="automation_rule_invalid",
            message="Unsupported automation action type",
        )

    def as_dict(self) -> dict[str, str | int]:
        result: dict[str, str | int] = {}
        for field in (
            "service_team_id",
            "technician_person_id",
            "priority",
            "status",
            "hours",
            "tag",
        ):
            value = getattr(self, field)
            if value is not None:
                if field == "status":
                    result[field] = parse_ticket_status(value).value
                else:
                    result[field] = str(value) if isinstance(value, UUID) else value
        return result


def _optional_uuid(value: object | None) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError as exc:
        raise TicketAutomationRuleError(
            code="automation_rule_invalid",
            message="Automation action identifier must be a UUID",
        ) from exc


def _optional_text(value: object | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _optional_hours(value: object | None) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TicketAutomationRuleError(
            code="automation_rule_invalid",
            message="Automation due hours must be an integer",
        )
    try:
        return int(value)
    except ValueError as exc:
        raise TicketAutomationRuleError(
            code="automation_rule_invalid",
            message="Automation due hours must be an integer",
        ) from exc


def _configuration_command(name: str):
    definition = OwnerCommandDefinition(owner=_OWNER, concern=_CONCERN, name=name)

    def decorate(operation):
        @wraps(operation)
        def wrapped(db: Session, *args, **kwargs):
            if owner_command_active(db, owner=_OWNER):
                return operation(db, *args, **kwargs)
            if not owner_command_active(db):
                from app.services.db_session_adapter import db_session_adapter

                db_session_adapter.release_read_transaction(db)
            context = CommandContext.system(
                actor="support-automation-admin",
                scope=f"support.ticket_automation_rule:{name}",
                reason=f"change Ticket automation rule via {name}",
            )

            def apply():
                result = operation(db, *args, **kwargs)
                entity_id = getattr(result, "id", None)
                if entity_id is None and args:
                    entity_id = args[0]
                stage_audit_event(
                    db,
                    action="ticket.automation_rule_changed",
                    entity_type="ticket_automation_rule",
                    entity_id=str(entity_id) if entity_id else None,
                    actor_type=AuditActorType.system,
                    metadata={"owner": _OWNER, "operation": name},
                )
                return result

            return execute_owner_command(
                db, definition=definition, context=context, operation=apply
            )

        return wrapped

    return decorate


def list_rules(db: Session) -> list[TicketAutomationRule]:
    stmt = select(TicketAutomationRule).order_by(
        TicketAutomationRule.sort_order, TicketAutomationRule.created_at
    )
    return list(db.scalars(stmt).all())


def get_rule(db: Session, rule_id: str | UUID) -> TicketAutomationRule:
    rule = db.get(TicketAutomationRule, rule_id)
    if rule is None:
        raise TicketAutomationRuleError(
            code="automation_rule_not_found",
            message="Automation rule not found",
        )
    return rule


@_configuration_command("create_automation_rule")
def create_rule(
    db: Session,
    *,
    name: str,
    trigger: AutomationTrigger,
    action_type: AutomationActionType,
    conditions: TicketAutomationConditions | None = None,
    action_value: TicketAutomationAction | None = None,
    description: str | None = None,
    sort_order: int = 100,
    is_active: bool = True,
) -> TicketAutomationRule:
    rule = TicketAutomationRule(
        name=name.strip(),
        description=(description or "").strip() or None,
        trigger=trigger,
        action_type=action_type,
        conditions=(conditions or TicketAutomationConditions()).as_dict(),
        action_value=(action_value or TicketAutomationAction()).as_dict(),
        sort_order=sort_order,
        is_active=is_active,
    )
    db.add(rule)
    db.flush()
    return rule


@_configuration_command("update_automation_rule")
def update_rule(
    db: Session,
    rule_id: str | UUID,
    *,
    name: str,
    trigger: AutomationTrigger,
    action_type: AutomationActionType,
    conditions: TicketAutomationConditions | None,
    action_value: TicketAutomationAction | None,
    description: str | None,
    sort_order: int,
    is_active: bool,
) -> TicketAutomationRule:
    rule = get_rule(db, rule_id)
    rule.name = name.strip()
    rule.description = (description or "").strip() or None
    rule.trigger = trigger
    rule.action_type = action_type
    rule.conditions = (conditions or TicketAutomationConditions()).as_dict()
    rule.action_value = (action_value or TicketAutomationAction()).as_dict()
    rule.sort_order = sort_order
    rule.is_active = is_active
    rule.updated_at = datetime.now(UTC)
    db.flush()
    return rule


@_configuration_command("delete_automation_rule")
def delete_rule(db: Session, rule_id: str | UUID) -> None:
    rule = get_rule(db, rule_id)
    db.delete(rule)
    db.flush()


@_configuration_command("set_automation_rule_active")
def set_rule_active(
    db: Session, rule_id: str | UUID, *, is_active: bool
) -> TicketAutomationRule:
    rule = get_rule(db, rule_id)
    if rule.is_active != is_active:
        rule.is_active = is_active
        rule.updated_at = datetime.now(UTC)
        db.flush()
    return rule


@_configuration_command("toggle_automation_rule")
def toggle_rule(db: Session, rule_id: str | UUID) -> TicketAutomationRule:
    rule = get_rule(db, rule_id)
    return set_rule_active(db, rule_id, is_active=not rule.is_active)
