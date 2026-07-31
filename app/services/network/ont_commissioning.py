"""Owner for temporary, assignment-free ONT commissioning.

Commissioning is deliberately narrower than service authorization. It owns a
time-bounded intent for one exact OLT/F/S/P/serial and may install only the
management VLAN service-port, IPHOST, and TR-069 profile. It never creates an
``OntAssignment`` and never applies customer internet, PPPoE, WAN, LAN, or Wi-Fi
configuration.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.models.audit import AuditActorType
from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntProvisioningStatus,
    OntUnit,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.ont_autofind import OltAutofindCandidate
from app.models.ont_commissioning import (
    OntCommissioningIntent,
    OntCommissioningState,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.network.olt_config_pack_live_audit import OltDependencyAuditScope
from app.services.network.serial_utils import (
    canonical as canonical_serial,
)
from app.services.network.serial_utils import (
    normalized_serial_sql,
)
from app.services.network_operation_dispatch import (
    NetworkOperationCommand,
    stage_dispatch,
)
from app.services.network_operations import network_operations
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

DEFAULT_COMMISSIONING_TTL = timedelta(hours=24)
MAX_COMMISSIONING_TTL = timedelta(hours=72)
_VERIFY_DELAYS_SECONDS = (30, 60, 120, 240, 240)
_ACTIVE_INTENT_STATES = (
    OntCommissioningState.commissioning,
    OntCommissioningState.authorizing,
    OntCommissioningState.awaiting_acs,
    OntCommissioningState.management_ready,
    OntCommissioningState.failed,
    OntCommissioningState.cleanup_pending,
    OntCommissioningState.cleanup_running,
)
_ASSIGNMENT_BLOCKED_STATES = (
    OntCommissioningState.commissioning,
    OntCommissioningState.authorizing,
    OntCommissioningState.awaiting_acs,
    OntCommissioningState.cleanup_pending,
    OntCommissioningState.cleanup_running,
)
_OPERATION_ACTIVE_STATES = (
    NetworkOperationStatus.pending,
    NetworkOperationStatus.running,
    NetworkOperationStatus.waiting,
)

_REQUEST_COMMISSIONING = OwnerCommandDefinition(
    owner="network.ont_commissioning",
    concern="temporary ONT commissioning intent lifecycle",
    name="request_ont_commissioning",
)
_RECONCILE_COMMISSIONING = OwnerCommandDefinition(
    owner="network.ont_commissioning",
    concern="commissioning expiry and assignment reconciliation",
    name="reconcile_ont_commissioning",
)


class OntCommissioningError(DomainError):
    """Stable commissioning-domain failure."""


def _error(code: str, message: str, **details: object) -> OntCommissioningError:
    return OntCommissioningError(
        code=f"network.ont_commissioning.{code}",
        message=message,
        details=details,
    )


def _uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise _error("invalid_target", f"{field} must be a UUID.", field=field) from exc


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RequestOntCommissioning:
    """Typed admission request for one exact autofind observation."""

    context: CommandContext
    candidate_id: uuid.UUID
    expected_olt_id: uuid.UUID
    expected_fsp: str
    expected_serial: str
    reason: str
    reference: str | None = None
    ttl: timedelta = DEFAULT_COMMISSIONING_TTL


@dataclass(frozen=True)
class OntCommissioningAdmission:
    intent_id: uuid.UUID
    operation_id: uuid.UUID
    dispatch_id: uuid.UUID
    expires_at: datetime
    duplicate: bool
    message: str


@dataclass(frozen=True)
class OntCommissioningReconcileResult:
    examined: int
    assigned: int
    provisioned: int
    cleanup_staged: int
    expired_without_device_write: int


def assignment_is_blocked_by_commissioning(
    db: Session,
    *,
    ont_unit_id: object,
) -> OntCommissioningIntent | None:
    """Return the exact active intent that makes assignment unsafe right now."""

    normalized_ont_id = _uuid(ont_unit_id, "ont_unit_id")
    ont = db.get(OntUnit, normalized_ont_id)
    identity_filters = [OntCommissioningIntent.ont_unit_id == normalized_ont_id]
    if ont is not None:
        serial = canonical_serial(ont.serial_number)
        if serial:
            identity_filters.append(OntCommissioningIntent.canonical_serial == serial)
    return db.scalars(
        select(OntCommissioningIntent)
        .where(
            or_(*identity_filters),
            OntCommissioningIntent.state.in_(_ASSIGNMENT_BLOCKED_STATES),
        )
        .order_by(OntCommissioningIntent.created_at.desc())
        .limit(1)
    ).first()


def _find_candidate_ont(
    db: Session,
    candidate: OltAutofindCandidate,
) -> OntUnit | None:
    if candidate.ont_unit_id is not None:
        return db.get(OntUnit, candidate.ont_unit_id)
    serial = canonical_serial(candidate.serial_number)
    if not serial:
        return None
    rows = db.scalars(
        select(OntUnit).where(
            OntUnit.olt_device_id == candidate.olt_id,
            normalized_serial_sql(OntUnit.serial_number).in_(
                {
                    canonical_serial(candidate.serial_number),
                    canonical_serial(candidate.serial_hex),
                }
            ),
        )
    ).all()
    return next(
        (ont for ont in rows if canonical_serial(ont.serial_number) == serial),
        None,
    )


def _active_assignment(db: Session, ont_id: object | None) -> OntAssignment | None:
    if ont_id is None:
        return None
    return db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont_id,
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.assigned_at.desc(), OntAssignment.id)
        .limit(1)
    ).first()


def _transition(
    db: Session,
    intent: OntCommissioningIntent,
    state: OntCommissioningState,
    *,
    actor: str,
    failure_code: str | None = None,
    failure_message: str | None = None,
) -> None:
    previous = intent.state
    if previous == state and failure_code == intent.failure_code:
        return
    now = datetime.now(UTC)
    intent.state = state
    intent.failure_code = failure_code
    intent.failure_message = failure_message
    if state is OntCommissioningState.management_ready:
        intent.management_ready_at = intent.management_ready_at or now
    elif state is OntCommissioningState.assigned:
        intent.assigned_at = intent.assigned_at or now
        intent.terminal_at = intent.terminal_at or now
    elif state is OntCommissioningState.provisioned:
        intent.assigned_at = intent.assigned_at or now
        intent.provisioned_at = intent.provisioned_at or now
        intent.terminal_at = intent.terminal_at or now
    elif state is OntCommissioningState.cleanup_running:
        intent.cleanup_started_at = intent.cleanup_started_at or now
    elif state in {OntCommissioningState.expired, OntCommissioningState.canceled}:
        intent.terminal_at = intent.terminal_at or now
    emit_event(
        db,
        EventType.ont_commissioning_state_changed,
        {
            "intent_id": str(intent.id),
            "ont_unit_id": str(intent.ont_unit_id) if intent.ont_unit_id else None,
            "olt_id": str(intent.olt_id),
            "fsp": intent.fsp,
            "serial_number": intent.canonical_serial,
            "from": previous.value,
            "to": state.value,
            "failure_code": failure_code,
        },
        actor=actor,
    )


def _existing_admission(
    db: Session,
    intent: OntCommissioningIntent,
) -> OntCommissioningAdmission | None:
    operation = (
        db.get(NetworkOperation, intent.latest_operation_id)
        if intent.latest_operation_id
        else None
    )
    if operation is None or operation.status not in _OPERATION_ACTIVE_STATES:
        return None
    dispatch = operation.dispatches[-1] if operation.dispatches else None
    if dispatch is None:
        return None
    return OntCommissioningAdmission(
        intent_id=intent.id,
        operation_id=operation.id,
        dispatch_id=dispatch.id,
        expires_at=intent.expires_at,
        duplicate=True,
        message="This exact ONT commissioning intent is already in progress.",
    )


def _admit(
    db: Session,
    request: RequestOntCommissioning,
) -> OntCommissioningAdmission:
    candidate = db.scalars(
        select(OltAutofindCandidate)
        .where(OltAutofindCandidate.id == request.candidate_id)
        .with_for_update()
    ).first()
    if candidate is None or not candidate.is_active:
        raise _error(
            "candidate_not_active",
            "The selected autofind candidate is no longer active. Refresh the scan.",
        )
    expected_fsp = request.expected_fsp.strip()
    expected_serial = canonical_serial(request.expected_serial)
    observed_serial = canonical_serial(candidate.serial_number)
    if (
        candidate.olt_id != request.expected_olt_id
        or candidate.fsp.strip() != expected_fsp
        or observed_serial != expected_serial
    ):
        raise _error(
            "stale_target",
            "The autofind observation changed. Refresh and select the exact ONT again.",
            candidate_id=str(candidate.id),
        )
    reason = request.reason.strip()
    if not reason:
        raise _error(
            "reason_required",
            "A commissioning reason is required.",
        )
    if request.ttl <= timedelta(0) or request.ttl > MAX_COMMISSIONING_TTL:
        raise _error(
            "invalid_expiry",
            "Commissioning expiry must be greater than zero and no more than 72 hours.",
        )
    ont = _find_candidate_ont(db, candidate)
    if ont is not None and _active_assignment(db, ont.id) is not None:
        raise _error(
            "assignment_exists",
            "This ONT already has an active assignment; use Authorize & provision.",
            ont_unit_id=str(ont.id),
        )

    existing = db.scalars(
        select(OntCommissioningIntent)
        .where(
            OntCommissioningIntent.canonical_serial == observed_serial,
            OntCommissioningIntent.state.in_(_ACTIVE_INTENT_STATES),
        )
        .order_by(OntCommissioningIntent.created_at.desc())
        .with_for_update()
    ).first()
    if existing is not None:
        duplicate = _existing_admission(db, existing)
        if duplicate is not None:
            return duplicate
        if existing.state is not OntCommissioningState.failed:
            raise _error(
                "intent_conflict",
                "An active commissioning lifecycle already owns this ONT.",
                intent_id=str(existing.id),
            )
        intent = existing
        intent.autofind_candidate_id = candidate.id
        intent.olt_id = candidate.olt_id
        intent.fsp = expected_fsp
        intent.reason = reason
        intent.reference = request.reference.strip() if request.reference else None
        intent.requested_by = request.context.actor
        intent.expires_at = datetime.now(UTC) + request.ttl
        intent.failure_code = None
        intent.failure_message = None
        _transition(
            db,
            intent,
            OntCommissioningState.commissioning,
            actor=request.context.actor,
        )
    else:
        now = datetime.now(UTC)
        intent = OntCommissioningIntent(
            id=uuid.uuid4(),
            autofind_candidate_id=candidate.id,
            ont_unit_id=ont.id if ont is not None else None,
            olt_id=candidate.olt_id,
            canonical_serial=observed_serial,
            fsp=expected_fsp,
            state=OntCommissioningState.commissioning,
            reason=reason,
            reference=request.reference.strip() if request.reference else None,
            requested_by=request.context.actor,
            expires_at=now + request.ttl,
            created_at=now,
            updated_at=now,
        )
        db.add(intent)
        db.flush()

    from app.services.network.ont_provisioning_commands import (
        ont_authorization_correlation_key,
    )

    operation = network_operations.start(
        db,
        NetworkOperationType.ont_commission,
        NetworkOperationTargetType.olt,
        str(candidate.olt_id),
        correlation_key=ont_authorization_correlation_key(
            olt_id=str(candidate.olt_id),
            fsp=expected_fsp,
            serial_number=observed_serial,
        ),
        input_payload={
            "intent_id": str(intent.id),
            "candidate_id": str(candidate.id),
            "olt_id": str(candidate.olt_id),
            "fsp": expected_fsp,
            "serial_number": observed_serial,
            "management_only": True,
            "expires_at": intent.expires_at.isoformat(),
        },
        initiated_by=request.context.actor,
    )
    dispatch = stage_dispatch(
        db,
        operation,
        NetworkOperationCommand.ont_commission_v1,
    )
    intent.latest_operation_id = operation.id
    stage_audit_event(
        db,
        action="network.ont_commissioning.request",
        entity_type="ont_commissioning_intent",
        entity_id=str(intent.id),
        actor_type=AuditActorType.user,
        actor_id=request.context.actor,
        metadata={
            "candidate_id": str(candidate.id),
            "olt_id": str(candidate.olt_id),
            "fsp": expected_fsp,
            "serial_number": observed_serial,
            "expires_at": intent.expires_at.isoformat(),
            "reference": intent.reference,
            "management_only": True,
        },
    )
    emit_event(
        db,
        EventType.ont_commissioning_requested,
        {
            "intent_id": str(intent.id),
            "candidate_id": str(candidate.id),
            "olt_id": str(candidate.olt_id),
            "fsp": expected_fsp,
            "serial_number": observed_serial,
            "expires_at": intent.expires_at.isoformat(),
            "management_only": True,
        },
        actor=request.context.actor,
    )
    db.flush()
    return OntCommissioningAdmission(
        intent_id=intent.id,
        operation_id=operation.id,
        dispatch_id=dispatch.id,
        expires_at=intent.expires_at,
        duplicate=False,
        message=(
            "Management-only commissioning accepted. No customer internet, "
            "PPPoE, or Wi-Fi configuration will be applied."
        ),
    )


def request_ont_commissioning(
    db: Session,
    request: RequestOntCommissioning,
) -> OntCommissioningAdmission:
    """Atomically admit an exact commissioning intent and durable command."""

    try:
        return execute_owner_command(
            db,
            definition=_REQUEST_COMMISSIONING,
            context=request.context,
            operation=lambda: _admit(db, request),
        )
    except IntegrityError as exc:
        raise _error(
            "concurrent_admission",
            "This ONT was commissioned concurrently. Refresh its current intent.",
        ) from exc


def stage_commissioning_verification(
    db: Session,
    operation: NetworkOperation,
    *,
    attempt: int,
) -> NetworkOperationDispatch:
    """Stage one non-blocking ACS check on the existing commission operation."""

    delay = _VERIFY_DELAYS_SECONDS[attempt]
    return stage_dispatch(
        db,
        operation,
        NetworkOperationCommand.ont_commission_verify_v1,
        dispatch_key=f"verify:{attempt}",
        not_before=datetime.now(UTC) + timedelta(seconds=delay),
    )


def _exact_live_autofind_preflight(
    intent: OntCommissioningIntent,
    olt: OLTDevice,
) -> tuple[bool, str]:
    from app.services.network.olt_ssh_ont.autofind import query_ont_autofind

    ok, message, entries = query_ont_autofind(olt, port=intent.fsp)
    if not ok:
        return False, message
    match = next(
        (
            entry
            for entry in entries
            if entry.fsp.strip() == intent.fsp
            and intent.canonical_serial
            in {
                canonical_serial(entry.serial_number),
                canonical_serial(entry.serial_hex),
            }
        ),
        None,
    )
    if match is None:
        return (
            False,
            "The ONT is no longer present in live autofind on the exact "
            f"target {intent.fsp}; no OLT write was attempted.",
        )
    return True, "Exact live autofind target confirmed."


def _management_only_spec(
    db: Session,
    *,
    ont: OntUnit,
    olt: OLTDevice,
    fsp: str,
    ont_id_on_olt: int,
):
    from app.services.network.effective_ont_config import resolve_effective_ont_config
    from app.services.network.iphost_priority import (
        resolve_management_iphost_priority,
    )
    from app.services.network.olt_batched_mgmt import (
        BatchedMgmtSpec,
        build_management_command_batch,
    )
    from app.services.network.ont_management_ipam import allocate_ont_management_ip

    effective = resolve_effective_ont_config(db, ont, olt=olt)
    config_pack = effective.get("config_pack")
    values = effective.get("values", {})
    if config_pack is None:
        raise _error(
            "config_pack_missing",
            "The OLT has no effective configuration pack for commissioning.",
        )
    mgmt_vlan = values.get("mgmt_vlan")
    mgmt_gem = values.get("mgmt_gem_index")
    tr069_profile = values.get("tr069_olt_profile_id")
    missing = [
        name
        for name, value in (
            ("management VLAN", mgmt_vlan),
            ("management GEM", mgmt_gem),
            ("TR-069 OLT profile", tr069_profile),
            ("TR-069 ACS server", values.get("tr069_acs_server_id")),
        )
        if value is None
    ]
    if missing:
        raise _error(
            "management_prerequisite_missing",
            "Management-only commissioning is missing: " + ", ".join(missing) + ".",
        )

    ip_mode = "dhcp"
    ip_address = subnet_mask = gateway = None
    if olt.mgmt_ip_pool_id is not None:
        allocation = allocate_ont_management_ip(db, ont=ont, olt=olt)
        ip_mode = "static"
        ip_address = allocation.address
        subnet_mask = allocation.subnet
        gateway = allocation.gateway
        if not subnet_mask or not gateway:
            raise _error(
                "management_ip_incomplete",
                "The allocated management IP is missing subnet or gateway data.",
            )
    priority = resolve_management_iphost_priority(
        db,
        olt_id=olt.id,
        fsp=fsp,
        ont_id_on_olt=ont_id_on_olt,
        mgmt_vlan_tag=mgmt_vlan,
        mgmt_gem_index=mgmt_gem,
        line_profile_id=values.get("authorization_line_profile_id"),
    )
    if ip_mode == "static" and priority is None:
        raise _error(
            "management_priority_missing",
            "Management IPHOST priority could not be resolved from imported OLT state.",
        )
    spec = BatchedMgmtSpec(
        fsp=fsp,
        ont_id_on_olt=ont_id_on_olt,
        mgmt_vlan_tag=int(mgmt_vlan),
        mgmt_gem_index=int(mgmt_gem),
        ip_mode=ip_mode,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        ip_priority=int(priority) if priority is not None else 0,
        ip_index=0,
        internet_config_ip_index=None,
        wan_config_profile_id=None,
        tr069_profile_id=int(tr069_profile),
    )
    descriptions = {
        description for _command, description in build_management_command_batch(spec)
    }
    forbidden = descriptions.intersection({"activate_internet_config", "configure_wan"})
    if forbidden:
        raise _error(
            "service_config_forbidden",
            "Commissioning attempted to build customer service commands.",
            forbidden_steps=sorted(forbidden),
        )
    return spec


def _fail_execution(
    db: Session,
    *,
    intent: OntCommissioningIntent,
    operation_id: str,
    code: str,
    message: str,
) -> dict[str, object]:
    _transition(
        db,
        intent,
        OntCommissioningState.failed,
        actor="system",
        failure_code=code,
        failure_message=message,
    )
    operation = db.get(NetworkOperation, operation_id)
    if operation is not None and operation.status in _OPERATION_ACTIVE_STATES:
        network_operations.mark_failed(
            db,
            operation_id,
            message,
            output_payload={
                **(operation.output_payload or {}),
                "success": False,
                "intent_id": str(intent.id),
                "management_only": True,
                "failure_code": code,
                "message": message,
            },
        )
    db.commit()
    return {
        "success": False,
        "intent_id": str(intent.id),
        "operation_id": operation_id,
        "failure_code": code,
        "message": message,
    }


def execute_ont_commissioning(
    db: Session,
    *,
    intent_id: str,
    operation_id: str,
) -> dict[str, object]:
    """Execute exact authorization and the restricted management-only baseline."""

    intent = db.scalars(
        select(OntCommissioningIntent)
        .where(OntCommissioningIntent.id == _uuid(intent_id, "intent_id"))
        .with_for_update()
    ).first()
    if intent is None or str(intent.latest_operation_id) != operation_id:
        raise _error(
            "intent_not_found",
            "Commissioning intent or operation ownership was not found.",
        )
    operation = network_operations.get(db, operation_id)
    if operation.status is NetworkOperationStatus.pending:
        network_operations.mark_running(db, operation_id)
    _transition(
        db,
        intent,
        OntCommissioningState.authorizing,
        actor="system",
    )
    db.commit()

    olt = db.get(OLTDevice, intent.olt_id)
    if olt is None or not olt.is_active:
        return _fail_execution(
            db,
            intent=intent,
            operation_id=operation_id,
            code="olt_unavailable",
            message="The exact commissioning OLT is no longer active.",
        )
    if _active_assignment(db, intent.ont_unit_id) is not None:
        return _fail_execution(
            db,
            intent=intent,
            operation_id=operation_id,
            code="assignment_exists",
            message="An assignment now exists; use the assigned authorization workflow.",
        )

    ont_id_on_olt: int
    if intent.device_authorized_at is None:
        live_ok, live_message = _exact_live_autofind_preflight(intent, olt)
        if not live_ok:
            return _fail_execution(
                db,
                intent=intent,
                operation_id=operation_id,
                code="live_autofind_mismatch",
                message=live_message,
            )
        from app.services.network.ont_authorization import authorize_ont

        result = authorize_ont(
            db,
            str(intent.olt_id),
            intent.fsp,
            intent.canonical_serial,
            request=None,
            provision=False,
            operation_id=operation_id,
            allow_registration_move=False,
            dependency_scope=OltDependencyAuditScope.MANAGEMENT_ONLY,
        )
        if result.completed_authorization:
            # The device write is authoritative external evidence even when
            # the local inventory projection failed. Persist it before mapping
            # the workflow failure so expiry can never misclassify this as a
            # no-write intent and silently skip cleanup.
            intent = db.get(OntCommissioningIntent, intent.id)
            assert intent is not None
            intent.device_authorized_at = intent.device_authorized_at or datetime.now(
                UTC
            )
            if result.ont_unit_id:
                intent.ont_unit_id = _uuid(result.ont_unit_id, "ont_unit_id")
            db.commit()
        if not result.success or not result.ont_unit_id or result.ont_id_on_olt is None:
            return _fail_execution(
                db,
                intent=intent,
                operation_id=operation_id,
                code=(
                    "local_inventory_failed"
                    if result.local_inventory_failed
                    else "authorization_failed"
                ),
                message=result.message,
            )
        intent = db.get(OntCommissioningIntent, intent.id)
        assert intent is not None
        intent.ont_unit_id = _uuid(result.ont_unit_id, "ont_unit_id")
        intent.device_authorized_at = intent.device_authorized_at or datetime.now(UTC)
        candidate = (
            db.get(OltAutofindCandidate, intent.autofind_candidate_id)
            if intent.autofind_candidate_id
            else None
        )
        if candidate is not None:
            candidate.ont_unit_id = intent.ont_unit_id
            candidate.resolution_reason = "commissioned"
        db.commit()
        ont_id_on_olt = result.ont_id_on_olt
    else:
        ont = db.get(OntUnit, intent.ont_unit_id)
        if ont is None:
            return _fail_execution(
                db,
                intent=intent,
                operation_id=operation_id,
                code="inventory_missing",
                message="The commissioned ONT inventory row is missing.",
            )
        from app.services.network.serial_utils import parse_ont_id_on_olt

        parsed_ont_id = parse_ont_id_on_olt(ont.external_id)
        if parsed_ont_id is None:
            return _fail_execution(
                db,
                intent=intent,
                operation_id=operation_id,
                code="olt_ont_id_missing",
                message="The commissioned ONT ID on the OLT is unavailable.",
            )
        ont_id_on_olt = parsed_ont_id

    ont = db.get(OntUnit, intent.ont_unit_id)
    assert ont is not None
    if _active_assignment(db, ont.id) is not None:
        return _fail_execution(
            db,
            intent=intent,
            operation_id=operation_id,
            code="assignment_exists",
            message=(
                "An assignment was created during authorization; continue through "
                "the assigned Authorize & provision workflow."
            ),
        )
    try:
        spec = _management_only_spec(
            db,
            ont=ont,
            olt=olt,
            fsp=intent.fsp,
            ont_id_on_olt=ont_id_on_olt,
        )
    except OntCommissioningError as exc:
        return _fail_execution(
            db,
            intent=intent,
            operation_id=operation_id,
            code=exc.code.rsplit(".", 1)[-1],
            message=exc.message,
        )
    # IPAM reservation must land before the external OLT write.
    db.commit()

    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    management_result = get_protocol_adapter(olt).configure_management_batch(spec)
    completed_steps = list((management_result.data or {}).get("steps_completed", []))
    forbidden_steps = [
        step
        for step in completed_steps
        if str(step).startswith(("activate_internet_config", "configure_wan"))
    ]
    if forbidden_steps:
        return _fail_execution(
            db,
            intent=intent,
            operation_id=operation_id,
            code="service_config_forbidden",
            message="Commissioning crossed the management-only command boundary.",
        )
    if not management_result.success:
        return _fail_execution(
            db,
            intent=intent,
            operation_id=operation_id,
            code="management_apply_failed",
            message=management_result.message,
        )

    intent = db.get(OntCommissioningIntent, intent.id)
    assert intent is not None
    operation = network_operations.get(db, operation_id)
    _transition(
        db,
        intent,
        OntCommissioningState.awaiting_acs,
        actor="system",
    )
    network_operations.merge_output_payload(
        db,
        operation_id,
        {
            "success": True,
            "waiting": True,
            "intent_id": str(intent.id),
            "ont_unit_id": str(intent.ont_unit_id),
            "management_only": True,
            "management_steps": completed_steps,
        },
    )
    network_operations.mark_waiting(
        db,
        operation_id,
        "Management path applied; waiting for the commissioned ONT to inform ACS.",
    )
    dispatch = stage_commissioning_verification(db, operation, attempt=0)
    db.commit()
    return {
        "success": True,
        "waiting": True,
        "intent_id": str(intent.id),
        "ont_unit_id": str(intent.ont_unit_id),
        "operation_id": operation_id,
        "verification_dispatch_id": str(dispatch.id),
        "management_only": True,
        "management_steps": completed_steps,
        "message": "Management path applied; waiting for ACS.",
    }


def verify_ont_commissioning(
    db: Session,
    *,
    intent_id: str,
    operation_id: str,
    attempt: int,
) -> dict[str, object]:
    """Perform one bounded, non-blocking ACS readiness observation."""

    intent = db.get(OntCommissioningIntent, _uuid(intent_id, "intent_id"))
    if intent is None or str(intent.latest_operation_id) != operation_id:
        raise _error("intent_not_found", "Commissioning intent was not found.")
    if intent.state is OntCommissioningState.management_ready:
        return {
            "success": True,
            "intent_id": intent_id,
            "operation_id": operation_id,
            "message": "ONT is already management-ready.",
        }
    if intent.state is not OntCommissioningState.awaiting_acs:
        return {
            "success": False,
            "intent_id": intent_id,
            "operation_id": operation_id,
            "message": f"Commissioning is {intent.state.value}; ACS check skipped.",
        }
    ont = db.get(OntUnit, intent.ont_unit_id)
    if ont is None:
        return _fail_execution(
            db,
            intent=intent,
            operation_id=operation_id,
            code="inventory_missing",
            message="The commissioned ONT inventory row is missing.",
        )
    from app.services.network._resolve import resolve_genieacs_with_reason

    resolved, reason = resolve_genieacs_with_reason(db, ont)
    if resolved is not None:
        _transition(
            db,
            intent,
            OntCommissioningState.management_ready,
            actor="system",
        )
        operation = network_operations.get(db, operation_id)
        network_operations.mark_succeeded(
            db,
            operation_id,
            output_payload={
                **(operation.output_payload or {}),
                "success": True,
                "waiting": False,
                "intent_id": intent_id,
                "ont_unit_id": str(ont.id),
                "management_only": True,
                "management_ready": True,
                "confirmation_source": reason,
            },
        )
        db.commit()
        return {
            "success": True,
            "waiting": False,
            "intent_id": intent_id,
            "operation_id": operation_id,
            "message": "ONT is management-ready in ACS.",
        }
    if attempt < 4:
        operation = network_operations.get(db, operation_id)
        dispatch = stage_commissioning_verification(
            db,
            operation,
            attempt=attempt + 1,
        )
        db.commit()
        return {
            "success": True,
            "waiting": True,
            "intent_id": intent_id,
            "operation_id": operation_id,
            "verification_dispatch_id": str(dispatch.id),
            "message": reason,
        }
    _transition(
        db,
        intent,
        OntCommissioningState.failed,
        actor="system",
        failure_code="acs_not_ready",
        failure_message=reason,
    )
    operation = network_operations.get(db, operation_id)
    network_operations.mark_warning(
        db,
        operation_id,
        reason,
        output_payload={
            **(operation.output_payload or {}),
            "success": False,
            "waiting": False,
            "intent_id": intent_id,
            "management_only": True,
            "management_ready": False,
            "failure_code": "acs_not_ready",
        },
    )
    db.commit()
    return {
        "success": False,
        "waiting": False,
        "intent_id": intent_id,
        "operation_id": operation_id,
        "message": reason,
    }


def complete_commissioning_after_inform(
    db: Session,
    *,
    ont_id: str,
    reason: str,
) -> bool:
    """Close commissioning from Inform without applying saved service intent."""

    intent = db.scalars(
        select(OntCommissioningIntent)
        .where(
            OntCommissioningIntent.ont_unit_id == _uuid(ont_id, "ont_id"),
            OntCommissioningIntent.state == OntCommissioningState.awaiting_acs,
        )
        .order_by(OntCommissioningIntent.created_at.desc())
        .with_for_update()
        .limit(1)
    ).first()
    if intent is None or intent.latest_operation_id is None:
        return False
    operation = db.get(NetworkOperation, intent.latest_operation_id)
    if operation is None or operation.status not in _OPERATION_ACTIVE_STATES:
        return False
    _transition(
        db,
        intent,
        OntCommissioningState.management_ready,
        actor="tr069_inform",
    )
    network_operations.mark_succeeded(
        db,
        str(operation.id),
        output_payload={
            **(operation.output_payload or {}),
            "success": True,
            "waiting": False,
            "intent_id": str(intent.id),
            "ont_unit_id": ont_id,
            "management_only": True,
            "management_ready": True,
            "confirmation_source": reason,
        },
    )
    return True


def _reconcile(
    db: Session,
    *,
    context: CommandContext,
    now: datetime,
) -> OntCommissioningReconcileResult:
    intents = list(
        db.scalars(
            select(OntCommissioningIntent)
            .where(OntCommissioningIntent.state.in_(_ACTIVE_INTENT_STATES))
            .order_by(OntCommissioningIntent.expires_at, OntCommissioningIntent.id)
            .with_for_update()
        )
    )
    assigned = provisioned = cleanup_staged = expired_without_write = 0
    for intent in intents:
        intent.last_reconciled_at = now
        assignment = _active_assignment(db, intent.ont_unit_id)
        if assignment is not None:
            ont = db.get(OntUnit, intent.ont_unit_id)
            if (
                ont is not None
                and ont.provisioning_status is OntProvisioningStatus.provisioned
            ):
                _transition(
                    db,
                    intent,
                    OntCommissioningState.provisioned,
                    actor=context.actor,
                )
                provisioned += 1
            else:
                _transition(
                    db,
                    intent,
                    OntCommissioningState.assigned,
                    actor=context.actor,
                )
                assigned += 1
            continue
        if _aware_utc(intent.expires_at) > now:
            continue
        if intent.device_authorized_at is None:
            _transition(
                db,
                intent,
                OntCommissioningState.expired,
                actor=context.actor,
            )
            expired_without_write += 1
            continue
        if intent.ont_unit_id is None:
            _transition(
                db,
                intent,
                OntCommissioningState.cleanup_pending,
                actor=context.actor,
                failure_code="cleanup_target_missing",
                failure_message=(
                    "OLT authorization landed, but no local ONT target exists; "
                    "manual projection repair is required before cleanup."
                ),
            )
            continue
        if intent.cleanup_operation_id is not None:
            continue
        prior = (
            db.get(NetworkOperation, intent.latest_operation_id)
            if intent.latest_operation_id
            else None
        )
        if prior is not None and prior.status in _OPERATION_ACTIVE_STATES:
            network_operations.mark_warning(
                db,
                str(prior.id),
                "Commissioning expired before assignment; cleanup was staged.",
                output_payload={
                    **(prior.output_payload or {}),
                    "success": False,
                    "waiting": False,
                    "expired": True,
                    "intent_id": str(intent.id),
                },
            )
        cleanup = network_operations.start(
            db,
            NetworkOperationType.ont_commission_cleanup,
            NetworkOperationTargetType.ont,
            str(intent.ont_unit_id),
            correlation_key=f"ont_commission_cleanup:{intent.id}",
            input_payload={
                "intent_id": str(intent.id),
                "ont_id": str(intent.ont_unit_id),
                "olt_id": str(intent.olt_id),
                "fsp": intent.fsp,
                "serial_number": intent.canonical_serial,
            },
            initiated_by=context.actor,
        )
        stage_dispatch(
            db,
            cleanup,
            NetworkOperationCommand.ont_commission_cleanup_v1,
        )
        intent.cleanup_operation_id = cleanup.id
        _transition(
            db,
            intent,
            OntCommissioningState.cleanup_pending,
            actor=context.actor,
        )
        cleanup_staged += 1
    db.flush()
    return OntCommissioningReconcileResult(
        examined=len(intents),
        assigned=assigned,
        provisioned=provisioned,
        cleanup_staged=cleanup_staged,
        expired_without_device_write=expired_without_write,
    )


def reconcile_ont_commissioning(
    db: Session,
    *,
    context: CommandContext,
    now: datetime | None = None,
) -> OntCommissioningReconcileResult:
    """Reconcile assignment conversion and expired-intent cleanup admission."""

    current = _aware_utc(now or datetime.now(UTC))
    return execute_owner_command(
        db,
        definition=_RECONCILE_COMMISSIONING,
        context=context,
        operation=lambda: _reconcile(db, context=context, now=current),
    )


def cleanup_ont_commissioning(
    db: Session,
    *,
    intent_id: str,
    operation_id: str,
) -> dict[str, object]:
    """Safely remove an expired, still-unassigned commissioned device."""

    intent = db.scalars(
        select(OntCommissioningIntent)
        .where(OntCommissioningIntent.id == _uuid(intent_id, "intent_id"))
        .with_for_update()
    ).first()
    if (
        intent is None
        or str(intent.cleanup_operation_id) != operation_id
        or intent.ont_unit_id is None
    ):
        raise _error("intent_not_found", "Commissioning cleanup intent was not found.")
    ont = db.scalars(
        select(OntUnit).where(OntUnit.id == intent.ont_unit_id).with_for_update()
    ).first()
    if ont is None:
        message = "Commissioned ONT inventory is missing; cleanup needs review."
        _transition(
            db,
            intent,
            OntCommissioningState.cleanup_pending,
            actor="system",
            failure_code="inventory_missing",
            failure_message=message,
        )
        network_operations.mark_failed(db, operation_id, message)
        db.commit()
        return {
            "success": False,
            "intent_id": intent_id,
            "failure_code": "inventory_missing",
            "message": message,
        }
    if _active_assignment(db, ont.id) is not None:
        _transition(
            db,
            intent,
            OntCommissioningState.assigned,
            actor="system",
        )
        network_operations.mark_canceled(db, operation_id)
        db.commit()
        return {
            "success": True,
            "skipped": True,
            "intent_id": intent_id,
            "message": "Cleanup canceled because an active assignment now exists.",
        }
    current_fsp = (
        f"0/{ont.board}/{ont.port}"
        if ont.board is not None and ont.port is not None
        else None
    )
    if (
        canonical_serial(ont.serial_number) != intent.canonical_serial
        or str(ont.olt_device_id) != str(intent.olt_id)
        or current_fsp != intent.fsp
    ):
        message = (
            "Cleanup stopped because the ONT identity no longer matches the intent."
        )
        _transition(
            db,
            intent,
            OntCommissioningState.cleanup_pending,
            actor="system",
            failure_code="cleanup_identity_mismatch",
            failure_message=message,
        )
        network_operations.mark_failed(db, operation_id, message)
        db.commit()
        return {
            "success": False,
            "intent_id": intent_id,
            "failure_code": "cleanup_identity_mismatch",
            "message": message,
        }
    network_operations.mark_running(db, operation_id)
    _transition(
        db,
        intent,
        OntCommissioningState.cleanup_running,
        actor="system",
    )
    db.commit()

    from app.services.network.ont_inventory import return_ont_to_inventory

    result = return_ont_to_inventory(db, str(ont.id))
    intent = db.get(OntCommissioningIntent, _uuid(intent_id, "intent_id"))
    assert intent is not None
    if not result.success:
        _transition(
            db,
            intent,
            OntCommissioningState.cleanup_pending,
            actor="system",
            failure_code="cleanup_failed",
            failure_message=result.message,
        )
        network_operations.mark_failed(db, operation_id, result.message)
        db.commit()
        return {
            "success": False,
            "intent_id": intent_id,
            "failure_code": "cleanup_failed",
            "message": result.message,
        }
    now = datetime.now(UTC)
    intent.cleanup_completed_at = now
    _transition(
        db,
        intent,
        OntCommissioningState.expired,
        actor="system",
    )
    network_operations.mark_succeeded(
        db,
        operation_id,
        output_payload={
            "success": True,
            "intent_id": intent_id,
            "ont_unit_id": str(ont.id),
            "message": result.message,
        },
    )
    db.commit()
    return {
        "success": True,
        "intent_id": intent_id,
        "operation_id": operation_id,
        "message": result.message,
    }


def latest_intents_for_candidates(
    db: Session,
    candidate_ids: list[object],
) -> dict[str, OntCommissioningIntent]:
    """Return the most recent intent keyed by autofind candidate id."""

    ids = [_uuid(value, "candidate_id") for value in candidate_ids]
    if not ids:
        return {}
    rows = db.scalars(
        select(OntCommissioningIntent)
        .where(OntCommissioningIntent.autofind_candidate_id.in_(ids))
        .order_by(
            OntCommissioningIntent.autofind_candidate_id,
            OntCommissioningIntent.created_at.desc(),
        )
    ).all()
    output: dict[str, OntCommissioningIntent] = {}
    for row in rows:
        key = str(row.autofind_candidate_id)
        output.setdefault(key, row)
    return output


def latest_intent_for_ont(
    db: Session,
    ont_id: object,
) -> OntCommissioningIntent | None:
    return db.scalars(
        select(OntCommissioningIntent)
        .where(OntCommissioningIntent.ont_unit_id == _uuid(ont_id, "ont_id"))
        .order_by(OntCommissioningIntent.created_at.desc())
        .limit(1)
    ).first()


def active_candidate_for_ont(
    db: Session,
    ont: OntUnit,
) -> OltAutofindCandidate | None:
    """Return the active exact autofind candidate represented by an ONT row."""

    if ont.olt_device_id is None:
        return None
    candidates = db.scalars(
        select(OltAutofindCandidate)
        .where(
            OltAutofindCandidate.olt_id == ont.olt_device_id,
            OltAutofindCandidate.is_active.is_(True),
        )
        .order_by(OltAutofindCandidate.last_seen_at.desc())
    ).all()
    expected_fsp = (
        f"0/{ont.board}/{ont.port}"
        if ont.board is not None and ont.port is not None
        else None
    )
    return next(
        (
            candidate
            for candidate in candidates
            if (
                candidate.ont_unit_id == ont.id
                or canonical_serial(candidate.serial_number)
                == canonical_serial(ont.serial_number)
            )
            and (expected_fsp is None or candidate.fsp == expected_fsp)
        ),
        None,
    )
