from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.operational_escalation import (
    OperationalDeliveryStatus,
    OperationalEntityType,
    OperationalEscalationDelivery,
    OperationalEscalationEvent,
    OperationalEscalationPolicy,
    OperationalEscalationStatus,
    OperationalOwner,
    OperationalOwnerRole,
    OperationalParticipantType,
    OperationalRoomLink,
    OperationalWatcher,
    OperationalWatcherRole,
)
from app.models.subscriber import Reseller, Subscriber
from app.services.domain_errors import DomainError

DIRECT_ADDRESS_CHANNELS = {"email", "sms", "whatsapp", "webhook"}
DEFAULT_ESCALATION_PARTICIPANTS = {
    OperationalParticipantType.person,
    OperationalParticipantType.team,
    OperationalParticipantType.duty_role,
}
SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "minor": 1,
    "warning": 2,
    "medium": 2,
    "moderate": 2,
    "high": 3,
    "major": 3,
    "critical": 4,
}


class OperationalEscalationError(DomainError, ValueError):
    """Stable rejection from the operational escalation participant."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> OperationalEscalationError:
    return OperationalEscalationError(
        code=f"operations.sla_escalation.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class SlaEventDefinition:
    """A business event that can emit a UI-configured SLA escalation."""

    trigger: str
    entity_type: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class SlaEmissionResult:
    """Events and deliveries materialized for one observed operational fact."""

    policy_count: int
    events: tuple[OperationalEscalationEvent, ...]
    deliveries: tuple[OperationalEscalationDelivery, ...]


OPERATIONAL_ENTITY_TYPES = (
    OperationalEntityType.outage,
    OperationalEntityType.ticket,
    OperationalEntityType.work_order,
    OperationalEntityType.project,
    OperationalEntityType.project_task,
    OperationalEntityType.inbox_conversation,
    OperationalEntityType.subscriber,
    OperationalEntityType.network_device,
    OperationalEntityType.site,
    OperationalEntityType.payment_incident,
    OperationalEntityType.payment_proof,
    OperationalEntityType.provisioning_failure,
)

KNOWN_SLA_EVENT_DEFINITIONS = (
    SlaEventDefinition(
        trigger="payment_proof.review_requested",
        entity_type=OperationalEntityType.payment_proof,
        label="Payment proof awaiting review",
        description=(
            "Escalates an unresolved bank-transfer receipt to staff who can verify "
            "payment proofs. The initial work item always appears in their inbox."
        ),
    ),
    SlaEventDefinition(
        trigger="ticket.created",
        entity_type=OperationalEntityType.ticket,
        label="Ticket created",
        description="Notifies the configured operational audience when a support ticket is created.",
    ),
    SlaEventDefinition(
        trigger="ticket.status_changed",
        entity_type=OperationalEntityType.ticket,
        label="Ticket status changed",
        description="Notifies the configured operational audience when a support ticket changes status.",
    ),
    SlaEventDefinition(
        trigger="ticket.sla_near_breach",
        entity_type=OperationalEntityType.ticket,
        label="Ticket SLA approaching breach",
        description="Notifies the configured operational audience before a ticket SLA deadline breaches.",
    ),
    SlaEventDefinition(
        trigger="ticket.sla_breached",
        entity_type=OperationalEntityType.ticket,
        label="Ticket SLA breached",
        description="Escalates a support ticket after its configured SLA clock breaches.",
    ),
    SlaEventDefinition(
        trigger="project.created",
        entity_type=OperationalEntityType.project,
        label="Project created",
        description="Notifies the configured operational audience when a project is created.",
    ),
    SlaEventDefinition(
        trigger="project.status_changed",
        entity_type=OperationalEntityType.project,
        label="Project status changed",
        description="Notifies the configured operational audience when a project changes status.",
    ),
    SlaEventDefinition(
        trigger="project.sla_near_breach",
        entity_type=OperationalEntityType.project,
        label="Project SLA approaching breach",
        description="Notifies the configured operational audience before a project SLA deadline breaches.",
    ),
    SlaEventDefinition(
        trigger="project.sla_breached",
        entity_type=OperationalEntityType.project,
        label="Project SLA breached",
        description="Escalates a project after its configured completion SLA breaches.",
    ),
    SlaEventDefinition(
        trigger="project_task.sla_near_breach",
        entity_type=OperationalEntityType.project_task,
        label="Project task SLA approaching breach",
        description="Notifies the configured operational audience before a project task SLA deadline breaches.",
    ),
    SlaEventDefinition(
        trigger="project_task.sla_breached",
        entity_type=OperationalEntityType.project_task,
        label="Project task SLA breached",
        description="Escalates a project task after its configured due clock breaches.",
    ),
    SlaEventDefinition(
        trigger="outage.created",
        entity_type=OperationalEntityType.outage,
        label="Outage created",
        description="Escalates a newly detected outage to its operational audience.",
    ),
    SlaEventDefinition(
        trigger="outage.confirmed",
        entity_type=OperationalEntityType.outage,
        label="Outage confirmed",
        description="Escalates an outage after an operator confirms it.",
    ),
)


def validate_sla_event(*, entity_type: str, trigger: str) -> tuple[str, str]:
    """Validate operator-owned policy keys without closing the event registry."""
    normalized_entity_type = entity_type.strip().lower()
    normalized_trigger = trigger.strip().lower()
    if normalized_entity_type not in OPERATIONAL_ENTITY_TYPES:
        raise _error(
            "invalid_event",
            f"Unsupported operational entity type: {entity_type}",
            entity_type=entity_type,
        )
    if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", normalized_trigger):
        raise _error(
            "invalid_event",
            "Event key must use dotted lower-case names, for example ticket.unowned",
            trigger=trigger,
        )
    if len(normalized_trigger) > 80:
        raise _error(
            "invalid_event",
            "Event key must be 80 characters or fewer",
            trigger=trigger,
        )
    if normalized_trigger.split(".", 1)[0] != normalized_entity_type:
        raise _error(
            "invalid_event",
            "Event key prefix must match the operational entity type",
            entity_type=entity_type,
            trigger=trigger,
        )
    known = next(
        (
            definition
            for definition in KNOWN_SLA_EVENT_DEFINITIONS
            if definition.trigger == normalized_trigger
        ),
        None,
    )
    if known is not None and known.entity_type != normalized_entity_type:
        raise _error(
            "invalid_event",
            f"{normalized_trigger} belongs to operational entity {known.entity_type}",
            entity_type=entity_type,
            trigger=trigger,
        )
    return normalized_entity_type, normalized_trigger


def matching_policies(
    db: Session,
    *,
    entity_type: str,
    trigger: str,
    severity: str | None = None,
    affected_customer_count: int | None = None,
) -> list[OperationalEscalationPolicy]:
    """Return active UI-owned policies for one emitted business event."""
    policies = list(
        db.query(OperationalEscalationPolicy)
        .filter(OperationalEscalationPolicy.is_active.is_(True))
        .filter(
            or_(
                OperationalEscalationPolicy.entity_type == entity_type,
                OperationalEscalationPolicy.entity_type.is_(None),
            )
        )
        .filter(
            or_(
                OperationalEscalationPolicy.trigger == trigger,
                OperationalEscalationPolicy.trigger.is_(None),
            )
        )
        .order_by(
            OperationalEscalationPolicy.level.asc(),
            OperationalEscalationPolicy.created_at.asc(),
        )
        .all()
    )
    return [
        policy
        for policy in policies
        if policy_matches_event(
            policy,
            severity=severity,
            affected_customer_count=affected_customer_count,
        )
    ]


def policy_matches_event(
    policy: OperationalEscalationPolicy,
    *,
    severity: str | None,
    affected_customer_count: int | None,
) -> bool:
    """Apply UI-configured observation thresholds to an emitted event."""
    if policy.min_severity:
        if severity is None:
            return False
        actual = severity.strip().lower()
        minimum = policy.min_severity.strip().lower()
        if SEVERITY_RANK.get(actual, -1) < SEVERITY_RANK.get(minimum, -1):
            return False
    if policy.min_affected_customers is not None:
        if affected_customer_count is None:
            return False
        if affected_customer_count < policy.min_affected_customers:
            return False
    return True


def _entity_id(value: str | UUID) -> str:
    return str(value)


def _participant_type(
    *,
    service_team_id: str | UUID | None = None,
    person_id: str | UUID | None = None,
    subscriber_id: str | UUID | None = None,
    duty_role: str | None = None,
    external: bool = False,
) -> str:
    selected = [
        service_team_id is not None,
        person_id is not None,
        subscriber_id is not None,
        bool(duty_role),
        external,
    ]
    if sum(1 for item in selected if item) != 1:
        raise _error(
            "invalid_participant",
            "exactly one participant target is required",
        )
    if service_team_id is not None:
        return OperationalParticipantType.team
    if person_id is not None:
        return OperationalParticipantType.person
    if subscriber_id is not None:
        return OperationalParticipantType.subscriber
    if duty_role:
        return OperationalParticipantType.duty_role
    return OperationalParticipantType.external


def _uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def set_owner(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    service_team_id: str | UUID | None = None,
    person_id: str | UUID | None = None,
    duty_role: str | None = None,
    role: str = OperationalOwnerRole.primary,
    source: str | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
    assigned_at: datetime | None = None,
) -> OperationalOwner:
    owner_type = _participant_type(
        service_team_id=service_team_id,
        person_id=person_id,
        duty_role=duty_role,
    )
    normalized_entity_id = _entity_id(entity_id)
    if role == OperationalOwnerRole.primary:
        (
            db.query(OperationalOwner)
            .filter(OperationalOwner.entity_type == entity_type)
            .filter(OperationalOwner.entity_id == normalized_entity_id)
            .filter(OperationalOwner.role == OperationalOwnerRole.primary)
            .filter(OperationalOwner.is_active.is_(True))
            .update({"is_active": False}, synchronize_session="fetch")
        )

    owner = OperationalOwner(
        entity_type=entity_type,
        entity_id=normalized_entity_id,
        owner_type=owner_type,
        role=role,
        service_team_id=_uuid(service_team_id),
        person_id=_uuid(person_id),
        duty_role=duty_role,
        source=source,
        reason=reason,
        metadata_=metadata,
        assigned_at=assigned_at or datetime.now(UTC),
    )
    db.add(owner)
    db.flush()
    return owner


def add_watcher(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    service_team_id: str | UUID | None = None,
    person_id: str | UUID | None = None,
    subscriber_id: str | UUID | None = None,
    duty_role: str | None = None,
    external: bool = False,
    role: str = OperationalWatcherRole.watcher,
    source: str | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
) -> OperationalWatcher:
    watcher_type = _participant_type(
        service_team_id=service_team_id,
        person_id=person_id,
        subscriber_id=subscriber_id,
        duty_role=duty_role,
        external=external,
    )
    normalized_entity_id = _entity_id(entity_id)
    existing = (
        db.query(OperationalWatcher)
        .filter(OperationalWatcher.entity_type == entity_type)
        .filter(OperationalWatcher.entity_id == normalized_entity_id)
        .filter(OperationalWatcher.watcher_type == watcher_type)
        .filter(OperationalWatcher.service_team_id == _uuid(service_team_id))
        .filter(OperationalWatcher.person_id == _uuid(person_id))
        .filter(OperationalWatcher.subscriber_id == _uuid(subscriber_id))
        .filter(OperationalWatcher.duty_role == duty_role)
        .one_or_none()
    )
    if existing is not None:
        existing.role = role
        existing.source = source or existing.source
        existing.reason = reason or existing.reason
        existing.metadata_ = metadata or existing.metadata_
        existing.is_active = True
        db.flush()
        return existing

    watcher = OperationalWatcher(
        entity_type=entity_type,
        entity_id=normalized_entity_id,
        watcher_type=watcher_type,
        role=role,
        service_team_id=_uuid(service_team_id),
        person_id=_uuid(person_id),
        subscriber_id=_uuid(subscriber_id),
        duty_role=duty_role,
        source=source,
        reason=reason,
        metadata_=metadata,
    )
    db.add(watcher)
    db.flush()
    return watcher


def list_watchers(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    active_only: bool = True,
) -> list[OperationalWatcher]:
    query = (
        db.query(OperationalWatcher)
        .filter(OperationalWatcher.entity_type == entity_type)
        .filter(OperationalWatcher.entity_id == _entity_id(entity_id))
        .order_by(OperationalWatcher.created_at.asc())
    )
    if active_only:
        query = query.filter(OperationalWatcher.is_active.is_(True))
    return list(query.all())


def link_room(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    provider: str,
    room_id: str,
    room_name: str | None = None,
    room_url: str | None = None,
    metadata: dict | None = None,
) -> OperationalRoomLink:
    normalized_entity_id = _entity_id(entity_id)
    existing = (
        db.query(OperationalRoomLink)
        .filter(OperationalRoomLink.entity_type == entity_type)
        .filter(OperationalRoomLink.entity_id == normalized_entity_id)
        .filter(OperationalRoomLink.provider == provider)
        .filter(OperationalRoomLink.room_id == room_id)
        .one_or_none()
    )
    if existing is not None:
        existing.room_name = room_name or existing.room_name
        existing.room_url = room_url or existing.room_url
        existing.metadata_ = metadata or existing.metadata_
        existing.is_active = True
        db.flush()
        return existing
    link = OperationalRoomLink(
        entity_type=entity_type,
        entity_id=normalized_entity_id,
        provider=provider,
        room_id=room_id,
        room_name=room_name,
        room_url=room_url,
        metadata_=metadata,
    )
    db.add(link)
    db.flush()
    return link


def create_policy(
    db: Session,
    *,
    name: str,
    entity_type: str | None = None,
    trigger: str | None = None,
    level: int = 1,
    channels: list[str] | None = None,
    cooldown_seconds: int = 0,
    scope_type: str | None = None,
    scope_id: str | None = None,
    min_severity: str | None = None,
    min_affected_customers: int | None = None,
    vip_only: bool = False,
    unowned_after_seconds: int | None = None,
    stale_owner_update_seconds: int | None = None,
    customer_update_due_within_seconds: int | None = None,
    unresolved_after_seconds: int | None = None,
    metadata: dict | None = None,
) -> OperationalEscalationPolicy:
    policy = OperationalEscalationPolicy(
        name=name,
        entity_type=entity_type,
        trigger=trigger,
        scope_type=scope_type,
        scope_id=scope_id,
        level=level,
        min_severity=min_severity,
        min_affected_customers=min_affected_customers,
        vip_only=vip_only,
        unowned_after_seconds=unowned_after_seconds,
        stale_owner_update_seconds=stale_owner_update_seconds,
        customer_update_due_within_seconds=customer_update_due_within_seconds,
        unresolved_after_seconds=unresolved_after_seconds,
        channels=channels or [],
        cooldown_seconds=cooldown_seconds,
        metadata_=metadata,
    )
    db.add(policy)
    db.flush()
    return policy


def update_policy(
    db: Session,
    policy: OperationalEscalationPolicy,
    *,
    name: str,
    entity_type: str,
    trigger: str,
    level: int,
    channels: list[str],
    min_severity: str | None,
    min_affected_customers: int | None,
    unresolved_after_seconds: int,
    metadata: dict | None,
    is_active: bool,
) -> OperationalEscalationPolicy:
    """Canonical mutation boundary for an operational SLA policy."""
    policy.name = name
    policy.entity_type = entity_type
    policy.trigger = trigger
    policy.level = level
    policy.channels = channels
    policy.min_severity = min_severity
    policy.min_affected_customers = min_affected_customers
    policy.unresolved_after_seconds = unresolved_after_seconds
    policy.cooldown_seconds = 0
    policy.metadata_ = metadata
    policy.is_active = is_active
    db.flush()
    return policy


def deactivate_policy(
    db: Session,
    policy: OperationalEscalationPolicy,
) -> OperationalEscalationPolicy:
    """Deactivate an SLA policy without deleting its historical identity."""
    policy.is_active = False
    db.flush()
    return policy


def record_event(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    trigger: str,
    level: int = 1,
    policy_id: str | UUID | None = None,
    severity: str | None = None,
    affected_customer_count: int | None = None,
    metadata: dict | None = None,
    triggered_at: datetime | None = None,
) -> OperationalEscalationEvent:
    event = OperationalEscalationEvent(
        entity_type=entity_type,
        entity_id=_entity_id(entity_id),
        policy_id=_uuid(policy_id),
        level=level,
        trigger=trigger,
        severity=severity,
        affected_customer_count=affected_customer_count,
        metadata_=metadata,
        triggered_at=triggered_at or datetime.now(UTC),
    )
    db.add(event)
    db.flush()
    return event


def record_event_once(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    trigger: str,
    policy: OperationalEscalationPolicy,
    severity: str | None = None,
    affected_customer_count: int | None = None,
    metadata: dict | None = None,
    triggered_at: datetime | None = None,
) -> OperationalEscalationEvent:
    """Materialize one open event for an entity, trigger, and policy."""
    existing = (
        db.query(OperationalEscalationEvent)
        .filter(OperationalEscalationEvent.entity_type == entity_type)
        .filter(OperationalEscalationEvent.entity_id == _entity_id(entity_id))
        .filter(OperationalEscalationEvent.policy_id == policy.id)
        .filter(OperationalEscalationEvent.trigger == trigger)
        .filter(
            OperationalEscalationEvent.status.in_(
                (
                    OperationalEscalationStatus.open,
                    OperationalEscalationStatus.acknowledged,
                )
            )
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    return record_event(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        trigger=trigger,
        policy_id=policy.id,
        level=policy.level,
        severity=severity,
        affected_customer_count=affected_customer_count,
        metadata=metadata,
        triggered_at=triggered_at,
    )


def emit_sla_event(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    trigger: str,
    severity: str | None = None,
    affected_customer_count: int | None = None,
    metadata: dict | None = None,
    triggered_at: datetime | None = None,
    policies: list[OperationalEscalationPolicy] | None = None,
) -> SlaEmissionResult:
    """Apply configured policies to a fact emitted by an operational owner."""
    active_policies = policies
    if active_policies is None:
        active_policies = matching_policies(
            db,
            entity_type=entity_type,
            trigger=trigger,
            severity=severity,
            affected_customer_count=affected_customer_count,
        )
    events: list[OperationalEscalationEvent] = []
    deliveries: list[OperationalEscalationDelivery] = []
    for policy in active_policies:
        event = record_event_once(
            db,
            entity_type=entity_type,
            entity_id=entity_id,
            trigger=trigger,
            policy=policy,
            severity=severity,
            affected_customer_count=affected_customer_count,
            metadata=metadata,
            triggered_at=triggered_at,
        )
        events.append(event)
        deliveries.extend(plan_policy_deliveries(db, event=event, policy=policy))
    return SlaEmissionResult(
        policy_count=len(active_policies),
        events=tuple(events),
        deliveries=tuple(deliveries),
    )


def plan_delivery(
    db: Session,
    *,
    event: OperationalEscalationEvent,
    channel: str,
    recipient_type: str,
    recipient_id: str | UUID | None = None,
    recipient_address: str | None = None,
    watcher_id: str | UUID | None = None,
    owner_id: str | UUID | None = None,
    cooldown_seconds: int = 1800,
    metadata: dict | None = None,
) -> OperationalEscalationDelivery:
    normalized_recipient_id = _entity_id(recipient_id) if recipient_id else None
    dedup_key = ":".join(
        [
            event.entity_type,
            event.entity_id,
            str(event.level),
            channel,
            recipient_type,
            normalized_recipient_id or recipient_address or "unknown",
        ]
    )
    existing = (
        db.query(OperationalEscalationDelivery)
        .filter(OperationalEscalationDelivery.dedup_key == dedup_key)
        .one_or_none()
    )
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    delivery = OperationalEscalationDelivery(
        event_id=event.id,
        watcher_id=_uuid(watcher_id),
        owner_id=_uuid(owner_id),
        channel=channel,
        recipient_type=recipient_type,
        recipient_id=normalized_recipient_id,
        recipient_address=recipient_address,
        delivery_status=OperationalDeliveryStatus.pending,
        dedup_key=dedup_key,
        escalation_level=event.level,
        cooldown_until=now + timedelta(seconds=cooldown_seconds),
        metadata_=metadata,
    )
    db.add(delivery)
    db.flush()
    return delivery


def plan_policy_deliveries(
    db: Session,
    *,
    event: OperationalEscalationEvent,
    policy: OperationalEscalationPolicy | None = None,
) -> list[OperationalEscalationDelivery]:
    active_policy = policy or event.policy
    if active_policy is None:
        return []

    deliveries: list[OperationalEscalationDelivery] = []
    escalation_delay_seconds = max(active_policy.unresolved_after_seconds or 0, 0)
    for channel_rule in _policy_channel_rules(active_policy):
        channel = channel_rule["channel"]
        recipient_groups = set(channel_rule.get("recipients") or ["owners", "watchers"])
        participant_types = set(
            channel_rule.get("participant_types") or DEFAULT_ESCALATION_PARTICIPANTS
        )
        watcher_roles = set(channel_rule.get("watcher_roles") or [])

        if "owners" in recipient_groups:
            for owner in _active_owners(db, event):
                if owner.owner_type not in participant_types:
                    continue
                deliveries.append(
                    plan_delivery(
                        db,
                        event=event,
                        channel=channel,
                        recipient_type=owner.owner_type,
                        recipient_id=_owner_recipient_id(owner),
                        owner_id=owner.id,
                        cooldown_seconds=escalation_delay_seconds,
                        metadata={"policy_id": str(active_policy.id)},
                    )
                )

        if "watchers" in recipient_groups:
            for watcher in list_watchers(
                db,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
            ):
                if watcher_roles and watcher.role not in watcher_roles:
                    continue
                if watcher.watcher_type not in participant_types:
                    continue
                recipient_id = _watcher_recipient_id(watcher)
                recipient_address = _watcher_recipient_address(db, watcher, channel)
                if (
                    watcher.watcher_type
                    in {
                        OperationalParticipantType.subscriber,
                        OperationalParticipantType.external,
                    }
                    and channel in DIRECT_ADDRESS_CHANNELS
                    and not recipient_address
                ):
                    continue
                deliveries.append(
                    plan_delivery(
                        db,
                        event=event,
                        channel=channel,
                        recipient_type=watcher.watcher_type,
                        recipient_id=recipient_id,
                        recipient_address=recipient_address,
                        watcher_id=watcher.id,
                        cooldown_seconds=escalation_delay_seconds,
                        metadata={"policy_id": str(active_policy.id)},
                    )
                )

        if "subscriber" in recipient_groups and _participant_allowed(
            channel_rule,
            participant_types,
            OperationalParticipantType.subscriber,
        ):
            for subscriber_id in _event_subscriber_ids(event.metadata_):
                deliveries.append(
                    plan_delivery(
                        db,
                        event=event,
                        channel=channel,
                        recipient_type=OperationalParticipantType.subscriber,
                        recipient_id=subscriber_id,
                        cooldown_seconds=escalation_delay_seconds,
                        metadata={"policy_id": str(active_policy.id)},
                    )
                )

        if "reseller" in recipient_groups and _participant_allowed(
            channel_rule,
            participant_types,
            OperationalParticipantType.reseller,
        ):
            for reseller_id in _event_reseller_ids(db, event.metadata_):
                deliveries.append(
                    plan_delivery(
                        db,
                        event=event,
                        channel=channel,
                        recipient_type=OperationalParticipantType.reseller,
                        recipient_id=reseller_id,
                        cooldown_seconds=escalation_delay_seconds,
                        metadata={"policy_id": str(active_policy.id)},
                    )
                )

    return deliveries


def _policy_channel_rules(policy: OperationalEscalationPolicy) -> list[dict]:
    rules: list[dict] = []
    defaults = (policy.metadata_ or {}).get("delivery_defaults") or {}
    for raw_channel in policy.channels or []:
        if isinstance(raw_channel, str):
            rule = {"channel": raw_channel}
        elif isinstance(raw_channel, dict) and raw_channel.get("channel"):
            rule = dict(raw_channel)
        else:
            continue
        for key, value in defaults.items():
            rule.setdefault(key, value)
        rules.append(rule)
    return rules


def _participant_allowed(
    channel_rule: dict,
    participant_types: set,
    participant_type: str,
) -> bool:
    return (
        "participant_types" not in channel_rule or participant_type in participant_types
    )


def _event_subscriber_ids(metadata: dict | None) -> list[str]:
    return _metadata_ids(
        metadata,
        singular_keys=("subscriber_id", "customer_id", "account_id"),
        plural_keys=("subscriber_ids", "customer_ids", "account_ids"),
    )


def _event_reseller_ids(db: Session, metadata: dict | None) -> list[str]:
    reseller_ids = _metadata_ids(
        metadata,
        singular_keys=("reseller_id", "partner_id"),
        plural_keys=("reseller_ids", "partner_ids"),
    )
    seen = set(reseller_ids)
    for subscriber_id in _event_subscriber_ids(metadata):
        subscriber = db.get(Subscriber, _uuid(subscriber_id))
        if subscriber is None or subscriber.reseller_id is None:
            continue
        reseller = db.get(Reseller, subscriber.reseller_id)
        if reseller is None or reseller.is_house:
            continue
        value = str(subscriber.reseller_id)
        if value not in seen:
            seen.add(value)
            reseller_ids.append(value)
    return reseller_ids


def _metadata_ids(
    metadata: dict | None,
    *,
    singular_keys: tuple[str, ...],
    plural_keys: tuple[str, ...],
) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for key in singular_keys:
        value = metadata.get(key)
        if value:
            text = str(value)
            if text not in seen:
                seen.add(text)
                values.append(text)
    for key in plural_keys:
        raw_values = metadata.get(key)
        if isinstance(raw_values, (str, bytes)) or raw_values is None:
            raw_values = [raw_values] if raw_values else []
        if not isinstance(raw_values, (list, tuple, set)):
            continue
        for value in raw_values:
            if not value:
                continue
            text = str(value)
            if text not in seen:
                seen.add(text)
                values.append(text)
    return values


def _active_owners(
    db: Session,
    event: OperationalEscalationEvent,
) -> list[OperationalOwner]:
    return list(
        db.query(OperationalOwner)
        .filter(OperationalOwner.entity_type == event.entity_type)
        .filter(OperationalOwner.entity_id == event.entity_id)
        .filter(OperationalOwner.is_active.is_(True))
        .order_by(OperationalOwner.assigned_at.asc())
        .all()
    )


def _owner_recipient_id(owner: OperationalOwner) -> str | UUID | None:
    if owner.service_team_id is not None:
        return owner.service_team_id
    if owner.person_id is not None:
        return owner.person_id
    return owner.duty_role


def _watcher_recipient_id(watcher: OperationalWatcher) -> str | UUID | None:
    if watcher.service_team_id is not None:
        return watcher.service_team_id
    if watcher.person_id is not None:
        return watcher.person_id
    if watcher.subscriber_id is not None:
        return watcher.subscriber_id
    return watcher.duty_role


def _watcher_recipient_address(
    db: Session,
    watcher: OperationalWatcher,
    channel: str,
) -> str | None:
    metadata = watcher.metadata_ or {}
    channel_addresses = metadata.get("channels")
    if isinstance(channel_addresses, dict) and channel_addresses.get(channel):
        return str(channel_addresses[channel])
    explicit_address = metadata.get(f"{channel}_address")
    if explicit_address:
        return str(explicit_address)

    if watcher.subscriber_id is not None:
        subscriber = db.get(Subscriber, watcher.subscriber_id)
        if subscriber is None:
            return None
        if channel == "email":
            return subscriber.email
        if channel in {"sms", "whatsapp"}:
            return subscriber.phone

    return None


def mark_delivery_sent(
    db: Session,
    delivery: OperationalEscalationDelivery,
    *,
    sent_at: datetime | None = None,
) -> OperationalEscalationDelivery:
    delivery.delivery_status = OperationalDeliveryStatus.sent
    delivery.sent_at = sent_at or datetime.now(UTC)
    db.flush()
    return delivery


def acknowledge_event(
    db: Session,
    event: OperationalEscalationEvent,
    *,
    person_id: str | UUID | None = None,
    acknowledged_at: datetime | None = None,
) -> OperationalEscalationEvent:
    now = acknowledged_at or datetime.now(UTC)
    event.status = OperationalEscalationStatus.acknowledged
    event.acknowledged_by_person_id = _uuid(person_id)
    event.acknowledged_at = now
    for delivery in event.deliveries:
        if delivery.delivery_status in {
            OperationalDeliveryStatus.pending,
            OperationalDeliveryStatus.sent,
        }:
            delivery.delivery_status = OperationalDeliveryStatus.acknowledged
            delivery.acknowledged_at = now
    db.flush()
    return event


def cancel_entity_events(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | UUID,
    trigger: str | None = None,
    reason: str | None = None,
    canceled_at: datetime | None = None,
) -> list[OperationalEscalationEvent]:
    now = canceled_at or datetime.now(UTC)
    query = (
        db.query(OperationalEscalationEvent)
        .filter(OperationalEscalationEvent.entity_type == entity_type)
        .filter(OperationalEscalationEvent.entity_id == _entity_id(entity_id))
        .filter(OperationalEscalationEvent.status == OperationalEscalationStatus.open)
    )
    if trigger:
        query = query.filter(OperationalEscalationEvent.trigger == trigger)
    events = query.all()
    for event in events:
        event.status = OperationalEscalationStatus.canceled
        event.resolved_at = now
        for delivery in event.deliveries:
            if delivery.delivery_status == OperationalDeliveryStatus.pending:
                delivery.delivery_status = OperationalDeliveryStatus.suppressed
                delivery.error_message = reason
                delivery.metadata_ = {
                    **(delivery.metadata_ or {}),
                    "suppressed_reason": reason,
                }
    db.flush()
    return list(events)
