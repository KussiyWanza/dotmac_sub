"""Audit boundary for operational services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.schemas.audit import AuditEventCreate
from app.services.adapters import adapter_registry


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Typed audit principal plus optional canonical Party enrichment.

    ``actor_id`` identifies the authenticating principal or automated
    component. ``party_id`` is accountability enrichment only: it may describe
    the person behind a user or API key, but it can never turn a system or
    service actor into a person.
    """

    actor_type: AuditActorType
    actor_id: str | None = None
    label: str | None = None
    party_id: UUID | None = None

    def __post_init__(self) -> None:
        actor_id = self.actor_id.strip() if self.actor_id is not None else None
        label = self.label.strip() if self.label is not None else None
        if self.actor_type is not AuditActorType.system and not actor_id:
            raise ValueError(
                f"audit actor type {self.actor_type.value!r} needs a non-empty actor_id"
            )
        if (
            self.actor_type in {AuditActorType.system, AuditActorType.service}
            and self.party_id is not None
        ):
            raise ValueError(
                f"audit actor type {self.actor_type.value!r} cannot carry a "
                "Party identity"
            )
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "label", label or None)

    @classmethod
    def system(
        cls,
        component_id: str | None = None,
        *,
        label: str | None = None,
    ) -> AuditActor:
        return cls(
            actor_type=AuditActorType.system,
            actor_id=component_id,
            label=label,
        )

    @classmethod
    def service(
        cls,
        service_principal_id: str,
        *,
        label: str | None = None,
    ) -> AuditActor:
        return cls(
            actor_type=AuditActorType.service,
            actor_id=service_principal_id,
            label=label,
        )

    @classmethod
    def user(
        cls,
        principal_id: str,
        *,
        label: str | None = None,
        party_id: UUID | None = None,
    ) -> AuditActor:
        return cls(
            actor_type=AuditActorType.user,
            actor_id=principal_id,
            label=label,
            party_id=party_id,
        )

    @classmethod
    def api_key(
        cls,
        key_id: str,
        *,
        label: str | None = None,
        party_id: UUID | None = None,
    ) -> AuditActor:
        return cls(
            actor_type=AuditActorType.api_key,
            actor_id=key_id,
            label=label,
            party_id=party_id,
        )


@dataclass(frozen=True, slots=True)
class AuditRecord:
    action: str
    entity_type: str
    entity_id: str | None = None
    actor: AuditActor = field(default_factory=AuditActor.system)
    status_code: int | None = None
    is_success: bool = True
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    details: dict[str, object] = field(default_factory=dict)
    occurred_at: datetime | None = None


class AuditAdapter:
    """Unified audit writer for operations."""

    name = "audit"
    depends_on: tuple[str, ...] = ("db.session.sqlalchemy",)

    def build_payload(self, record: AuditRecord) -> AuditEventCreate:
        # DB requires status_code NOT NULL; default to 200 for in-band callers
        # that don't carry an HTTP status (workflows, bulk ops, etc.)
        status_code = record.status_code
        if status_code is None:
            status_code = 200 if record.is_success else 500
        return AuditEventCreate(
            actor_type=record.actor.actor_type,
            actor_id=record.actor.actor_id,
            actor_label=record.actor.label,
            actor_party_id=record.actor.party_id,
            action=record.action,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            status_code=status_code,
            is_success=record.is_success,
            ip_address=record.ip_address,
            user_agent=record.user_agent,
            request_id=record.request_id,
            metadata_=dict(record.metadata or {}),
            details=dict(record.details or {}),
            occurred_at=record.occurred_at,
        )

    def record(
        self,
        db: Session,
        record: AuditRecord,
        *,
        defer_until_commit: bool = False,
    ) -> AuditEvent:
        from app.services import audit as audit_service

        return audit_service.audit_events.record(
            db,
            self.build_payload(record),
            defer_until_commit=defer_until_commit,
        )

    def stage(self, db: Session, record: AuditRecord) -> AuditEvent:
        """Stage an audit event in the caller-owned transaction."""
        from app.services import audit as audit_service

        return audit_service.audit_events.stage(
            db,
            self.build_payload(record),
        )

    def list_events(
        self,
        db: Session,
        *,
        actor_id: str | None = None,
        actor_search: str | None = None,
        actor_type: AuditActorType | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        request_id: str | None = None,
        is_success: bool | None = None,
        status_code: int | None = None,
        is_active: bool | None = None,
        order_by: str = "occurred_at",
        order_dir: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        from app.services import audit as audit_service

        return audit_service.audit_events.list(
            db=db,
            actor_id=actor_id,
            actor_search=actor_search,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            request_id=request_id,
            is_success=is_success,
            status_code=status_code,
            is_active=is_active,
            order_by=order_by,
            order_dir=order_dir,
            limit=limit,
            offset=offset,
        )


audit_adapter = AuditAdapter()
adapter_registry.register(audit_adapter)


def _resolve_actor(
    *,
    actor: AuditActor | None,
    actor_type: AuditActorType,
    actor_id: str | None,
    actor_label: str | None,
    actor_party_id: UUID | None,
) -> AuditActor:
    """Normalize the temporary scalar compatibility surface once."""

    if actor is not None:
        legacy_values_are_present = bool(
            actor_type is not AuditActorType.system
            or actor_id is not None
            or actor_label is not None
            or actor_party_id is not None
        )
        if legacy_values_are_present:
            raise ValueError(
                "typed audit actor cannot be combined with legacy actor fields"
            )
        return actor
    return AuditActor(
        actor_type=actor_type,
        actor_id=actor_id,
        label=actor_label,
        party_id=actor_party_id,
    )


def record_audit_event(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor: AuditActor | None = None,
    actor_type: AuditActorType = AuditActorType.system,
    actor_id: str | None = None,
    actor_label: str | None = None,
    actor_party_id: UUID | None = None,
    metadata: dict[str, object] | None = None,
    details: dict[str, object] | None = None,
    status_code: int | None = None,
    is_success: bool = True,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    occurred_at: datetime | None = None,
    defer_until_commit: bool = False,
) -> AuditEvent:
    return audit_adapter.record(
        db,
        AuditRecord(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=_resolve_actor(
                actor=actor,
                actor_type=actor_type,
                actor_id=actor_id,
                actor_label=actor_label,
                actor_party_id=actor_party_id,
            ),
            metadata=dict(metadata or {}),
            details=dict(details or {}),
            status_code=status_code,
            is_success=is_success,
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
            occurred_at=occurred_at,
        ),
        defer_until_commit=defer_until_commit,
    )


def stage_audit_event(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor: AuditActor | None = None,
    actor_type: AuditActorType = AuditActorType.system,
    actor_id: str | None = None,
    actor_label: str | None = None,
    actor_party_id: UUID | None = None,
    metadata: dict[str, object] | None = None,
    details: dict[str, object] | None = None,
    status_code: int | None = None,
    is_success: bool = True,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    occurred_at: datetime | None = None,
) -> AuditEvent:
    return audit_adapter.stage(
        db,
        AuditRecord(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=_resolve_actor(
                actor=actor,
                actor_type=actor_type,
                actor_id=actor_id,
                actor_label=actor_label,
                actor_party_id=actor_party_id,
            ),
            metadata=dict(metadata or {}),
            details=dict(details or {}),
            status_code=status_code,
            is_success=is_success,
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
            occurred_at=occurred_at,
        ),
    )
