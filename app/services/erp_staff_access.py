from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.erp_staff_access import (
    ErpStaffAccountStatusProjection,
    ErpStaffLeaveRestriction,
)
from app.models.system_user import SystemUser
from app.schemas.erp_staff_access_webhook import (
    ErpStaffAccountStatusEvent,
    ErpStaffLeaveRestrictionEvent,
)
from app.services import auth_cache, staff_provisioning
from app.services import system_user_assignments as assignment_service
from app.services.audit_adapter import AuditActor, record_audit_event, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.integrations.backoffice_contracts import (
    ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
    ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.session_hooks import run_after_commit


class StaffLeaveRestrictionStatus(StrEnum):
    active = "active"
    cancelled = "cancelled"
    revoked = "revoked"


class StaffAccountStatus(StrEnum):
    active = "active"
    inactive = "inactive"


class ErpStaffAccessError(DomainError):
    """Stable error contract for ERP staff-access synchronization."""


@dataclass(frozen=True)
class ApplyLeaveRestrictionCommand:
    context: CommandContext
    event: ErpStaffLeaveRestrictionEvent
    delivery_id: str | None = None


@dataclass(frozen=True)
class ApplyAccountStatusCommand:
    context: CommandContext
    event: ErpStaffAccountStatusEvent
    delivery_id: str | None = None


@dataclass(frozen=True)
class ReconcileStaffAccessSnapshotCommand:
    context: CommandContext
    leave_restrictions: tuple[ErpStaffLeaveRestrictionEvent, ...] = ()
    account_statuses: tuple[ErpStaffAccountStatusEvent, ...] = ()


@dataclass(frozen=True)
class StaffAccessApplyOutcome:
    event_id: str
    system_user_id: UUID
    version: int
    applied: bool
    status: str
    reason: str


@dataclass(frozen=True)
class StaffAccessReconcileOutcome:
    leave_restrictions_seen: int
    account_statuses_seen: int
    applied: int
    ignored: int
    command_id: UUID
    correlation_id: UUID


_LEAVE_COMMAND = OwnerCommandDefinition(
    owner="auth.erp_staff_access",
    concern="ERP staff leave restriction projection",
    name="apply_staff_leave_restriction_event",
)
_ACCOUNT_STATUS_COMMAND = OwnerCommandDefinition(
    owner="auth.erp_staff_access",
    concern="ERP staff account-status projection",
    name="apply_staff_account_status_event",
)
_RECONCILE_COMMAND = OwnerCommandDefinition(
    owner="auth.erp_staff_access",
    concern="ERP staff access reconciliation",
    name="reconcile_staff_access_snapshot",
)


def _error(code: str, message: str, **details: object) -> ErpStaffAccessError:
    return ErpStaffAccessError(
        code=f"auth.erp_staff_access.{code}",
        message=message,
        details=details,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return _as_utc(left) == _as_utc(right)


def _validate_context(context: CommandContext) -> tuple[AuditActorType, str]:
    if context.scope not in {
        ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY,
        ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
    }:
        raise _error(
            "invalid_command",
            "ERP staff access command scope is not valid.",
            field="scope",
        )
    actor_type_value, separator, actor_id = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "ERP staff access actor type is not supported.",
            field="actor",
        ) from exc
    if not separator or not actor_id.strip():
        raise _error(
            "invalid_command",
            "ERP staff access actor identity is incomplete.",
            field="actor",
        )
    return actor_type, actor_id.strip()


def _actor_metadata(context: CommandContext) -> dict[str, object]:
    return {
        "schema_version": 1,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "causation_id": str(context.causation_id) if context.causation_id else None,
        "idempotency_key_sha256": (
            hashlib.sha256(context.idempotency_key.encode()).hexdigest()
            if context.idempotency_key
            else None
        ),
        "scope": context.scope,
        "reason": context.reason,
    }


def _stage_audit(
    db: Session,
    *,
    action: str,
    user_id: UUID,
    context: CommandContext,
    actor_type: AuditActorType,
    actor_id: str,
    metadata: dict[str, object],
    status_code: int = 200,
    is_success: bool = True,
) -> None:
    stage_audit_event(
        db,
        action=action,
        entity_type="system_user",
        entity_id=str(user_id),
        actor=AuditActor(actor_type=actor_type, actor_id=actor_id),
        request_id=str(context.correlation_id),
        status_code=status_code,
        is_success=is_success,
        metadata={**_actor_metadata(context), **metadata},
    )


def _lock_system_user(db: Session, user_id: UUID) -> SystemUser:
    user = db.execute(
        select(SystemUser).where(SystemUser.id == user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        raise _error(
            "mapping_not_found",
            "ERP staff event references a Selfcare staff account that does not exist.",
            system_user_id=str(user_id),
        )
    return user


def _leave_projection(
    db: Session, *, source_system: str, restriction_id: str
) -> ErpStaffLeaveRestriction | None:
    return db.execute(
        select(ErpStaffLeaveRestriction)
        .where(ErpStaffLeaveRestriction.source_system == source_system)
        .where(ErpStaffLeaveRestriction.restriction_id == restriction_id)
        .with_for_update()
    ).scalar_one_or_none()


def _account_projection(
    db: Session, *, source_system: str, erp_employee_id: str
) -> ErpStaffAccountStatusProjection | None:
    return db.execute(
        select(ErpStaffAccountStatusProjection)
        .where(ErpStaffAccountStatusProjection.source_system == source_system)
        .where(ErpStaffAccountStatusProjection.erp_employee_id == erp_employee_id)
        .with_for_update()
    ).scalar_one_or_none()


def _ensure_same_mapping(
    *,
    projected_system_user_id: UUID,
    event_system_user_id: UUID,
    erp_employee_id: str,
) -> None:
    if projected_system_user_id == event_system_user_id:
        return
    raise _error(
        "mapping_conflict",
        "ERP staff event changed the Selfcare account mapping for an employee.",
        erp_employee_id_sha256=hashlib.sha256(erp_employee_id.encode()).hexdigest(),
        projected_system_user_id=str(projected_system_user_id),
        event_system_user_id=str(event_system_user_id),
    )


def _apply_leave_event_in_transaction(
    db: Session,
    *,
    command: ApplyLeaveRestrictionCommand,
    actor_type: AuditActorType,
    actor_id: str,
) -> StaffAccessApplyOutcome:
    event = command.event
    source_system = event.source_system.strip()
    restriction_id = event.restriction_id.strip()
    erp_employee_id = event.erp_employee_id.strip()
    now = datetime.now(UTC)
    projection = _leave_projection(
        db,
        source_system=source_system,
        restriction_id=restriction_id,
    )
    if projection is not None:
        if event.version < projection.version:
            _stage_audit(
                db,
                action="auth.erp_staff_leave_restriction_stale_ignored",
                user_id=projection.system_user_id,
                context=command.context,
                actor_type=actor_type,
                actor_id=actor_id,
                metadata={
                    "restriction_id": restriction_id,
                    "event_version": event.version,
                    "current_version": projection.version,
                },
                status_code=202,
            )
            return StaffAccessApplyOutcome(
                event_id=event.event_id,
                system_user_id=projection.system_user_id,
                version=projection.version,
                applied=False,
                status=projection.status,
                reason="stale",
            )
        user = _lock_system_user(db, event.system_user_id)
        _ensure_same_mapping(
            projected_system_user_id=projection.system_user_id,
            event_system_user_id=user.id,
            erp_employee_id=erp_employee_id,
        )
        same_state = (
            event.version == projection.version
            and projection.status == event.status
            and _same_instant(projection.effective_from, event.effective_from)
            and _same_instant(projection.effective_until, event.effective_until)
        )
        if same_state:
            _stage_audit(
                db,
                action="auth.erp_staff_leave_restriction_duplicate",
                user_id=user.id,
                context=command.context,
                actor_type=actor_type,
                actor_id=actor_id,
                metadata={
                    "restriction_id": restriction_id,
                    "event_version": event.version,
                },
                status_code=202,
            )
            return StaffAccessApplyOutcome(
                event_id=event.event_id,
                system_user_id=user.id,
                version=projection.version,
                applied=False,
                status=projection.status,
                reason="duplicate",
            )
        if event.version == projection.version:
            raise _error(
                "version_conflict",
                "ERP staff leave event reused a version with different state.",
                restriction_id=restriction_id,
                version=event.version,
            )
    else:
        user = _lock_system_user(db, event.system_user_id)
        projection = ErpStaffLeaveRestriction(
            source_system=source_system,
            restriction_id=restriction_id,
            erp_employee_id=erp_employee_id,
            system_user_id=user.id,
            effective_from=_as_utc(event.effective_from),
            effective_until=(
                _as_utc(event.effective_until) if event.effective_until else None
            ),
            status=event.status,
            version=event.version,
            source_updated_at=_as_utc(event.updated_at),
            last_event_id=event.event_id,
            last_delivery_id=command.delivery_id,
            last_observed_at=now,
        )
        db.add(projection)

    old_status = projection.status
    projection.erp_employee_id = erp_employee_id
    projection.system_user_id = user.id
    projection.effective_from = _as_utc(event.effective_from)
    projection.effective_until = (
        _as_utc(event.effective_until) if event.effective_until else None
    )
    projection.status = event.status
    projection.version = event.version
    projection.source_updated_at = _as_utc(event.updated_at)
    projection.last_event_id = event.event_id
    projection.last_delivery_id = command.delivery_id
    projection.last_observed_at = now
    action = (
        "auth.erp_staff_leave_restriction_cancelled"
        if event.status in {"cancelled", "revoked"}
        else (
            "auth.erp_staff_leave_restriction_received"
            if old_status == event.status
            else "auth.erp_staff_leave_restriction_updated"
        )
    )
    _stage_audit(
        db,
        action=action,
        user_id=user.id,
        context=command.context,
        actor_type=actor_type,
        actor_id=actor_id,
        metadata={
            "restriction_id": restriction_id,
            "status": event.status,
            "version": event.version,
        },
    )
    return StaffAccessApplyOutcome(
        event_id=event.event_id,
        system_user_id=user.id,
        version=event.version,
        applied=True,
        status=event.status,
        reason="applied",
    )


def _reactivation_allowed(
    user: SystemUser,
    projection: ErpStaffAccountStatusProjection,
) -> bool:
    applied_at = projection.erp_inactive_applied_at
    updated_at = user.updated_at
    if not projection.erp_inactive_applied or user.is_active or applied_at is None:
        return False
    if updated_at is None:
        return True
    return _as_utc(updated_at) <= _as_utc(applied_at)


def _apply_account_status_in_transaction(
    db: Session,
    *,
    command: ApplyAccountStatusCommand,
    actor_type: AuditActorType,
    actor_id: str,
) -> StaffAccessApplyOutcome:
    event = command.event
    source_system = event.source_system.strip()
    erp_employee_id = event.erp_employee_id.strip()
    now = datetime.now(UTC)
    projection = _account_projection(
        db,
        source_system=source_system,
        erp_employee_id=erp_employee_id,
    )
    if projection is not None:
        if event.version < projection.version:
            _stage_audit(
                db,
                action="auth.erp_staff_account_status_stale_ignored",
                user_id=projection.system_user_id,
                context=command.context,
                actor_type=actor_type,
                actor_id=actor_id,
                metadata={
                    "event_version": event.version,
                    "current_version": projection.version,
                },
                status_code=202,
            )
            return StaffAccessApplyOutcome(
                event_id=event.event_id,
                system_user_id=projection.system_user_id,
                version=projection.version,
                applied=False,
                status=projection.desired_status,
                reason="stale",
            )
        user = _lock_system_user(db, event.system_user_id)
        _ensure_same_mapping(
            projected_system_user_id=projection.system_user_id,
            event_system_user_id=user.id,
            erp_employee_id=erp_employee_id,
        )
        if (
            event.version == projection.version
            and projection.desired_status == event.account_status
        ):
            inactive_drift = (
                event.account_status == StaffAccountStatus.inactive.value
                and user.is_active
            )
            if not inactive_drift:
                _stage_audit(
                    db,
                    action="auth.erp_staff_account_status_duplicate",
                    user_id=user.id,
                    context=command.context,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    metadata={"event_version": event.version},
                    status_code=202,
                )
                return StaffAccessApplyOutcome(
                    event_id=event.event_id,
                    system_user_id=user.id,
                    version=projection.version,
                    applied=False,
                    status=projection.desired_status,
                    reason="duplicate",
                )
        if (
            event.version == projection.version
            and projection.desired_status != event.account_status
        ):
            raise _error(
                "version_conflict",
                "ERP staff account-status event reused a version with different state.",
                version=event.version,
            )
    else:
        user = _lock_system_user(db, event.system_user_id)
        projection = ErpStaffAccountStatusProjection(
            source_system=source_system,
            erp_employee_id=erp_employee_id,
            system_user_id=user.id,
            desired_status=event.account_status,
            version=event.version,
            source_updated_at=_as_utc(event.updated_at),
            reason=event.reason,
            last_event_id=event.event_id,
            last_delivery_id=command.delivery_id,
            erp_inactive_applied=False,
            erp_inactive_applied_at=None,
            last_observed_at=now,
        )
        db.add(projection)

    changed = False
    credential_changes = 0
    credential_created = False
    credential_reconciled = False
    revoked_sessions = 0
    reactivated = False
    if event.account_status == StaffAccountStatus.inactive.value:
        projection.erp_inactive_applied = bool(user.is_active)
        if user.is_active:
            assignment_service.ensure_can_deactivate_system_user(db, user.id)
            user.is_active = False
            credential_changes, revoked_sessions = (
                staff_provisioning.close_principal_access(db, user.id)
            )
            db.flush()
            projection.erp_inactive_applied_at = _as_utc(user.updated_at or now)
            changed = True
    elif event.account_status == StaffAccountStatus.active.value:
        if _reactivation_allowed(user, projection):
            credential_changes, credential_created, credential_reconciled = (
                staff_provisioning.reconcile_active_local_login_credential(
                    db,
                    user_id=user.id,
                    email=user.email,
                )
            )
            user.is_active = True
            reactivated = True
            changed = True
        projection.erp_inactive_applied = False
        projection.erp_inactive_applied_at = None

    projection.system_user_id = user.id
    projection.desired_status = event.account_status
    projection.version = event.version
    projection.source_updated_at = _as_utc(event.updated_at)
    projection.reason = event.reason
    projection.last_event_id = event.event_id
    projection.last_delivery_id = command.delivery_id
    projection.last_observed_at = now
    _invalidate_auth_after_commit(db, user.id)
    action = (
        "auth.erp_staff_account_inactive"
        if event.account_status == StaffAccountStatus.inactive.value
        else "auth.erp_staff_account_reactivated"
    )
    _stage_audit(
        db,
        action=action,
        user_id=user.id,
        context=command.context,
        actor_type=actor_type,
        actor_id=actor_id,
        metadata={
            "account_status": event.account_status,
            "version": event.version,
            "changed": changed,
            "reactivated": reactivated,
            "credential_changes": int(credential_changes or 0),
            "credential_created": credential_created,
            "credential_reconciled": credential_reconciled,
            "revoked_sessions": int(revoked_sessions or 0),
        },
    )
    return StaffAccessApplyOutcome(
        event_id=event.event_id,
        system_user_id=user.id,
        version=event.version,
        applied=True,
        status=event.account_status,
        reason="applied",
    )


def _invalidate_auth_after_commit(db: Session, user_id: UUID) -> None:
    def invalidate(_callback_db: Session) -> None:
        auth_cache.invalidate_principal("system_user", str(user_id))

    run_after_commit(db, invalidate)


def apply_staff_leave_restriction_event(
    db: Session,
    command: ApplyLeaveRestrictionCommand,
) -> StaffAccessApplyOutcome:
    def operation() -> StaffAccessApplyOutcome:
        actor_type, actor_id = _validate_context(command.context)
        return _apply_leave_event_in_transaction(
            db, command=command, actor_type=actor_type, actor_id=actor_id
        )

    return execute_owner_command(
        db,
        definition=_LEAVE_COMMAND,
        context=command.context,
        operation=operation,
    )


def apply_staff_account_status_event(
    db: Session,
    command: ApplyAccountStatusCommand,
) -> StaffAccessApplyOutcome:
    def operation() -> StaffAccessApplyOutcome:
        actor_type, actor_id = _validate_context(command.context)
        return _apply_account_status_in_transaction(
            db, command=command, actor_type=actor_type, actor_id=actor_id
        )

    return execute_owner_command(
        db,
        definition=_ACCOUNT_STATUS_COMMAND,
        context=command.context,
        operation=operation,
    )


def reconcile_staff_access_snapshot(
    db: Session,
    command: ReconcileStaffAccessSnapshotCommand,
) -> StaffAccessReconcileOutcome:
    def operation() -> StaffAccessReconcileOutcome:
        actor_type, actor_id = _validate_context(command.context)
        applied = 0
        ignored = 0
        for event in command.leave_restrictions:
            outcome = _apply_leave_event_in_transaction(
                db,
                command=ApplyLeaveRestrictionCommand(
                    context=command.context,
                    event=event,
                    delivery_id=None,
                ),
                actor_type=actor_type,
                actor_id=actor_id,
            )
            applied += int(outcome.applied)
            ignored += int(not outcome.applied)
        for account_event in command.account_statuses:
            outcome = _apply_account_status_in_transaction(
                db,
                command=ApplyAccountStatusCommand(
                    context=command.context,
                    event=account_event,
                    delivery_id=None,
                ),
                actor_type=actor_type,
                actor_id=actor_id,
            )
            applied += int(outcome.applied)
            ignored += int(not outcome.applied)
        return StaffAccessReconcileOutcome(
            leave_restrictions_seen=len(command.leave_restrictions),
            account_statuses_seen=len(command.account_statuses),
            applied=applied,
            ignored=ignored,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    return execute_owner_command(
        db,
        definition=_RECONCILE_COMMAND,
        context=command.context,
        operation=operation,
    )


def active_leave_restriction(
    db: Session,
    *,
    system_user_id: UUID,
    at: datetime | None = None,
) -> ErpStaffLeaveRestriction | None:
    observed_at = _as_utc(at or datetime.now(UTC))
    return db.scalars(
        select(ErpStaffLeaveRestriction)
        .where(ErpStaffLeaveRestriction.system_user_id == system_user_id)
        .where(
            ErpStaffLeaveRestriction.status == StaffLeaveRestrictionStatus.active.value
        )
        .where(ErpStaffLeaveRestriction.effective_from <= observed_at)
        .where(
            (ErpStaffLeaveRestriction.effective_until.is_(None))
            | (ErpStaffLeaveRestriction.effective_until > observed_at)
        )
        .order_by(
            ErpStaffLeaveRestriction.version.desc(),
            ErpStaffLeaveRestriction.updated_at.desc(),
        )
        .limit(1)
    ).first()


def staff_write_restricted(
    db: Session,
    auth: dict[str, object],
    *,
    method: str,
    at: datetime | None = None,
) -> ErpStaffLeaveRestriction | None:
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return None
    if auth.get("principal_type") != "system_user":
        return None
    principal_id = str(auth.get("principal_id") or "")
    try:
        system_user_id = UUID(principal_id)
    except ValueError:
        return None
    return active_leave_restriction(db, system_user_id=system_user_id, at=at)


def audit_denied_write(
    db: Session,
    *,
    auth: dict[str, object],
    restriction: ErpStaffLeaveRestriction,
    request_id: str | None,
    permission_key: str,
) -> None:
    principal_id = str(auth.get("principal_id") or "")
    stage_audit_event(
        db,
        action="auth.erp_staff_leave_write_denied",
        entity_type="system_user",
        entity_id=principal_id,
        actor=AuditActor.user(principal_id),
        request_id=request_id,
        status_code=403,
        is_success=False,
        metadata={
            "restriction_id": restriction.restriction_id,
            "permission_key": permission_key,
            "source_system": restriction.source_system,
        },
    )


def record_denied_write(
    db: Session,
    *,
    auth: dict[str, object],
    restriction: ErpStaffLeaveRestriction,
    request_id: str | None,
    permission_key: str,
) -> None:
    audit_denied_write(
        db,
        auth=auth,
        restriction=restriction,
        request_id=request_id,
        permission_key=permission_key,
    )
    db.commit()


def record_staff_access_webhook_rejection(
    db: Session,
    *,
    action: str,
    installation_id: UUID,
    capability_binding_id: UUID,
    status_code: int,
    metadata: dict[str, object] | None = None,
) -> None:
    record_audit_event(
        db,
        action=action,
        entity_type="integration_capability_binding",
        entity_id=str(capability_binding_id),
        actor=AuditActor.service(str(installation_id)),
        status_code=status_code,
        is_success=False,
        metadata=metadata or {},
    )
