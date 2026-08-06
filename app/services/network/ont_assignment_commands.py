"""Canonical commands for normal, explicit ONT service assignments.

This owner is deliberately narrower than the reviewed identity-repair owner.
It accepts only an exact ONT, subscription, and modeled PON selected by an
authorized workflow.  It never infers a customer from a name, address, MAC,
work order, imported registration, or map geometry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.audit_adapter import stage_audit_event
from app.services.network._common import SubscriberValidator


class OntAssignmentCommandError(ValueError):
    """Raised when a normal assignment command is invalid or conflicts."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class OntAssignmentCommandResult:
    assignment: OntAssignment
    ont_unit_id: uuid.UUID
    subscription_id: uuid.UUID
    subscriber_id: uuid.UUID
    pon_port_id: uuid.UUID
    olt_id: uuid.UUID
    action: str
    replayed: bool = False


@dataclass(frozen=True)
class OntAssignmentReleaseResult:
    assignment: OntAssignment
    ont_unit_id: uuid.UUID
    assignment_id: uuid.UUID
    released_at: datetime
    replayed: bool = False


def _uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise OntAssignmentCommandError(f"{field} must be a UUID") from exc


def _actor_type(actor_id: str | None) -> AuditActorType:
    return AuditActorType.user if actor_id else AuditActorType.service


def _fsp_parts(
    name: str | None, *, vendor: str | None
) -> tuple[str | None, str | None]:
    """Split a PON name into ``(board, port)`` for the platform, or ``(None, None)``.

    Splitting on ``/`` alone accepted ``pon-0/1/13`` -- three parts -- and
    returned board ``pon-0/1``, writing a board that names nothing onto the ONT
    instead of failing closed. Callers are already refused upstream by
    ``pon_port_identity.assert_assignable``; this reads the name through the
    identity owner so the helper cannot reintroduce the corruption on its own.

    The vendor is required because the shapes differ. On a chassis OLT the
    board is ``frame/slot``. On a single-box OLT there is no board at all, and
    reading its ``pon<n>`` name as a chassis name yielded ``(None, None)`` --
    silently discarding the port number on every UF-OLT assignment and move.
    """
    from app.services.network.pon_port_identity import (
        SingleBoxPonIdentity,
        read_name,
        shape_for_vendor,
    )

    identity = read_name(name, shape=shape_for_vendor(vendor)).identity
    if identity is None:
        return None, None
    if isinstance(identity, SingleBoxPonIdentity):
        # A single-box OLT has no board to record. Reporting ``None`` states
        # that honestly; inventing one would put a board that names nothing
        # back onto the ONT, which is the defect this helper exists to stop.
        return None, str(identity.port)
    return f"{identity.frame}/{identity.slot}", str(identity.port)


class OntAssignmentCommands:
    """Own normal explicit assignment, release, and physical PON moves."""

    def __init__(self, subscriber_validator: SubscriberValidator | None) -> None:
        self._subscriber_validator = subscriber_validator

    def _resolve_customer_identity(
        self,
        db: Session,
        *,
        subscription_id: uuid.UUID,
        subscriber_id: object | None,
        service_address_id: object | None,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        validator = self._subscriber_validator
        if validator is None:
            raise OntAssignmentCommandError(
                "Subscription validation is unavailable",
                status_code=503,
            )
        try:
            resolved_subscription, resolved_subscriber = (
                validator.resolve_assignment_subscription(
                    db,
                    subscription_id=subscription_id,
                    subscriber_id=subscriber_id,
                )
            )
            validator.validate_assignment_customer_links(
                db,
                subscriber_id=resolved_subscriber,
                service_address_id=service_address_id,
            )
        except HTTPException as exc:
            raise OntAssignmentCommandError(
                str(exc.detail), status_code=exc.status_code
            ) from exc
        return (
            _uuid(resolved_subscription, "subscription_id"),
            _uuid(resolved_subscriber, "subscriber_id"),
        )

    @staticmethod
    def _load_target(
        db: Session,
        *,
        ont_unit_id: uuid.UUID,
        pon_port_id: uuid.UUID,
    ) -> tuple[OntUnit, PonPort, OLTDevice]:
        ont = db.scalar(
            select(OntUnit).where(OntUnit.id == ont_unit_id).with_for_update()
        )
        if ont is None:
            raise OntAssignmentCommandError("ONT unit not found", status_code=404)
        from app.services.network.ont_commissioning import (
            assignment_is_blocked_by_commissioning,
        )

        commissioning = assignment_is_blocked_by_commissioning(
            db,
            ont_unit_id=ont.id,
        )
        if commissioning is not None:
            raise OntAssignmentCommandError(
                "ONT assignment is temporarily blocked while commissioning is "
                f"{commissioning.state.value}. Wait for management-ready state "
                "or resolve commissioning cleanup first.",
                status_code=409,
            )
        pon = db.scalar(
            select(PonPort).where(PonPort.id == pon_port_id).with_for_update()
        )
        if pon is None or not pon.is_active:
            raise OntAssignmentCommandError(
                "Active PON port not found", status_code=404
            )
        # Identity before decision. A prefixed, malformed, or ambiguous PON row
        # cannot be stated structurally, so an assignment recorded against it
        # points at something nobody can name -- and the reference outlives the
        # row. Refused here, under the same lock as the rest of the target
        # load, so the check cannot race a concurrent rename.
        from app.services.network.pon_port_identity import (
            PonPortIdentityError,
            assert_assignable,
        )

        try:
            assert_assignable(db, pon)
        except PonPortIdentityError as exc:
            raise OntAssignmentCommandError(
                f"{exc} Repair it through network.pon_port_identity first.",
                status_code=409,
            ) from exc
        olt = db.scalar(
            select(OLTDevice).where(OLTDevice.id == pon.olt_id).with_for_update()
        )
        if olt is None or not olt.is_active:
            raise OntAssignmentCommandError("Active OLT not found", status_code=404)
        if ont.olt_device_id is not None and ont.olt_device_id != olt.id:
            raise OntAssignmentCommandError(
                "ONT OLT identity conflicts with the selected PON; use reviewed identity repair",
                status_code=409,
            )
        if ont.pon_port_id is not None and ont.pon_port_id != pon.id:
            raise OntAssignmentCommandError(
                "ONT PON identity conflicts with the selected PON; use reviewed identity repair",
                status_code=409,
            )
        return ont, pon, olt

    @staticmethod
    def _check_capacity(db: Session, pon: PonPort, *, creating: bool) -> None:
        if not creating or pon.max_ont_capacity is None:
            return
        assigned = int(
            db.scalar(
                select(func.count(OntAssignment.id)).where(
                    OntAssignment.pon_port_id == pon.id,
                    OntAssignment.active.is_(True),
                )
            )
            or 0
        )
        if assigned >= pon.max_ont_capacity:
            raise OntAssignmentCommandError(
                "PON port ONT capacity has been reached", status_code=409
            )

    def assign(
        self,
        db: Session,
        *,
        ont_unit_id: object,
        subscription_id: object,
        pon_port_id: object,
        subscriber_id: object | None = None,
        service_address_id: object | None = None,
        work_order_mirror_id: object | None = None,
        notes: str | None = None,
        actor_id: str | None = None,
        source: str = "explicit_provisioning",
        commit: bool = True,
    ) -> OntAssignmentCommandResult:
        """Create or claim one exact normal service assignment.

        Existing customer-bound disagreements are never overwritten here. They
        belong to ``network.ont_assignment_identity`` and its independent review.
        """

        ont_id = _uuid(ont_unit_id, "ont_unit_id")
        sub_id = _uuid(subscription_id, "subscription_id")
        pon_id = _uuid(pon_port_id, "pon_port_id")
        customer_id = (
            _uuid(subscriber_id, "subscriber_id") if subscriber_id is not None else None
        )
        address_id = (
            _uuid(service_address_id, "service_address_id")
            if service_address_id is not None
            else None
        )
        work_order_id = (
            _uuid(work_order_mirror_id, "work_order_mirror_id")
            if work_order_mirror_id is not None
            else None
        )
        sub_id, customer_id = self._resolve_customer_identity(
            db,
            subscription_id=sub_id,
            subscriber_id=customer_id,
            service_address_id=address_id,
        )
        ont, pon, olt = self._load_target(db, ont_unit_id=ont_id, pon_port_id=pon_id)

        active_rows = list(
            db.scalars(
                select(OntAssignment)
                .where(
                    OntAssignment.active.is_(True),
                    or_(
                        OntAssignment.ont_unit_id == ont.id,
                        OntAssignment.subscription_id == sub_id,
                    ),
                )
                .order_by(OntAssignment.id)
                .with_for_update()
            )
        )
        ont_assignment = next(
            (row for row in active_rows if row.ont_unit_id == ont.id), None
        )
        subscription_conflict = next(
            (
                row
                for row in active_rows
                if row.subscription_id == sub_id and row.ont_unit_id != ont.id
            ),
            None,
        )
        if subscription_conflict is not None:
            raise OntAssignmentCommandError(
                "Subscription already has an active ONT assignment; release it explicitly or use reviewed repair",
                status_code=409,
            )

        replayed = False
        action = "created"
        if ont_assignment is not None:
            exact = (
                ont_assignment.subscription_id == sub_id
                and ont_assignment.subscriber_id == customer_id
                and ont_assignment.pon_port_id == pon.id
                and ont_assignment.service_address_id == address_id
            )
            if exact:
                assignment = ont_assignment
                replayed = True
                action = "replayed"
            elif (
                ont_assignment.subscription_id is None
                and ont_assignment.subscriber_id is None
                and ont_assignment.pon_port_id in (None, pon.id)
            ):
                assignment = ont_assignment
                assignment.subscription_id = sub_id
                assignment.subscriber_id = customer_id
                assignment.pon_port_id = pon.id
                assignment.service_address_id = address_id
                assignment.work_order_mirror_id = work_order_id
                assignment.assigned_at = assignment.assigned_at or datetime.now(UTC)
                assignment.released_at = None
                assignment.release_reason = None
                assignment.notes = (notes or "").strip() or None
                action = "claimed_legacy_placeholder"
            else:
                raise OntAssignmentCommandError(
                    "ONT already has a conflicting active assignment; use reviewed identity repair",
                    status_code=409,
                )
        else:
            self._check_capacity(db, pon, creating=True)
            assignment = OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=pon.id,
                subscriber_id=customer_id,
                subscription_id=sub_id,
                service_address_id=address_id,
                work_order_mirror_id=work_order_id,
                assigned_at=datetime.now(UTC),
                active=True,
                notes=(notes or "").strip() or None,
            )
            db.add(assignment)

        ont.olt_device_id = olt.id
        ont.pon_port_id = pon.id
        board, port = _fsp_parts(pon.name, vendor=olt.vendor)
        if ont.board is None and board is not None:
            ont.board = board
        if ont.port is None and port is not None:
            ont.port = port
        ont.is_active = True
        db.flush()

        if not replayed:
            validator = self._subscriber_validator
            assert validator is not None
            validator.apply_subscription_device_intent(
                db, subscription_id=sub_id, ont=ont
            )
            from app.services.network.cpe import ensure_cpe_for_ont

            ensure_cpe_for_ont(db, ont, assignment, commit=False)

        exact_result = {
            "action": action,
            "assignment_id": str(assignment.id),
            "olt_id": str(olt.id),
            "ont_unit_id": str(ont.id),
            "pon_port_id": str(pon.id),
            "replayed": replayed,
            "subscriber_id": str(customer_id),
            "subscription_id": str(sub_id),
        }
        stage_audit_event(
            db,
            action="network.ont_assignment.assign",
            entity_type="ont_assignment",
            entity_id=str(assignment.id),
            actor_type=_actor_type(actor_id),
            actor_id=actor_id,
            metadata={"source": source, "exact_result": exact_result},
        )
        if commit:
            db.commit()
            db.refresh(assignment)
        return OntAssignmentCommandResult(
            assignment=assignment,
            ont_unit_id=ont.id,
            subscription_id=sub_id,
            subscriber_id=customer_id,
            pon_port_id=pon.id,
            olt_id=olt.id,
            action=action,
            replayed=replayed,
        )

    def release(
        self,
        db: Session,
        *,
        assignment_id: object,
        reason: str,
        actor_id: str | None = None,
        source: str = "explicit_deprovisioning",
        commit: bool = True,
    ) -> OntAssignmentReleaseResult:
        """Close one exact normal assignment without deleting its history."""

        normalized_id = _uuid(assignment_id, "assignment_id")
        assignment = db.scalar(
            select(OntAssignment)
            .where(OntAssignment.id == normalized_id)
            .with_for_update()
        )
        if assignment is None:
            raise OntAssignmentCommandError("ONT assignment not found", status_code=404)
        ont = db.scalar(
            select(OntUnit)
            .where(OntUnit.id == assignment.ont_unit_id)
            .with_for_update()
        )
        if ont is None:
            raise OntAssignmentCommandError("ONT unit not found", status_code=404)
        released_at = assignment.released_at or datetime.now(UTC)
        replayed = not assignment.active
        if assignment.active:
            assignment.active = False
            assignment.released_at = released_at
            assignment.release_reason = str(reason or "").strip()[:64] or "released"
            other_active = db.scalars(
                select(OntAssignment.id).where(
                    OntAssignment.ont_unit_id == ont.id,
                    OntAssignment.active.is_(True),
                    OntAssignment.id != assignment.id,
                )
            ).first()
            ont.is_active = other_active is not None
            db.flush()
            from app.services.network.cpe import ensure_cpe_for_ont

            ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)

        stage_audit_event(
            db,
            action="network.ont_assignment.release",
            entity_type="ont_assignment",
            entity_id=str(assignment.id),
            actor_type=_actor_type(actor_id),
            actor_id=actor_id,
            metadata={
                "source": source,
                "exact_result": {
                    "active": False,
                    "assignment_id": str(assignment.id),
                    "ont_unit_id": str(ont.id),
                    "reason": assignment.release_reason,
                    "released_at": released_at.isoformat(),
                    "replayed": replayed,
                },
            },
        )
        if commit:
            db.commit()
            db.refresh(assignment)
        return OntAssignmentReleaseResult(
            assignment=assignment,
            ont_unit_id=ont.id,
            assignment_id=assignment.id,
            released_at=released_at,
            replayed=replayed,
        )

    def move_to_pon(
        self,
        db: Session,
        *,
        ont_unit_id: object,
        target_pon_port_id: object,
        actor_id: str | None = None,
        source: str = "ont_device_move",
        commit: bool = True,
    ) -> OntAssignmentCommandResult:
        """Project an already-executed physical move onto exact local identity."""

        ont_id = _uuid(ont_unit_id, "ont_unit_id")
        pon_id = _uuid(target_pon_port_id, "target_pon_port_id")
        ont = db.scalar(select(OntUnit).where(OntUnit.id == ont_id).with_for_update())
        if ont is None:
            raise OntAssignmentCommandError("ONT unit not found", status_code=404)
        assignment = db.scalars(
            select(OntAssignment)
            .where(
                OntAssignment.ont_unit_id == ont.id,
                OntAssignment.active.is_(True),
            )
            .with_for_update()
        ).first()
        if (
            assignment is None
            or assignment.subscription_id is None
            or assignment.subscriber_id is None
        ):
            raise OntAssignmentCommandError(
                "ONT move requires an exact active subscription assignment; repair legacy identity first",
                status_code=409,
            )
        pon = db.scalar(select(PonPort).where(PonPort.id == pon_id).with_for_update())
        if pon is None or not pon.is_active:
            raise OntAssignmentCommandError(
                "Active target PON not found", status_code=404
            )
        # Same refusal as ``_load_target``. A physical move loads its target PON
        # directly rather than through that helper, so without this the guard
        # covered ``assign`` but not ``move_to_pon`` -- and the move writes
        # ``ont.board``/``ont.port`` from the row's name, which is exactly how a
        # non-canonical name becomes corrupt ONT inventory.
        from app.services.network.pon_port_identity import (
            PonPortIdentityError,
            assert_assignable,
        )

        try:
            assert_assignable(db, pon)
        except PonPortIdentityError as exc:
            raise OntAssignmentCommandError(
                f"{exc} Repair it through network.pon_port_identity first.",
                status_code=409,
            ) from exc
        olt = db.scalar(
            select(OLTDevice).where(OLTDevice.id == pon.olt_id).with_for_update()
        )
        if olt is None or not olt.is_active:
            raise OntAssignmentCommandError(
                "Active target OLT not found", status_code=404
            )
        if ont.olt_device_id is not None and ont.olt_device_id != olt.id:
            raise OntAssignmentCommandError(
                "Cross-OLT assignment moves are not supported", status_code=409
            )
        before_pon_id = assignment.pon_port_id
        replayed = before_pon_id == pon.id and ont.pon_port_id == pon.id
        if not replayed:
            self._check_capacity(db, pon, creating=True)
            assignment.pon_port_id = pon.id
            ont.olt_device_id = olt.id
            ont.pon_port_id = pon.id
            board, port = _fsp_parts(pon.name, vendor=olt.vendor)
            ont.board = board
            ont.port = port
            db.flush()
        stage_audit_event(
            db,
            action="network.ont_assignment.move_pon",
            entity_type="ont_assignment",
            entity_id=str(assignment.id),
            actor_type=_actor_type(actor_id),
            actor_id=actor_id,
            metadata={
                "source": source,
                "exact_result": {
                    "assignment_id": str(assignment.id),
                    "from_pon_port_id": str(before_pon_id) if before_pon_id else None,
                    "olt_id": str(olt.id),
                    "ont_unit_id": str(ont.id),
                    "replayed": replayed,
                    "to_pon_port_id": str(pon.id),
                },
            },
        )
        if commit:
            db.commit()
            db.refresh(assignment)
        return OntAssignmentCommandResult(
            assignment=assignment,
            ont_unit_id=ont.id,
            subscription_id=_uuid(assignment.subscription_id, "subscription_id"),
            subscriber_id=_uuid(assignment.subscriber_id, "subscriber_id"),
            pon_port_id=pon.id,
            olt_id=olt.id,
            action="moved" if not replayed else "replayed",
            replayed=replayed,
        )


# ---------------------------------------------------------------------------
# assigned_at backfill reconciler
#
# Active assignments must carry an assigned_at (the future constraint cutover
# requires it). Some rows are missing it while their identity is already
# canonical, so the reviewed identity `canonicalize` path rejects them
# ("already canonical") and `deactivate` is wrong. This owner-side reconciler
# backfills assigned_at from the assignment's own created_at (its true
# assignment time) with a preview and a staged audit event per change; it is
# idempotent and issues no ad-hoc SQL.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssignedAtDrift:
    assignment_id: str
    ont_unit_id: str | None
    subscription_id: str | None
    created_at: str | None


def _missing_assigned_at_query(db: Session):
    return (
        db.query(OntAssignment)
        .filter(OntAssignment.active.is_(True))
        .filter(OntAssignment.assigned_at.is_(None))
        .filter(OntAssignment.created_at.isnot(None))
        .order_by(OntAssignment.created_at)
    )


def preview_assigned_at_drift(db: Session) -> list[AssignedAtDrift]:
    """Read-only: active assignments missing ``assigned_at``."""
    return [
        AssignedAtDrift(
            assignment_id=str(a.id),
            ont_unit_id=str(a.ont_unit_id) if a.ont_unit_id else None,
            subscription_id=str(a.subscription_id) if a.subscription_id else None,
            created_at=a.created_at.isoformat() if a.created_at else None,
        )
        for a in _missing_assigned_at_query(db).all()
    ]


def reconcile_assigned_at(
    db: Session, *, actor: str = "reconcile_assigned_at", apply: bool = False
) -> dict[str, object]:
    """Backfill ``assigned_at = created_at`` for active assignments missing it.

    Preview by default. With ``apply=True`` it sets ``assigned_at`` from the
    assignment's own ``created_at`` and stages a
    ``network.ont_assignment.backfill_assigned_at`` audit event per change.
    Idempotent — rows that already have ``assigned_at`` are not returned.
    """
    rows = _missing_assigned_at_query(db).all()
    backfilled = 0
    if apply:
        for assignment in rows:
            if assignment.assigned_at is not None or assignment.created_at is None:
                continue
            assignment.assigned_at = assignment.created_at
            stage_audit_event(
                db,
                action="network.ont_assignment.backfill_assigned_at",
                entity_type="ont_assignment",
                entity_id=str(assignment.id),
                actor_type=AuditActorType.service,
                actor_id=actor,
                metadata={
                    "assigned_at": assignment.created_at.isoformat(),
                    "source": "reconcile_assigned_at",
                },
            )
            backfilled += 1
        db.commit()
    return {"applied": apply, "candidates": len(rows), "backfilled": backfilled}
