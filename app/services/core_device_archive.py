"""Reviewed archive and restore lifecycle for core network devices."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice
from app.models.forwarding_topology import ForwardingTopologyDeclaration
from app.models.network_monitoring import NetworkDevice, NetworkDeviceLifecycleState
from app.models.router_management import Router
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.outage_impact import resolve_node_impact
from app.services.network_monitoring import set_network_device_active
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

ARCHIVE_SCOPE = "network:device:archive"
_ARCHIVE_COMMAND = OwnerCommandDefinition(
    owner="network.core_device_archive",
    concern="reviewed core device archive and restoration",
    name="archive_core_device",
)
_RESTORE_COMMAND = OwnerCommandDefinition(
    owner="network.core_device_archive",
    concern="reviewed core device archive and restoration",
    name="restore_core_device",
)


class CoreDeviceArchiveError(DomainError):
    """Stable transport-neutral archive lifecycle failure."""


def _error(suffix: str, message: str, **details: object) -> CoreDeviceArchiveError:
    return CoreDeviceArchiveError(
        code=f"network.core_device_archive.{suffix}",
        message=message,
        details=details,
    )


# Compatibility name for the archive owner's public outcomes. The shared enum
# is defined alongside the persisted lifecycle facts so read projections and
# command outcomes cannot drift onto different vocabularies.
CoreDeviceLifecycle = NetworkDeviceLifecycleState


class CoreDeviceMutation(StrEnum):
    EDIT = "edit"
    DEACTIVATE = "deactivate"
    PROVISIONING_ACCESS = "provisioning_access"
    INTERFACE_MONITORING = "interface_monitoring"
    GRAPH_CONFIGURATION = "graph_configuration"
    BACKUP_SETTINGS = "backup_settings"
    BACKUP_TRIGGER = "backup_trigger"
    PING = "ping"
    REBOOT = "reboot"


@dataclass(frozen=True, slots=True)
class ArchivePreviewFingerprint:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) != 64 or any(
            character not in "0123456789abcdef" for character in self.value
        ):
            raise ValueError("archive preview fingerprint must be lowercase SHA-256")

    @classmethod
    def parse(cls, value: str) -> ArchivePreviewFingerprint:
        normalized = value.strip().lower()
        try:
            return cls(normalized)
        except ValueError as exc:
            raise _error(
                "invalid_preview_fingerprint",
                "Review the decommission impact again before confirming.",
            ) from exc


@dataclass(frozen=True, slots=True)
class PreviewCoreDeviceArchiveRequest:
    device_id: UUID


@dataclass(frozen=True, slots=True)
class RequireCoreDeviceMutableRequest:
    device_id: UUID
    mutation: CoreDeviceMutation


@dataclass(frozen=True, slots=True)
class CoreDeviceMutationEligibility:
    device_id: UUID
    lifecycle_state: CoreDeviceLifecycle
    mutation: CoreDeviceMutation


@dataclass(frozen=True, slots=True)
class CoreDeviceArchivePreview:
    device_id: UUID
    device_name: str
    lifecycle_state: CoreDeviceLifecycle
    allowed: bool
    fingerprint: ArchivePreviewFingerprint
    active_child_ids: tuple[UUID, ...]
    active_forwarding_declaration_ids: tuple[UUID, ...]
    linked_nas_ids: tuple[UUID, ...]
    linked_router_ids: tuple[UUID, ...]
    affected_customer_count: int
    blockers: tuple[str, ...]
    archived_at: datetime | None = None

    @property
    def affected_count(self) -> int:
        return (
            len(self.active_child_ids)
            + len(self.active_forwarding_declaration_ids)
            + len(self.linked_nas_ids)
            + len(self.linked_router_ids)
            + self.affected_customer_count
        )


@dataclass(frozen=True, slots=True)
class ArchiveCoreDeviceCommand:
    context: CommandContext
    device_id: UUID
    expected_preview_fingerprint: ArchivePreviewFingerprint


@dataclass(frozen=True, slots=True)
class RestoreCoreDeviceCommand:
    context: CommandContext
    device_id: UUID


@dataclass(frozen=True, slots=True)
class CoreDeviceArchiveOutcome:
    device_id: UUID
    device_name: str
    lifecycle_state: CoreDeviceLifecycle
    archived_at: datetime | None
    replayed: bool


def _actor(context: CommandContext) -> AuditActor:
    prefix, separator, identifier = context.actor.partition(":")
    actor_id = identifier if separator and identifier else context.actor
    if prefix == "api_key":
        return AuditActor.api_key(actor_id)
    if prefix == "user":
        return AuditActor.user(actor_id)
    if prefix == "service":
        return AuditActor.service(actor_id)
    return AuditActor.system(actor_id)


def _device(db: Session, device_id: UUID, *, lock: bool = False) -> NetworkDevice:
    stmt = select(NetworkDevice).where(NetworkDevice.id == device_id)
    if lock:
        stmt = stmt.with_for_update()
    device = db.scalar(stmt)
    if device is None:
        raise _error(
            "device_not_found",
            "The core device was not found.",
            device_id=str(device_id),
        )
    return device


def _affected_customer_count(db: Session, device: NetworkDevice) -> int:
    try:
        impact = resolve_node_impact(db, device)
    except Exception as exc:
        raise _error(
            "impact_unavailable",
            "Decommission impact could not be calculated. Try again after topology "
            "is available.",
            device_id=str(device.id),
        ) from exc
    return impact.affected_count


def _preview(db: Session, device: NetworkDevice) -> CoreDeviceArchivePreview:
    child_ids = tuple(
        db.scalars(
            select(NetworkDevice.id)
            .where(
                NetworkDevice.parent_device_id == device.id,
                NetworkDevice.is_active.is_(True),
                NetworkDevice.archived_at.is_(None),
            )
            .order_by(NetworkDevice.id)
        ).all()
    )
    forwarding_ids = tuple(
        db.scalars(
            select(ForwardingTopologyDeclaration.id)
            .where(
                ForwardingTopologyDeclaration.active.is_(True),
                or_(
                    ForwardingTopologyDeclaration.downstream_device_id == device.id,
                    ForwardingTopologyDeclaration.upstream_device_id == device.id,
                ),
            )
            .order_by(ForwardingTopologyDeclaration.id)
        ).all()
    )
    nas_ids = tuple(
        db.scalars(
            select(NasDevice.id)
            .where(
                NasDevice.network_device_id == device.id,
                NasDevice.is_active.is_(True),
            )
            .order_by(NasDevice.id)
        ).all()
    )
    router_ids = tuple(
        db.scalars(
            select(Router.id)
            .where(
                Router.network_device_id == device.id,
                Router.is_active.is_(True),
            )
            .order_by(Router.id)
        ).all()
    )
    affected_count = _affected_customer_count(db, device)
    blockers: list[str] = []
    if device.archived_at is not None:
        blockers.append("Device is already decommissioned")
    if child_ids:
        blockers.append(f"{len(child_ids)} active child device(s)")
    if forwarding_ids:
        blockers.append(f"{len(forwarding_ids)} active forwarding declaration(s)")
    if nas_ids:
        blockers.append(f"{len(nas_ids)} active linked NAS record(s)")
    if router_ids:
        blockers.append(f"{len(router_ids)} active linked router record(s)")
    if affected_count:
        blockers.append(f"{affected_count} affected active customer(s)")

    evidence = {
        "device_id": str(device.id),
        "updated_at": device.updated_at.isoformat() if device.updated_at else None,
        "archived_at": device.archived_at.isoformat() if device.archived_at else None,
        "active_child_ids": [str(value) for value in child_ids],
        "active_forwarding_declaration_ids": [str(value) for value in forwarding_ids],
        "linked_nas_ids": [str(value) for value in nas_ids],
        "linked_router_ids": [str(value) for value in router_ids],
        "affected_customer_count": affected_count,
    }
    fingerprint = ArchivePreviewFingerprint(
        hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    return CoreDeviceArchivePreview(
        device_id=device.id,
        device_name=device.name,
        lifecycle_state=(
            CoreDeviceLifecycle.ARCHIVED
            if device.archived_at
            else (
                CoreDeviceLifecycle.ACTIVE
                if device.is_active
                else CoreDeviceLifecycle.INACTIVE
            )
        ),
        allowed=not blockers,
        fingerprint=fingerprint,
        active_child_ids=child_ids,
        active_forwarding_declaration_ids=forwarding_ids,
        linked_nas_ids=nas_ids,
        linked_router_ids=router_ids,
        affected_customer_count=affected_count,
        blockers=tuple(blockers),
        archived_at=device.archived_at,
    )


def preview_core_device_archive(
    db: Session, request: PreviewCoreDeviceArchiveRequest
) -> CoreDeviceArchivePreview:
    """Return authoritative archive eligibility and impact without writing."""
    return _preview(db, _device(db, request.device_id))


def require_core_device_mutable(
    db: Session, request: RequireCoreDeviceMutableRequest
) -> CoreDeviceMutationEligibility:
    """Fail closed when an older mutation path targets an archived device."""
    device = _device(db, request.device_id)
    lifecycle_state = (
        CoreDeviceLifecycle.ARCHIVED
        if device.archived_at is not None
        else (
            CoreDeviceLifecycle.ACTIVE
            if device.is_active
            else CoreDeviceLifecycle.INACTIVE
        )
    )
    if lifecycle_state is CoreDeviceLifecycle.ARCHIVED:
        raise _error(
            "archived_device_read_only",
            "Restore this decommissioned device before changing or operating it.",
            device_id=str(device.id),
            mutation=request.mutation.value,
        )
    return CoreDeviceMutationEligibility(
        device_id=device.id,
        lifecycle_state=lifecycle_state,
        mutation=request.mutation,
    )


def _validate_context(context: CommandContext) -> None:
    if context.scope != ARCHIVE_SCOPE:
        raise _error(
            "scope_mismatch", "Core-device decommission permission is required."
        )
    reason = context.reason.strip()
    if len(reason) < 3 or len(reason) > 500:
        raise _error(
            "invalid_reason",
            "A lifecycle reason between 3 and 500 characters is required.",
        )


def _stage_evidence(
    db: Session,
    *,
    device: NetworkDevice,
    context: CommandContext,
    event_type: EventType,
    action: str,
    archived_at: datetime | None,
) -> None:
    actor = _actor(context)
    metadata: dict[str, object] = {
        "schema_version": 1,
        "device_id": str(device.id),
        "device_name": device.name,
        "lifecycle_state": "archived" if archived_at else "inactive",
        "archived_at": archived_at.isoformat() if archived_at else None,
        "reason": context.reason,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
    }
    stage_audit_event(
        db,
        action=action,
        entity_type="core_device",
        entity_id=str(device.id),
        actor=actor,
        request_id=str(context.correlation_id),
        metadata=metadata,
    )
    emit_event(db, event_type, metadata, actor=context.actor)


def archive_core_device(
    db: Session, command: ArchiveCoreDeviceCommand
) -> CoreDeviceArchiveOutcome:
    """Archive one reviewed core device atomically and retain its history."""

    def operation() -> CoreDeviceArchiveOutcome:
        _validate_context(command.context)
        device = _device(db, command.device_id, lock=True)
        if device.archived_at is not None:
            return CoreDeviceArchiveOutcome(
                device_id=device.id,
                device_name=device.name,
                lifecycle_state=CoreDeviceLifecycle.ARCHIVED,
                archived_at=device.archived_at,
                replayed=True,
            )
        preview = _preview(db, device)
        if preview.fingerprint != command.expected_preview_fingerprint:
            raise _error(
                "stale_preview",
                "The device or its dependencies changed. Review the decommission "
                "impact again.",
            )
        if not preview.allowed:
            raise _error(
                "dependencies_block_archive",
                "Resolve the listed dependencies before decommissioning this device.",
                blockers=list(preview.blockers),
            )
        archived_at = datetime.now(UTC)
        set_network_device_active(
            db, device, False, reason="core_device_archive", now=archived_at
        )
        device.archived_at = archived_at
        device.archived_by = command.context.actor[:160]
        device.archive_reason = command.context.reason.strip()
        db.flush()
        _stage_evidence(
            db,
            device=device,
            context=command.context,
            event_type=EventType.network_device_archived,
            action="network.core_device_archived",
            archived_at=archived_at,
        )
        return CoreDeviceArchiveOutcome(
            device_id=device.id,
            device_name=device.name,
            lifecycle_state=CoreDeviceLifecycle.ARCHIVED,
            archived_at=archived_at,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_ARCHIVE_COMMAND,
        context=command.context,
        operation=operation,
    )


def restore_core_device(
    db: Session, command: RestoreCoreDeviceCommand
) -> CoreDeviceArchiveOutcome:
    """Restore an archived device to visible, inactive inventory."""

    def operation() -> CoreDeviceArchiveOutcome:
        _validate_context(command.context)
        device = _device(db, command.device_id, lock=True)
        if device.archived_at is None:
            return CoreDeviceArchiveOutcome(
                device_id=device.id,
                device_name=device.name,
                lifecycle_state=(
                    CoreDeviceLifecycle.ACTIVE
                    if device.is_active
                    else CoreDeviceLifecycle.INACTIVE
                ),
                archived_at=None,
                replayed=True,
            )
        set_network_device_active(
            db,
            device,
            False,
            reason="core_device_restore_inactive",
            now=datetime.now(UTC),
        )
        device.archived_at = None
        device.archived_by = None
        device.archive_reason = None
        db.flush()
        _stage_evidence(
            db,
            device=device,
            context=command.context,
            event_type=EventType.network_device_restored,
            action="network.core_device_restored",
            archived_at=None,
        )
        return CoreDeviceArchiveOutcome(
            device_id=device.id,
            device_name=device.name,
            lifecycle_state=CoreDeviceLifecycle.INACTIVE,
            archived_at=None,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_RESTORE_COMMAND,
        context=command.context,
        operation=operation,
    )
