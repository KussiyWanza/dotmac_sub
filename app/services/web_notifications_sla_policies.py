"""Admin form helpers for operational SLA escalation policies."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.operational_escalation import (
    OperationalEscalationPolicy,
    OperationalNotificationChannel,
)
from app.services import operational_escalation
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

CHANNELS = (
    OperationalNotificationChannel.email,
    OperationalNotificationChannel.whatsapp,
    OperationalNotificationChannel.sms,
    OperationalNotificationChannel.push,
    OperationalNotificationChannel.web,
    OperationalNotificationChannel.nextcloud_talk,
    OperationalNotificationChannel.webhook,
)
SEVERITIES = ("info", "low", "warning", "high", "critical")

_POLICY_COMMAND_CONCERN = "operational SLA policy command confirmation"
_CREATE_COMMAND = OwnerCommandDefinition(
    owner="operations.sla_escalation_commands",
    concern=_POLICY_COMMAND_CONCERN,
    name="create_operational_sla_policy",
)
_UPDATE_COMMAND = OwnerCommandDefinition(
    owner="operations.sla_escalation_commands",
    concern=_POLICY_COMMAND_CONCERN,
    name="update_operational_sla_policy",
)
_DEACTIVATE_COMMAND = OwnerCommandDefinition(
    owner="operations.sla_escalation_commands",
    concern=_POLICY_COMMAND_CONCERN,
    name="deactivate_operational_sla_policy",
)


class SlaPolicyCommandError(DomainError, ValueError):
    """Stable rejection from the operational SLA policy coordinator."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> SlaPolicyCommandError:
    return SlaPolicyCommandError(
        code=f"operations.sla_escalation_commands.{suffix}",
        message=message,
        details=details,
    )


def list_data(
    db: Session,
    *,
    trigger: str | None,
    active: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    query = db.query(OperationalEscalationPolicy)
    if trigger:
        query = query.filter(OperationalEscalationPolicy.trigger == trigger)
    active_filter = str(active or "").strip().lower()
    if active_filter == "active":
        query = query.filter(OperationalEscalationPolicy.is_active.is_(True))
    elif active_filter == "inactive":
        query = query.filter(OperationalEscalationPolicy.is_active.is_(False))
    total = query.count()
    policies = (
        query.order_by(
            OperationalEscalationPolicy.trigger.asc(),
            OperationalEscalationPolicy.level.asc(),
            OperationalEscalationPolicy.created_at.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "policies": policies,
        "event_definitions": operational_escalation.KNOWN_SLA_EVENT_DEFINITIONS,
        "event_labels": {
            item.trigger: item.label
            for item in operational_escalation.KNOWN_SLA_EVENT_DEFINITIONS
        },
        "entity_types": operational_escalation.OPERATIONAL_ENTITY_TYPES,
        "trigger": trigger or "",
        "active": active_filter,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


def form_data(
    db: Session,
    *,
    policy_id: UUID | None = None,
) -> dict[str, object] | None:
    policy = db.get(OperationalEscalationPolicy, policy_id) if policy_id else None
    if policy_id and policy is None:
        return None
    metadata = policy.metadata_ if policy and isinstance(policy.metadata_, dict) else {}
    near_breach_seconds = metadata.get("near_breach_seconds")
    near_breach_minutes = ""
    if near_breach_seconds is not None:
        try:
            near_breach_minutes = str(max(1, int(near_breach_seconds) // 60))
        except (TypeError, ValueError):
            near_breach_minutes = ""
    return {
        "policy": policy,
        "event_definitions": operational_escalation.KNOWN_SLA_EVENT_DEFINITIONS,
        "entity_types": operational_escalation.OPERATIONAL_ENTITY_TYPES,
        "channels": CHANNELS,
        "severities": SEVERITIES,
        "selected_channels": set(policy.channels or []) if policy else set(),
        "delay_minutes": (
            int((policy.unresolved_after_seconds or 0) / 60) if policy else 0
        ),
        "notes": str(metadata.get("notes") or "") if policy else "",
        "near_breach_minutes": near_breach_minutes,
    }


def create_policy(
    db: Session,
    *,
    context: CommandContext,
    name: str,
    entity_type: str,
    trigger: str,
    level: int,
    delay_minutes: int,
    channels: list[str],
    min_severity: str | None,
    min_affected_customers: int | None,
    notes: str | None,
    near_breach_minutes: int | None = None,
    is_active: bool,
) -> OperationalEscalationPolicy:
    def operation() -> OperationalEscalationPolicy:
        values = _validated_values(
            db,
            name=name,
            entity_type=entity_type,
            trigger=trigger,
            level=level,
            delay_minutes=delay_minutes,
            channels=channels,
            min_severity=min_severity,
            min_affected_customers=min_affected_customers,
            exclude_policy_id=None,
            is_active=is_active,
        )
        policy = operational_escalation.create_policy(
            db,
            **values,
            cooldown_seconds=0,
            metadata=_metadata(
                notes=notes,
                near_breach_minutes=near_breach_minutes,
            ),
        )
        policy.is_active = is_active
        return policy

    return execute_owner_command(
        db,
        definition=_CREATE_COMMAND,
        context=context,
        operation=operation,
    )


def update_policy(
    db: Session,
    *,
    context: CommandContext,
    policy_id: UUID,
    name: str,
    entity_type: str,
    trigger: str,
    level: int,
    delay_minutes: int,
    channels: list[str],
    min_severity: str | None,
    min_affected_customers: int | None,
    notes: str | None,
    near_breach_minutes: int | None = None,
    is_active: bool,
) -> OperationalEscalationPolicy:
    def operation() -> OperationalEscalationPolicy:
        policy = db.get(OperationalEscalationPolicy, policy_id)
        if policy is None:
            raise _error("not_found", "SLA policy not found", policy_id=str(policy_id))
        values = _validated_values(
            db,
            name=name,
            entity_type=entity_type,
            trigger=trigger,
            level=level,
            delay_minutes=delay_minutes,
            channels=channels,
            min_severity=min_severity,
            min_affected_customers=min_affected_customers,
            exclude_policy_id=policy_id,
            is_active=is_active,
        )
        return operational_escalation.update_policy(
            db,
            policy,
            **values,
            metadata=_metadata(
                notes=notes,
                near_breach_minutes=near_breach_minutes,
            ),
            is_active=is_active,
        )

    return execute_owner_command(
        db,
        definition=_UPDATE_COMMAND,
        context=context,
        operation=operation,
    )


def deactivate_policy(
    db: Session,
    *,
    context: CommandContext,
    policy_id: UUID,
) -> None:
    def operation() -> None:
        policy = db.get(OperationalEscalationPolicy, policy_id)
        if policy is None:
            raise _error("not_found", "SLA policy not found", policy_id=str(policy_id))
        operational_escalation.deactivate_policy(db, policy)

    execute_owner_command(
        db,
        definition=_DEACTIVATE_COMMAND,
        context=context,
        operation=operation,
    )


def _validated_values(
    db: Session,
    *,
    name: str,
    entity_type: str,
    trigger: str,
    level: int,
    delay_minutes: int,
    channels: list[str],
    min_severity: str | None,
    min_affected_customers: int | None,
    exclude_policy_id: UUID | None,
    is_active: bool,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise _error("invalid_policy", "Policy name is required")
    try:
        normalized_entity_type, normalized_trigger = (
            operational_escalation.validate_sla_event(
                entity_type=entity_type,
                trigger=trigger,
            )
        )
    except operational_escalation.OperationalEscalationError as exc:
        raise _error(
            "invalid_policy",
            exc.message,
            entity_type=entity_type,
            trigger=trigger,
        ) from exc
    if level < 1:
        raise _error("invalid_policy", "Escalation level must be at least 1")
    if delay_minutes < 0 or delay_minutes > 525_600:
        raise _error(
            "invalid_policy",
            "Escalation delay must be between 0 and 525600 minutes",
        )
    selected_channels = list(dict.fromkeys(channels))
    invalid_channels = set(selected_channels) - set(CHANNELS)
    if invalid_channels:
        raise _error(
            "invalid_policy",
            f"Unsupported notification channels: {sorted(invalid_channels)}",
        )
    if not selected_channels:
        raise _error("invalid_policy", "Select at least one notification channel")
    if min_affected_customers is not None and min_affected_customers < 0:
        raise _error("invalid_policy", "Minimum affected customers cannot be negative")
    normalized_min_severity = min_severity.strip().lower() if min_severity else None
    if normalized_min_severity and normalized_min_severity not in SEVERITIES:
        raise _error("invalid_policy", "Select a supported minimum severity")
    if is_active:
        duplicate = (
            db.query(OperationalEscalationPolicy)
            .filter(OperationalEscalationPolicy.entity_type == normalized_entity_type)
            .filter(OperationalEscalationPolicy.trigger == normalized_trigger)
            .filter(OperationalEscalationPolicy.level == level)
            .filter(OperationalEscalationPolicy.is_active.is_(True))
        )
        if exclude_policy_id is not None:
            duplicate = duplicate.filter(
                OperationalEscalationPolicy.id != exclude_policy_id
            )
        if duplicate.first() is not None:
            raise _error(
                "duplicate_active_policy",
                "An active policy already owns this event and escalation level",
                entity_type=normalized_entity_type,
                trigger=normalized_trigger,
                level=level,
            )
    return {
        "name": clean_name,
        "entity_type": normalized_entity_type,
        "trigger": normalized_trigger,
        "level": level,
        "channels": selected_channels,
        "min_severity": normalized_min_severity,
        "min_affected_customers": min_affected_customers,
        "unresolved_after_seconds": delay_minutes * 60,
    }


def _metadata(
    *,
    notes: str | None,
    near_breach_minutes: int | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if notes and notes.strip():
        metadata["notes"] = notes.strip()
    if near_breach_minutes is not None:
        if near_breach_minutes < 1 or near_breach_minutes > 525_600:
            raise _error(
                "invalid_policy",
                "Near-breach window must be between 1 and 525600 minutes",
            )
        metadata["near_breach_seconds"] = near_breach_minutes * 60
    return metadata
