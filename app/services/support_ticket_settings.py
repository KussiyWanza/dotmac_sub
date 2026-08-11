from __future__ import annotations

import re
from collections.abc import Sequence
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
from app.models.support import TicketStatus, parse_ticket_status
from app.models.ticket_workflow import TicketAssignmentRule, TicketAssignmentStrategy
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services import service_team_lifecycle, support_ticket_region_projection
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
TERMINAL_STATUSES = {"closed", "canceled", "merged"}

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


class RoutingOptionAvailability(str, Enum):
    active = "active"
    inactive = "inactive"
    unavailable = "unavailable"


@dataclass(frozen=True, slots=True)
class RegionAssignmentRule:
    """Typed regional routing input owned by ticket configuration."""

    region: str
    ticket_manager_person_id: UUID | None = None
    site_coordinator_person_id: UUID | None = None
    technician_person_id: UUID | None = None
    service_team_id: UUID | None = None
    assignee_person_ids: tuple[UUID, ...] = ()

    def as_storage_value(self) -> dict[str, str | list[str] | None]:
        return {
            "ticket_manager_person_id": _uuid_text(self.ticket_manager_person_id),
            "site_coordinator_person_id": _uuid_text(self.site_coordinator_person_id),
            "technician_person_id": _uuid_text(self.technician_person_id),
            "service_team_id": _uuid_text(self.service_team_id),
            "assignee_person_ids": [str(value) for value in self.assignee_person_ids],
        }


@dataclass(frozen=True, slots=True)
class RegionRoutingRuleUpdate:
    """One typed row submitted to the regional routing configuration owner."""

    region: str | None = None
    ticket_manager_person_id: UUID | None = None
    site_coordinator_person_id: UUID | None = None
    technician_person_id: UUID | None = None
    service_team_id: UUID | None = None
    assignee_person_ids: tuple[UUID, ...] = ()

    @property
    def has_assignment(self) -> bool:
        return any(
            (
                self.ticket_manager_person_id,
                self.site_coordinator_person_id,
                self.technician_person_id,
                self.service_team_id,
                self.assignee_person_ids,
            )
        )


@dataclass(frozen=True, slots=True)
class TicketSlaPolicyUpdate:
    priority: str
    response_hours: int
    resolution_hours: int
    aging_hours: int


@dataclass(frozen=True, slots=True)
class TicketTypeSlaPolicyUpdate:
    ticket_type: str
    resolution_hours: int


@dataclass(frozen=True, slots=True)
class TicketConfigurationUpdate:
    """Complete typed input for the ticket-configuration owner command."""

    statuses: tuple[str, ...]
    priorities: tuple[str, ...]
    ticket_types: tuple[str, ...]
    regions: tuple[str, ...] | None = None
    auto_assign: bool | None = None
    auto_assign_max_open_tickets: int | None = None
    replace_auto_assign_max_open_tickets: bool = False
    routing_rules: tuple[RegionRoutingRuleUpdate, ...] | None = None
    sla_policy: tuple[TicketSlaPolicyUpdate, ...] | None = None
    ticket_type_sla_policy: tuple[TicketTypeSlaPolicyUpdate, ...] | None = None


@dataclass(frozen=True, slots=True)
class TicketConfigurationUpdateOutcome:
    routing_rule_count: int


@dataclass(frozen=True, slots=True)
class RoutingSelectOption:
    id: UUID
    label: str
    availability: RoutingOptionAvailability

    def as_template_value(self) -> dict[str, str]:
        return {
            "id": str(self.id),
            "label": self.label,
            "availability": self.availability.value,
        }


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


def normalize_region_key(value: str | None) -> str:
    return support_ticket_region_projection.normalize_region_value(value)


def normalize_ticket_status(value: TicketStatus | str) -> str:
    """Keep configured choices inside the lifecycle owner's vocabulary."""
    normalized = (
        value.value
        if isinstance(value, TicketStatus)
        else normalize_system_value(value)
    )
    try:
        return parse_ticket_status(normalized).value
    except ValueError:
        return ""


def _normalize_list(
    raw: Sequence[object] | None,
    *,
    defaults: list[str],
    normalizer=None,
) -> list[str]:
    values = raw or ()
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


def _parse_uuid(value: object | None) -> UUID | None:
    normalized = _normalize_uuid(value)
    return UUID(normalized) if normalized else None


def _uuid_text(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _normalize_non_negative_int(value: object | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{text!r} must be a whole number") from exc
    return max(parsed, 0)


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
        normalizer=normalize_region_key,
    )


def list_canonical_region_options(db: Session) -> list[str]:
    """Return the canonical region projection shared by ticket forms."""

    return list(
        support_ticket_region_projection.list_canonical_region_options(
            db,
            configured_regions=tuple(list_region_options(db)),
        )
    )


def canonical_region_option(db: Session, submitted: str | None) -> str | None:
    """Resolve a submitted region only when it is a current canonical option."""

    return support_ticket_region_projection.canonical_region_option(
        db,
        submitted,
        configured_regions=tuple(list_region_options(db)),
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


def region_assignment_rules(db: Session) -> dict[str, RegionAssignmentRule]:
    raw = _read_raw_setting(db, REGION_ASSIGNMENT_RULES_KEY)
    rules = raw if isinstance(raw, dict) else {}
    normalized: dict[str, RegionAssignmentRule] = {}
    for region, rule in rules.items():
        key = normalize_region_key(str(region))
        if not key or not isinstance(rule, dict):
            continue
        raw_assignees = rule.get("assignee_person_ids")
        assignee_values = raw_assignees if isinstance(raw_assignees, list) else []
        normalized[key] = RegionAssignmentRule(
            region=key,
            ticket_manager_person_id=_parse_uuid(rule.get("ticket_manager_person_id")),
            site_coordinator_person_id=_parse_uuid(
                rule.get("site_coordinator_person_id")
            ),
            technician_person_id=_parse_uuid(rule.get("technician_person_id")),
            service_team_id=_parse_uuid(rule.get("service_team_id")),
            assignee_person_ids=tuple(
                uid for uid in (_parse_uuid(item) for item in assignee_values) if uid
            ),
        )
    return normalized


def active_routing_staff_ids(db: Session) -> frozenset[UUID]:
    """Return staff identities currently eligible for configured routing."""

    return frozenset(
        option.system_user_id
        for option in service_team_lifecycle.list_staff_options(db)
    )


def resolve_region_assignment_rule(
    db: Session, region: str | None
) -> RegionAssignmentRule | None:
    """Resolve a current rule and remove assignments that are no longer active."""

    region_key = normalize_region_key(region)
    if not region_key:
        return None
    configured = region_assignment_rules(db).get(region_key)
    if configured is None:
        return None

    active_staff_ids = active_routing_staff_ids(db)
    active_team_ids = {
        team_id
        for team_id, _label in service_team_lifecycle.list_active_team_options(db)
    }

    def active_staff(value: UUID | None) -> UUID | None:
        return value if value in active_staff_ids else None

    return RegionAssignmentRule(
        region=configured.region,
        ticket_manager_person_id=active_staff(configured.ticket_manager_person_id),
        site_coordinator_person_id=active_staff(configured.site_coordinator_person_id),
        technician_person_id=active_staff(configured.technician_person_id),
        service_team_id=(
            configured.service_team_id
            if configured.service_team_id in active_team_ids
            else None
        ),
        assignee_person_ids=tuple(
            person_id
            for person_id in configured.assignee_person_ids
            if person_id in active_staff_ids
        ),
    )


def region_manager_routing_preview(db: Session) -> tuple[RegionAssignmentRule, ...]:
    """Return current manager decisions for the new-ticket preview."""

    previews: list[RegionAssignmentRule] = []
    for region in region_assignment_rules(db):
        resolved = resolve_region_assignment_rule(db, region)
        if resolved is not None and resolved.ticket_manager_person_id is not None:
            previews.append(resolved)
    return tuple(previews)


def list_routing_service_team_options(
    db: Session, *, include_ids: tuple[UUID, ...] = ()
) -> tuple[RoutingSelectOption, ...]:
    """Return active teams plus stale saved teams for configuration repair."""

    active_rows = service_team_lifecycle.list_active_team_options(db)
    options = [
        RoutingSelectOption(
            id=team_id,
            label=label,
            availability=RoutingOptionAvailability.active,
        )
        for team_id, label in active_rows
    ]
    seen = {option.id for option in options}
    for team_id in include_ids:
        if team_id in seen:
            continue
        try:
            detail = service_team_lifecycle.get_team(db, team_id)
        except DomainError:
            options.append(
                RoutingSelectOption(
                    id=team_id,
                    label=f"Unavailable service team ({team_id})",
                    availability=RoutingOptionAvailability.unavailable,
                )
            )
        else:
            options.append(
                RoutingSelectOption(
                    id=team_id,
                    label=f"{detail.team.name} (Inactive)",
                    availability=RoutingOptionAvailability.inactive,
                )
            )
        seen.add(team_id)
    return tuple(options)


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
def update_ticket_configuration(
    db: Session, command: TicketConfigurationUpdate
) -> TicketConfigurationUpdateOutcome:
    requested_statuses = _normalize_list(
        command.statuses,
        defaults=DEFAULT_STATUS_OPTIONS,
        normalizer=normalize_system_value,
    )
    invalid_statuses = [
        status for status in requested_statuses if not normalize_ticket_status(status)
    ]
    if invalid_statuses:
        unsupported = ", ".join(invalid_statuses)
        raise ValueError(f"Unsupported ticket status: {unsupported}")
    normalized_statuses = list(
        dict.fromkeys(normalize_ticket_status(status) for status in requested_statuses)
    )
    normalized_priorities = _normalize_list(
        command.priorities,
        defaults=DEFAULT_PRIORITY_OPTIONS,
        normalizer=normalize_system_value,
    )
    normalized_types = _normalize_list(
        command.ticket_types,
        defaults=DEFAULT_TYPE_OPTIONS,
    )
    _write_list(db, key=STATUS_OPTIONS_KEY, values=normalized_statuses)
    _write_list(db, key=PRIORITY_OPTIONS_KEY, values=normalized_priorities)
    _write_list(db, key=TYPE_OPTIONS_KEY, values=normalized_types)
    if command.regions is not None:
        _write_list(
            db,
            key=REGION_OPTIONS_KEY,
            values=_normalize_list(
                command.regions,
                defaults=DEFAULT_REGION_OPTIONS,
                normalizer=normalize_region_key,
            ),
        )
    if command.auto_assign is not None:
        _write_bool(db, key=AUTO_ASSIGN_ENABLED_KEY, value=command.auto_assign)
    if command.replace_auto_assign_max_open_tickets:
        _write_optional_int(
            db,
            key=AUTO_ASSIGN_MAX_OPEN_TICKETS_KEY,
            value=command.auto_assign_max_open_tickets,
        )
    routing_rule_count = len(region_assignment_rules(db))
    if command.routing_rules is not None:
        rules: dict[str, dict[str, str | list[str] | None]] = {}
        active_team_ids = {
            team_id
            for team_id, _label in service_team_lifecycle.list_active_team_options(db)
        }
        active_staff_ids = active_routing_staff_ids(db)
        for submitted in command.routing_rules:
            region = normalize_region_key(submitted.region)
            if not region:
                if submitted.has_assignment:
                    raise ValueError(
                        "Routing assignments require a region. Remove the assignments "
                        "or select a region."
                    )
                continue
            if (
                submitted.service_team_id is not None
                and submitted.service_team_id not in active_team_ids
            ):
                raise ValueError(
                    "Routing service team must reference an active native team."
                )
            submitted_staff_ids = {
                value
                for value in (
                    submitted.ticket_manager_person_id,
                    submitted.site_coordinator_person_id,
                    submitted.technician_person_id,
                    *submitted.assignee_person_ids,
                )
                if value is not None
            }
            if not submitted_staff_ids.issubset(active_staff_ids):
                raise ValueError(
                    "Routing assignments must reference active staff. Replace any "
                    "inactive or unavailable assignment."
                )
            rule = RegionAssignmentRule(
                region=region,
                ticket_manager_person_id=submitted.ticket_manager_person_id,
                site_coordinator_person_id=submitted.site_coordinator_person_id,
                technician_person_id=submitted.technician_person_id,
                service_team_id=submitted.service_team_id,
                assignee_person_ids=submitted.assignee_person_ids,
            )
            rules[region] = rule.as_storage_value()
        _write_json(db, key=REGION_ASSIGNMENT_RULES_KEY, value=rules)
        routing_rule_count = len(rules)
    if command.sla_policy is not None:
        policy: dict[str, dict[str, int]] = {}
        for sla_update in command.sla_policy:
            priority = normalize_system_value(sla_update.priority)
            if not priority:
                continue
            policy[priority] = {
                "response_hours": max(sla_update.response_hours, 0),
                "resolution_hours": max(sla_update.resolution_hours, 0),
                "aging_hours": max(sla_update.aging_hours, 0),
            }
        _write_json(db, key=SLA_POLICY_KEY, value=policy)
    if command.ticket_type_sla_policy is not None:
        type_policy: dict[str, int] = {}
        for type_sla_update in command.ticket_type_sla_policy:
            ticket_type = " ".join(type_sla_update.ticket_type.strip().lower().split())
            if not ticket_type:
                continue
            resolution_hours = max(type_sla_update.resolution_hours, 0)
            if resolution_hours > 0:
                type_policy[ticket_type] = resolution_hours
        _write_json(db, key=TYPE_SLA_POLICY_KEY, value=type_policy)
    return TicketConfigurationUpdateOutcome(routing_rule_count=routing_rule_count)


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
