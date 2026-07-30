from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.domain_settings import SettingDomain
from app.models.service_team import ServiceTeam
from app.models.subscription_engine import SettingValueType
from app.models.support import Ticket, TicketStatus
from app.models.ticket_workflow import TicketAssignmentRule, TicketAssignmentStrategy
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services import service_team_lifecycle
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)

STATUS_OPTIONS_KEY = "support_ticket_status_options"
PRIORITY_OPTIONS_KEY = "support_ticket_priority_options"
TYPE_OPTIONS_KEY = "support_ticket_type_options"
REGION_OPTIONS_KEY = "support_ticket_region_options"
AUTO_ASSIGN_ENABLED_KEY = "support_ticket_auto_assign_enabled"
AUTO_ASSIGN_MAX_OPEN_TICKETS_KEY = "support_ticket_auto_assign_max_open_tickets"
REGION_ASSIGNMENT_RULES_KEY = "support_region_assignment_rules"
SLA_POLICY_KEY = "support_ticket_sla_policy"
TYPE_SLA_POLICY_KEY = "support_ticket_type_sla_policy"
SETTINGS_DOMAIN = SettingDomain.workflow

DEFAULT_STATUS_OPTIONS = [status.value for status in TicketStatus]
VALID_STATUS_OPTIONS = frozenset(DEFAULT_STATUS_OPTIONS)
DEFAULT_PRIORITY_OPTIONS = [
    "lower",
    "low",
    "medium",
    "normal",
    "high",
    "urgent",
]
DEFAULT_TYPE_OPTIONS = [
    "incident",
    "request",
    "change",
    "maintenance",
    "outage",
]
DEFAULT_REGION_OPTIONS = ["north", "south", "east", "west", "central"]
DEFAULT_SLA_POLICY = {
    "urgent": {"response_hours": 1, "resolution_hours": 8, "aging_hours": 4},
    "high": {"response_hours": 4, "resolution_hours": 24, "aging_hours": 12},
    "normal": {"response_hours": 8, "resolution_hours": 72, "aging_hours": 24},
    "medium": {"response_hours": 8, "resolution_hours": 72, "aging_hours": 24},
    "low": {"response_hours": 24, "resolution_hours": 120, "aging_hours": 48},
    "lower": {"response_hours": 24, "resolution_hours": 168, "aging_hours": 72},
}
TERMINAL_STATUSES = {"resolved", "closed", "canceled", "merged"}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_CONFIGURATION_OWNER = "support.ticket_configuration"
_CONFIGURATION_CONCERN = "ticket configuration mutations"
CUSTOMER_EXPERIENCE_TEAM_NAME = "Customer Experience"
SYSTEM_ADMIN_TEAM_NAME = "System Admin"


class PortalTicketTeamRoutingSource(str, Enum):
    """Configured fallback that supplied a portal ticket's requested team."""

    customer_experience = "customer_experience"
    system_admin = "system_admin"
    unassigned = "unassigned"


@dataclass(frozen=True)
class SupportTeamRoutingResolution:
    """Typed, current resolution of the customer-portal team fallback policy."""

    service_team_id: UUID | None
    service_team_name: str | None
    source: PortalTicketTeamRoutingSource


class SupportTicketConfigurationError(DomainError):
    """Transport-neutral ticket configuration error."""


def _configuration_command(name: str):
    definition = OwnerCommandDefinition(
        owner=_CONFIGURATION_OWNER,
        concern=_CONFIGURATION_CONCERN,
        name=name,
    )

    def decorate(operation):
        @wraps(operation)
        def wrapped(db: Session, *args, **kwargs):
            if owner_command_active(db, owner=_CONFIGURATION_OWNER):
                return operation(db, *args, **kwargs)
            if not owner_command_active(db):
                from app.services.db_session_adapter import db_session_adapter

                db_session_adapter.release_read_transaction(db)
            context = CommandContext.system(
                actor="support-settings-admin",
                scope=f"support.ticket_configuration:{name}",
                reason=f"change Ticket configuration via {name}",
            )

            def apply():
                try:
                    result = operation(db, *args, **kwargs)
                except ValueError as exc:
                    raise SupportTicketConfigurationError(
                        code="ticket_configuration_invalid",
                        message=str(exc),
                    ) from exc
                stage_audit_event(
                    db,
                    action="ticket.configuration_changed",
                    entity_type="support_ticket_configuration",
                    actor_type=AuditActorType.system,
                    metadata={"owner": _CONFIGURATION_OWNER, "operation": name},
                )
                return result

            return execute_owner_command(
                db, definition=definition, context=context, operation=apply
            )

        return wrapped

    return decorate


def _settings_service():
    service = getattr(domain_settings_service, "workflow_settings", None)
    if service is not None:
        return service
    return domain_settings_service.settings


def display_label(value: str) -> str:
    text = str(value or "").strip().replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in text.split()) or "-"


def normalize_system_value(value: str) -> str:
    text = str(value or "").strip().lower()
    text = _NON_ALNUM_RE.sub("_", text)
    return text.strip("_")


def normalize_ticket_status(value: str) -> str:
    """Keep configured choices inside the lifecycle owner's vocabulary."""
    normalized = normalize_system_value(value)
    return normalized if normalized in VALID_STATUS_OPTIONS else ""


def _normalize_list(
    raw: Any,
    *,
    defaults: list[str],
    normalizer=None,
) -> list[str]:
    values = raw if isinstance(raw, list) else []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if normalizer is not None:
            text = normalizer(text)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized or list(defaults)


def _read_list(
    db: Session,
    *,
    key: str,
    defaults: list[str],
    normalizer=None,
) -> list[str]:
    service = _settings_service()
    try:
        setting = (
            service.get_by_key(db, key)
            if getattr(service, "domain", None) is not None
            else domain_settings_service.settings.get_by_key(db, key)
        )
    except Exception:
        setting = None
    raw = getattr(setting, "value_json", None)
    return _normalize_list(raw, defaults=defaults, normalizer=normalizer)


def _write_list(
    db: Session,
    *,
    key: str,
    values: list[str],
) -> None:
    payload = DomainSettingUpdate(
        domain=SETTINGS_DOMAIN,
        value_type=SettingValueType.json,
        value_text=None,
        value_json=list(values),
        is_secret=False,
        is_active=True,
    )
    service = _settings_service()
    if getattr(service, "domain", None) is not None:
        service.stage_upsert_by_key(db, key, payload)
        return
    domain_settings_service.settings.upsert_by_key(db, key, payload)


def _read_raw_setting(db: Session, key: str) -> Any:
    service = _settings_service()
    try:
        setting = (
            service.get_by_key(db, key)
            if getattr(service, "domain", None) is not None
            else domain_settings_service.settings.get_by_key(db, key)
        )
    except Exception:
        return None
    if setting is None:
        return None
    return setting.value_json if setting.value_json is not None else setting.value_text


def _write_json(db: Session, *, key: str, value: Any) -> None:
    payload = DomainSettingUpdate(
        domain=SETTINGS_DOMAIN,
        value_type=SettingValueType.json,
        value_text=None,
        value_json=value,
        is_secret=False,
        is_active=True,
    )
    service = _settings_service()
    if getattr(service, "domain", None) is not None:
        service.stage_upsert_by_key(db, key, payload)
        return
    domain_settings_service.settings.upsert_by_key(db, key, payload)


def _write_bool(db: Session, *, key: str, value: bool) -> None:
    payload = DomainSettingUpdate(
        domain=SETTINGS_DOMAIN,
        value_type=SettingValueType.boolean,
        value_text="true" if value else "false",
        value_json=value,
        is_secret=False,
        is_active=True,
    )
    service = _settings_service()
    if getattr(service, "domain", None) is not None:
        service.stage_upsert_by_key(db, key, payload)
        return
    domain_settings_service.settings.upsert_by_key(db, key, payload)


def _write_optional_int(db: Session, *, key: str, value: int | None) -> None:
    payload = DomainSettingUpdate(
        domain=SETTINGS_DOMAIN,
        value_type=SettingValueType.integer
        if value is not None
        else SettingValueType.string,
        value_text=str(value) if value is not None else "",
        value_json=value,
        is_secret=False,
        is_active=True,
    )
    service = _settings_service()
    if getattr(service, "domain", None) is not None:
        service.stage_upsert_by_key(db, key, payload)
        return
    domain_settings_service.settings.upsert_by_key(db, key, payload)


def _normalize_uuid(
    value: object | None, *, allow_generate: bool = False
) -> str | None:
    text = str(value or "").strip()
    if not text and allow_generate:
        return str(uuid4())
    if not text:
        return None
    try:
        return str(UUID(text))
    except (TypeError, ValueError):
        raise ValueError(f"{text!r} is not a valid UUID")


def _normalize_non_negative_int(value: object | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{text!r} must be a whole number") from exc
    return max(parsed, 0)


def _normalize_optional_non_negative_int(value: object | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _normalize_non_negative_int(value)


def list_assignment_rules(db: Session) -> list[dict[str, Any]]:
    team_lookup = {str(team.id): team.name for team in db.query(ServiceTeam).all()}
    rows = (
        db.query(TicketAssignmentRule)
        .order_by(
            TicketAssignmentRule.priority.desc(), TicketAssignmentRule.created_at.asc()
        )
        .all()
    )
    rules: list[dict[str, Any]] = []
    for rule in rows:
        config = rule.match_config if isinstance(rule.match_config, dict) else {}
        team_id = str(rule.team_id) if rule.team_id else ""
        rules.append(
            {
                "id": str(rule.id),
                "name": rule.name,
                "priority": int(rule.priority or 0),
                "is_active": bool(rule.is_active),
                "strategy": str(
                    rule.strategy or TicketAssignmentStrategy.round_robin.value
                ),
                "team_id": team_id,
                "team_label": team_lookup.get(team_id, team_id),
                "assignment_target": str(
                    config.get("assignment_target") or "technician"
                ),
                "assignee_person_id": str(config.get("assignee_person_id") or ""),
                "ticket_types": config.get("ticket_types")
                if isinstance(config.get("ticket_types"), list)
                else [],
                "regions": config.get("regions")
                if isinstance(config.get("regions"), list)
                else [],
            }
        )
    return rules


def create_assignment_rule(
    db: Session,
    *,
    name: str,
    priority: object,
    strategy: str,
    team_id: str | None,
    ticket_types: list[str],
    regions: list[str],
    assignee_person_id: str | None,
    assignment_target: str,
    is_active: bool,
) -> TicketAssignmentRule:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Assignment rule name is required.")
    clean_strategy = str(strategy or TicketAssignmentStrategy.round_robin.value).strip()
    if clean_strategy not in {item.value for item in TicketAssignmentStrategy}:
        raise ValueError("Assignment strategy is invalid.")
    clean_team_id = _normalize_uuid(team_id)
    clean_assignee_id = _normalize_uuid(assignee_person_id)
    from app.services.ticket_assignment import admin as assignment_admin

    normalized_ticket_types = [
        str(item).strip() for item in ticket_types if str(item).strip()
    ]
    normalized_regions = [
        normalize_system_value(str(item)) for item in regions if str(item).strip()
    ]
    target = str(assignment_target or "technician").strip() or "technician"
    return assignment_admin.create_rule(
        db,
        name=clean_name,
        priority=_normalize_non_negative_int(priority),
        strategy=clean_strategy,
        team_id=UUID(clean_team_id) if clean_team_id else None,
        is_active=is_active,
        match_config=assignment_admin.TicketAssignmentRuleMatch(
            entity_types=("ticket",),
            ticket_types=tuple(normalized_ticket_types),
            regions=tuple(normalized_regions),
            assignee_person_id=UUID(clean_assignee_id) if clean_assignee_id else None,
            assignment_target=(
                assignment_admin.TicketAssignmentTarget(target)
                if clean_assignee_id
                else None
            ),
        ),
    )


def delete_assignment_rule(db: Session, rule_id: str) -> None:
    clean_id = _normalize_uuid(rule_id)
    if not clean_id:
        raise ValueError("Assignment rule ID is invalid.")
    from app.services.ticket_assignment import admin as assignment_admin

    assignment_admin.delete_rule(db, UUID(clean_id))


def list_status_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=STATUS_OPTIONS_KEY,
        defaults=DEFAULT_STATUS_OPTIONS,
        normalizer=normalize_ticket_status,
    )


def list_priority_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=PRIORITY_OPTIONS_KEY,
        defaults=DEFAULT_PRIORITY_OPTIONS,
        normalizer=normalize_system_value,
    )


def list_ticket_type_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=TYPE_OPTIONS_KEY,
        defaults=DEFAULT_TYPE_OPTIONS,
    )


def list_region_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=REGION_OPTIONS_KEY,
        defaults=DEFAULT_REGION_OPTIONS,
        normalizer=normalize_system_value,
    )


def list_canonical_region_options(db: Session) -> list[str]:
    """Return the canonical region projection shared by ticket forms."""

    rows = (
        db.query(Ticket.region)
        .filter(
            Ticket.is_active.is_(True),
            Ticket.region.isnot(None),
            Ticket.region != "",
        )
        .distinct()
        .order_by(Ticket.region.asc())
        .limit(200)
        .all()
    )
    discovered = [str(item[0]) for item in rows if item and item[0]]
    return sorted(set(discovered + list_region_options(db)))


def canonical_region_option(db: Session, submitted: str | None) -> str | None:
    """Resolve a submitted region only when it is a current canonical option."""

    candidate = str(submitted or "").strip()
    if not candidate:
        return None
    return next(
        (option for option in list_canonical_region_options(db) if option == candidate),
        None,
    )


def resolve_portal_ticket_team_routing(
    db: Session,
) -> SupportTeamRoutingResolution:
    """Resolve the first active exact-name team in the portal fallback order."""

    candidates = (
        (
            CUSTOMER_EXPERIENCE_TEAM_NAME,
            PortalTicketTeamRoutingSource.customer_experience,
        ),
        (SYSTEM_ADMIN_TEAM_NAME, PortalTicketTeamRoutingSource.system_admin),
    )
    for team_name, source in candidates:
        team = (
            db.query(ServiceTeam)
            .filter(
                ServiceTeam.is_active.is_(True),
                func.lower(ServiceTeam.name) == team_name.lower(),
            )
            .one_or_none()
        )
        if team is not None:
            return SupportTeamRoutingResolution(
                service_team_id=team.id,
                service_team_name=team.name,
                source=source,
            )
    return SupportTeamRoutingResolution(
        service_team_id=None,
        service_team_name=None,
        source=PortalTicketTeamRoutingSource.unassigned,
    )


def list_service_teams(db: Session) -> list[dict[str, str]]:
    """Project active shared teams; ticket settings does not own team identity."""

    return [
        {"id": str(team_id), "label": label}
        for team_id, label in service_team_lifecycle.list_active_team_options(db)
    ]


def auto_assign_enabled(db: Session) -> bool:
    raw = _read_raw_setting(db, AUTO_ASSIGN_ENABLED_KEY)
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return True


def region_assignment_rules(db: Session) -> dict[str, dict[str, Any]]:
    raw = _read_raw_setting(db, REGION_ASSIGNMENT_RULES_KEY)
    rules = raw if isinstance(raw, dict) else {}
    normalized: dict[str, dict[str, Any]] = {}
    for region, rule in rules.items():
        key = normalize_system_value(str(region))
        if not key or not isinstance(rule, dict):
            continue
        raw_assignees = rule.get("assignee_person_ids")
        assignee_values = raw_assignees if isinstance(raw_assignees, list) else []
        normalized[key] = {
            "ticket_manager_person_id": _normalize_uuid(
                rule.get("ticket_manager_person_id")
            ),
            "site_coordinator_person_id": _normalize_uuid(
                rule.get("site_coordinator_person_id")
            ),
            "technician_person_id": _normalize_uuid(rule.get("technician_person_id")),
            "service_team_id": _normalize_uuid(rule.get("service_team_id")),
            "assignee_person_ids": [
                uid
                for uid in (_normalize_uuid(item) for item in assignee_values)
                if uid
            ],
        }
    return normalized


def auto_assign_max_open_tickets(db: Session) -> int | None:
    raw = _read_raw_setting(db, AUTO_ASSIGN_MAX_OPEN_TICKETS_KEY)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def sla_policy(db: Session) -> dict[str, dict[str, int]]:
    raw = _read_raw_setting(db, SLA_POLICY_KEY)
    configured = raw if isinstance(raw, dict) else {}
    priorities = list_priority_options(db)
    policy: dict[str, dict[str, int]] = {}
    for priority in priorities:
        source = configured.get(priority)
        if not isinstance(source, dict):
            source = DEFAULT_SLA_POLICY.get(priority, {})
        policy[priority] = {
            "response_hours": _normalize_non_negative_int(source.get("response_hours")),
            "resolution_hours": _normalize_non_negative_int(
                source.get("resolution_hours")
            ),
            "aging_hours": _normalize_non_negative_int(source.get("aging_hours")),
        }
    return policy


def ticket_type_sla_policy(db: Session) -> dict[str, int]:
    """Return UI-owned resolution hours keyed by normalized ticket type."""
    raw = _read_raw_setting(db, TYPE_SLA_POLICY_KEY)
    configured = raw if isinstance(raw, dict) else {}
    policy: dict[str, int] = {
        " ".join(str(ticket_type).strip().lower().split()): (
            _normalize_non_negative_int(hours)
        )
        for ticket_type, hours in configured.items()
        if str(ticket_type).strip()
    }
    for ticket_type in list_ticket_type_options(db):
        key = " ".join(str(ticket_type).strip().lower().split())
        policy.setdefault(key, 0)
    return policy


@_configuration_command("update_ticket_configuration")
def update_options(
    db: Session,
    *,
    statuses: list[str],
    priorities: list[str],
    ticket_types: list[str],
    regions: list[str] | None = None,
    auto_assign: bool | None = None,
    auto_assign_max_open_tickets: str | int | None = None,
    routing_regions: list[str] | None = None,
    routing_ticket_manager_person_ids: list[str] | None = None,
    routing_site_coordinator_person_ids: list[str] | None = None,
    routing_technician_person_ids: list[str] | None = None,
    routing_service_team_ids: list[str] | None = None,
    routing_assignee_person_ids: list[str] | None = None,
    sla_priorities: list[str] | None = None,
    sla_response_hours: list[str] | None = None,
    sla_resolution_hours: list[str] | None = None,
    sla_aging_hours: list[str] | None = None,
    sla_ticket_types: list[str] | None = None,
    sla_ticket_type_resolution_hours: list[str] | None = None,
) -> None:
    requested_statuses = _normalize_list(
        statuses,
        defaults=DEFAULT_STATUS_OPTIONS,
        normalizer=normalize_system_value,
    )
    invalid_statuses = [
        status for status in requested_statuses if status not in VALID_STATUS_OPTIONS
    ]
    if invalid_statuses:
        unsupported = ", ".join(invalid_statuses)
        raise ValueError(f"Unsupported ticket status: {unsupported}")
    normalized_statuses = requested_statuses
    normalized_priorities = _normalize_list(
        priorities,
        defaults=DEFAULT_PRIORITY_OPTIONS,
        normalizer=normalize_system_value,
    )
    normalized_types = _normalize_list(
        ticket_types,
        defaults=DEFAULT_TYPE_OPTIONS,
    )
    _write_list(db, key=STATUS_OPTIONS_KEY, values=normalized_statuses)
    _write_list(db, key=PRIORITY_OPTIONS_KEY, values=normalized_priorities)
    _write_list(db, key=TYPE_OPTIONS_KEY, values=normalized_types)
    if regions is not None:
        _write_list(
            db,
            key=REGION_OPTIONS_KEY,
            values=_normalize_list(
                regions,
                defaults=DEFAULT_REGION_OPTIONS,
                normalizer=normalize_system_value,
            ),
        )
    if auto_assign is not None:
        _write_bool(db, key=AUTO_ASSIGN_ENABLED_KEY, value=auto_assign)
    if auto_assign_max_open_tickets is not None:
        value = _normalize_optional_non_negative_int(auto_assign_max_open_tickets)
        _write_optional_int(db, key=AUTO_ASSIGN_MAX_OPEN_TICKETS_KEY, value=value)
    if routing_regions is not None:

        def indexed(values: list[str] | None, index: int) -> str | None:
            return values[index] if values and index < len(values) else None

        rules: dict[str, dict[str, Any]] = {}
        active_team_ids = {item["id"] for item in list_service_teams(db)}
        for index, region_raw in enumerate(routing_regions):
            region = normalize_system_value(region_raw)
            if not region:
                continue
            assignee_raw = indexed(routing_assignee_person_ids, index) or ""
            assignees = [
                uid
                for uid in (
                    _normalize_uuid(item.strip())
                    for item in str(assignee_raw or "").split(",")
                    if item.strip()
                )
                if uid
            ]
            service_team_id = _normalize_uuid(indexed(routing_service_team_ids, index))
            if service_team_id and service_team_id not in active_team_ids:
                raise ValueError(
                    "Routing service team must reference an active native team."
                )
            rules[region] = {
                "ticket_manager_person_id": _normalize_uuid(
                    indexed(routing_ticket_manager_person_ids, index)
                ),
                "site_coordinator_person_id": _normalize_uuid(
                    indexed(routing_site_coordinator_person_ids, index)
                ),
                "technician_person_id": _normalize_uuid(
                    indexed(routing_technician_person_ids, index)
                ),
                "service_team_id": service_team_id,
                "assignee_person_ids": assignees,
            }
        _write_json(db, key=REGION_ASSIGNMENT_RULES_KEY, value=rules)
    if sla_priorities is not None:
        policy: dict[str, dict[str, int]] = {}
        for index, priority_raw in enumerate(sla_priorities):
            priority = normalize_system_value(priority_raw)
            if not priority:
                continue
            policy[priority] = {
                "response_hours": _normalize_non_negative_int(
                    sla_response_hours[index]
                    if sla_response_hours and index < len(sla_response_hours)
                    else None
                ),
                "resolution_hours": _normalize_non_negative_int(
                    sla_resolution_hours[index]
                    if sla_resolution_hours and index < len(sla_resolution_hours)
                    else None
                ),
                "aging_hours": _normalize_non_negative_int(
                    sla_aging_hours[index]
                    if sla_aging_hours and index < len(sla_aging_hours)
                    else None
                ),
            }
        _write_json(db, key=SLA_POLICY_KEY, value=policy)
    if sla_ticket_types is not None:
        type_policy: dict[str, int] = {}
        for index, ticket_type_raw in enumerate(sla_ticket_types):
            ticket_type = " ".join(str(ticket_type_raw).strip().lower().split())
            if not ticket_type:
                continue
            resolution_hours = _normalize_non_negative_int(
                sla_ticket_type_resolution_hours[index]
                if sla_ticket_type_resolution_hours
                and index < len(sla_ticket_type_resolution_hours)
                else None
            )
            if resolution_hours > 0:
                type_policy[ticket_type] = resolution_hours
        _write_json(db, key=TYPE_SLA_POLICY_KEY, value=type_policy)


def default_status(db: Session) -> str:
    options = list_status_options(db)
    return "open" if "open" in options else options[0]


def default_priority(db: Session) -> str:
    options = list_priority_options(db)
    return "normal" if "normal" in options else options[0]


def status_is_terminal(value: str | None) -> bool:
    return str(value or "").strip() in TERMINAL_STATUSES


def status_is_merged(value: str | None) -> bool:
    return str(value or "").strip() == "merged"
