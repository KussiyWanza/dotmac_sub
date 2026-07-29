"""Reconciler for the unified device projection (``device_projections``).

``network.device_projection`` is the sole canonical writer of the
``device_projections`` table. This reconciler is that writer: it runs the
authoritative multi-source device derivation (:func:`collect_devices`, which
aggregates OLTs, core ``NetworkDevice`` rows, ONTs and CPEs and derives each
one's operational status) and projects the result into one materialised row per
device.

The pass is idempotent and self-healing:

* every derived device is upserted on its ``(device_type, source_id)`` natural
  key, so re-running with unchanged inputs converges to the same rows;
* ``refreshed_at`` is stamped on every upserted row, carrying freshness;
* rows whose source device no longer exists are pruned, so the table cannot
  drift into holding phantom devices.

Pruning follows *existence*, not admission. A deactivated device still exists,
so it is still derived and still projected — marked ``lifecycle_state =
'inactive'`` and forced to ``not_working`` by :func:`_gated_status`. Filtering
inactive devices out of the derivation instead made deactivation delete the
device from the staff ledger, because this reconciler removes whatever the
derivation stops returning.

The table is a rebuildable cache — the authoritative device tables remain the
source of truth. Callers that need a live device list read the projection; they
never write it, and they request a reconcile rather than maintaining a parallel
derivation path.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceProjection
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.web_network_core_devices_inventory import collect_devices

logger = logging.getLogger(__name__)

# Columns copied verbatim from a derived device dict onto its projection row.
_PROJECTED_FIELDS = (
    "class_facts",
    "name",
    "serial_number",
    "ip_address",
    "vendor",
    "model",
    "operational_reason",
    "last_seen",
)

_RECONCILE_COMMAND = OwnerCommandDefinition(
    owner="network.device_projection",
    concern="device_projections materialised table",
    name="reconcile_device_projections",
)
# Stable PostgreSQL transaction-level advisory lock. It prevents two beat or
# operator-triggered rebuilds from racing the natural-key upsert.
_RECONCILE_LOCK_KEY = 328_160_319

_LIFECYCLE_STATES = ("active", "inactive")


def _gated_status(lifecycle_state: str, status: str) -> str:
    """Release gate: an inactive device can never project ``working``.

    An inactive device has left the poll sweep, so nothing refreshes its
    reachability — anything it still claims is frozen, and a frozen ``working``
    is what let a decommissioned device keep vetoing outage detection. The
    matching CHECK constraint makes the violation unrepresentable in the
    database; this normalisation is what keeps the reconciler *converging*
    instead of crashing the beat job on a bad upstream derivation.
    """
    if lifecycle_state != "active" and status != "not_working":
        logger.warning(
            "device_projection: forcing inactive device status %r -> not_working",
            status,
        )
        return "not_working"
    return status


class DeviceProjectionCommandError(DomainError):
    """Stable validation failure for the projection reconcile command."""


@dataclass(frozen=True)
class ReconcileDeviceProjectionsCommand:
    """Typed request to rebuild the canonical network-device projection."""

    context: CommandContext
    reconciled_at: datetime | None = None


@dataclass(frozen=True)
class DeviceProjectionReconcileResult:
    """Outcome of a single reconcile pass."""

    inserted: int
    updated: int
    pruned: int
    reconciled_at: datetime
    command_id: uuid.UUID
    correlation_id: uuid.UUID

    @property
    def total(self) -> int:
        """Rows present in the projection after this pass."""
        return self.inserted + self.updated


def _subscriber_id(subscriber: object) -> uuid.UUID | None:
    """Best-effort extraction of a subscriber UUID from the derived dict.

    The derivation currently carries ``None`` for every device, but accept an
    id or an object with an ``id`` so the projection keeps working if the
    derivation starts linking CPE to subscribers.
    """
    if subscriber is None:
        return None
    candidate = getattr(subscriber, "id", subscriber)
    return cast("uuid.UUID | None", coerce_uuid(candidate))


def _acquire_reconcile_lock(db: Session) -> None:
    """Serialize projection rebuilds across PostgreSQL workers."""

    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _RECONCILE_LOCK_KEY},
        )


def _validate_command(command: ReconcileDeviceProjectionsCommand) -> datetime:
    stamp = command.reconciled_at or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise DeviceProjectionCommandError(
            code="network.device_projection.invalid_command",
            message="Projection reconciliation time must include a UTC offset.",
            details={"field": "reconciled_at"},
        )
    return stamp.astimezone(UTC)


def _reconcile(
    db: Session,
    *,
    command: ReconcileDeviceProjectionsCommand,
    stamp: datetime,
) -> DeviceProjectionReconcileResult:
    _acquire_reconcile_lock(db)

    existing: dict[tuple[str, str], DeviceProjection] = {
        (row.device_type, row.source_id): row
        for row in db.execute(select(DeviceProjection)).scalars()
    }

    seen: set[tuple[str, str]] = set()
    inserted = 0
    updated = 0

    for device in collect_devices(db):
        device_type = str(device["type"])
        source_id = str(device["id"])
        key = (device_type, source_id)
        seen.add(key)

        lifecycle_state = str(device.get("lifecycle_state") or "active")
        if lifecycle_state not in _LIFECYCLE_STATES:
            lifecycle_state = "active"
        status = _gated_status(
            lifecycle_state, str(device.get("status") or "not_working")
        )
        subscriber_id = _subscriber_id(device.get("subscriber"))

        row = existing.get(key)
        if row is None:
            row = DeviceProjection(
                device_type=device_type,
                source_id=source_id,
                operational_status=status,
                lifecycle_state=lifecycle_state,
                subscriber_id=subscriber_id,
                refreshed_at=stamp,
            )
            for field in _PROJECTED_FIELDS:
                setattr(row, field, device.get(field))
            db.add(row)
            inserted += 1
        else:
            row.operational_status = status
            row.lifecycle_state = lifecycle_state
            row.subscriber_id = subscriber_id
            row.refreshed_at = stamp
            for field in _PROJECTED_FIELDS:
                setattr(row, field, device.get(field))
            updated += 1

    pruned = 0
    for key, row in existing.items():
        if key not in seen:
            db.delete(row)
            pruned += 1

    emit_event(
        db,
        EventType.device_projection_reconciled,
        {
            "schema_version": 1,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "causation_id": (
                str(command.context.causation_id)
                if command.context.causation_id is not None
                else None
            ),
            "idempotency_key": command.context.idempotency_key,
            "aggregate_type": "device_projection",
            "aggregate_id": "network:global",
            "aggregate_version": str(command.context.command_id),
            "scope": command.context.scope,
            "reason": command.context.reason,
            "reconciled_at": stamp.isoformat(),
            "inserted": inserted,
            "updated": updated,
            "pruned": pruned,
        },
        actor=command.context.actor,
    )

    logger.info(
        "device_projection reconcile staged: %d inserted, %d updated, %d pruned",
        inserted,
        updated,
        pruned,
    )
    return DeviceProjectionReconcileResult(
        inserted=inserted,
        updated=updated,
        pruned=pruned,
        reconciled_at=stamp,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
    )


def reconcile_device_projections(
    db: Session,
    command: ReconcileDeviceProjectionsCommand,
) -> DeviceProjectionReconcileResult:
    """Rebuild the projection in a manifest-verified owner transaction.

    Idempotent: safe to request on any schedule. The session must have no
    active caller transaction; success or failure completes before return.
    """

    def operation() -> DeviceProjectionReconcileResult:
        stamp = _validate_command(command)
        return _reconcile(db, command=command, stamp=stamp)

    return execute_owner_command(
        db,
        definition=_RECONCILE_COMMAND,
        context=command.context,
        operation=operation,
    )
