"""Lifecycle binding for the ONT reconciler's current-state projection.

This module is the only writer of the configuration identity attached to
``OntUnit.sync_status``/``last_error``. Configuration coordinators and inventory
flows call these typed, flush-only participants; adapters never clear status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntSyncStatus, OntUnit
from app.models.ont_observation import OntObservation
from app.models.ont_service_configuration import (
    OntServiceConfigurationHead,
    OntServiceConfigurationPhase,
    OntServiceConfigurationRevision,
)
from app.services.events import emit_event
from app.services.events.types import EventType


@dataclass(frozen=True, slots=True)
class ReconcileLifecycleBinding:
    """Exact configuration identity whose outcome may update current status."""

    ont_unit_id: UUID
    assignment_id: UUID
    configuration_head_id: UUID
    desired_revision: int
    operation_id: UUID


@dataclass(frozen=True, slots=True)
class RetireOntReconcileProjectionForInventory:
    """Typed inventory participant input after external cleanup succeeded."""

    ont_unit_id: UUID
    assignment_ids: tuple[UUID, ...]
    actor: str
    reason: str


@dataclass(frozen=True, slots=True)
class RetireOntReconcileProjectionOutcome:
    ont_unit_id: UUID
    retired_head_ids: tuple[UUID, ...]
    observation_invalidated: bool
    replayed: bool


def reconcile_binding_matches(ont: OntUnit, binding: ReconcileLifecycleBinding) -> bool:
    """Whether the current fault/status belongs to exactly this operation."""

    return (
        ont.id == binding.ont_unit_id
        and ont.reconcile_configuration_head_id == binding.configuration_head_id
        and ont.reconcile_assignment_id == binding.assignment_id
        and ont.reconcile_desired_revision == binding.desired_revision
        and ont.reconcile_operation_id == binding.operation_id
    )


def bind_reconcile_projection(ont: OntUnit, binding: ReconcileLifecycleBinding) -> None:
    """Bind subsequent status/error writes to one exact configuration attempt."""

    if ont.id != binding.ont_unit_id:
        raise ValueError("Reconcile lifecycle binding targets another ONT")
    ont.reconcile_configuration_head_id = binding.configuration_head_id
    ont.reconcile_assignment_id = binding.assignment_id
    ont.reconcile_desired_revision = binding.desired_revision
    ont.reconcile_operation_id = binding.operation_id


def retire_ont_reconcile_projection_for_inventory(
    db: Session,
    command: RetireOntReconcileProjectionForInventory,
) -> RetireOntReconcileProjectionOutcome:
    """Retire current configuration/reconcile state without deleting history.

    Flush-only participant. The canonical inventory transaction calls it only
    after OLT/ACS cleanup has succeeded; a failed cleanup therefore preserves
    the current fault and observation.
    """

    reason = command.reason.strip()
    if not reason:
        raise ValueError("Inventory reconcile retirement requires a reason")
    actor = command.actor.strip()
    if not actor:
        raise ValueError("Inventory reconcile retirement requires an actor")
    ont = db.scalar(
        select(OntUnit)
        .where(OntUnit.id == command.ont_unit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ont is None:
        raise ValueError("ONT not found for reconcile projection retirement")

    assignment_ids = tuple(dict.fromkeys(command.assignment_ids))
    heads = (
        list(
            db.scalars(
                select(OntServiceConfigurationHead)
                .where(OntServiceConfigurationHead.ont_unit_id == ont.id)
                .where(OntServiceConfigurationHead.assignment_id.in_(assignment_ids))
                .with_for_update()
            )
        )
        if assignment_ids
        else []
    )
    now = datetime.now(UTC)
    retired_ids: list[UUID] = []
    changed = False
    for head in heads:
        retired_ids.append(head.id)
        if head.phase is not OntServiceConfigurationPhase.retired:
            head.phase = OntServiceConfigurationPhase.retired
            head.waiting_reason = None
            head.failure_code = None
            head.failure_message = None
            head.retired_at = now
            revision = db.scalar(
                select(OntServiceConfigurationRevision)
                .where(
                    OntServiceConfigurationRevision.head_id == head.id,
                    OntServiceConfigurationRevision.revision == head.current_revision,
                )
                .with_for_update()
            )
            if revision is not None:
                revision.phase = OntServiceConfigurationPhase.retired
                revision.waiting_reason = reason
            changed = True

    binding_was_present = (
        any(
            value is not None
            for value in (
                ont.reconcile_configuration_head_id,
                ont.reconcile_assignment_id,
                ont.reconcile_desired_revision,
                ont.reconcile_operation_id,
                ont.last_error,
            )
        )
        or ont.sync_status is not OntSyncStatus.synced
    )
    ont.sync_status = OntSyncStatus.synced
    ont.last_error = None
    ont.last_reconciled_at = None
    ont.last_reconcile_started_at = None
    ont.reconcile_configuration_head_id = None
    ont.reconcile_assignment_id = None
    ont.reconcile_desired_revision = None
    ont.reconcile_operation_id = None

    observation = db.scalar(
        select(OntObservation)
        .where(OntObservation.ont_unit_id == ont.id)
        .with_for_update()
    )
    observation_invalidated = observation is not None
    if observation is not None:
        db.delete(observation)
    if changed or binding_was_present or observation_invalidated:
        emit_event(
            db,
            EventType.ont_service_configuration_retired,
            {
                "ont_unit_id": str(ont.id),
                "configuration_head_ids": [str(value) for value in retired_ids],
                "reason": reason,
            },
            actor=actor,
        )
    db.flush()
    return RetireOntReconcileProjectionOutcome(
        ont_unit_id=ont.id,
        retired_head_ids=tuple(retired_ids),
        observation_invalidated=observation_invalidated,
        replayed=not (changed or binding_was_present or observation_invalidated),
    )


__all__ = (
    "ReconcileLifecycleBinding",
    "RetireOntReconcileProjectionForInventory",
    "RetireOntReconcileProjectionOutcome",
    "bind_reconcile_projection",
    "reconcile_binding_matches",
    "retire_ont_reconcile_projection_for_inventory",
)
