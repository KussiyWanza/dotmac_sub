"""Owner for temporary, assignment-free ONT commissioning.

Commissioning is deliberately narrower than service authorization. It owns a
time-bounded intent for one exact OLT/F/S/P/serial and may install only the
management VLAN service-port, IPHOST, and TR-069 profile. It never creates an
``OntAssignment`` and never applies customer internet, PPPoE, WAN, LAN, or Wi-Fi
configuration.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

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
    NetworkOperationDispatchStatus,
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
from app.services.network.olt_batched_mgmt import (
    BatchedMgmtSpec,
    build_management_command_batch,
)
from app.services.network.ont_authorization_contracts import (
    OntAuthorizationTarget,
    OntFsp,
    OntSerialNumber,
    RegisterCommissioningOnt,
)
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

    from app.services.network.olt_protocol_adapters import (
        OltConnectionConfig,
        OltProtocolAdapterContract,
    )

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
    expected_fsp: OntFsp
    expected_serial: OntSerialNumber
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
    recovery_staged: int
    recovery_failed_closed: int
    cleanup_staged: int
    expired_without_device_write: int


@dataclass(frozen=True)
class ExecuteOntCommissioning:
    """Typed worker command for one durable commissioning operation."""

    context: CommandContext
    intent_id: uuid.UUID
    operation_id: uuid.UUID


@dataclass(frozen=True)
class OntCommissioningExecutionOutcome:
    """Typed result retained until the task adapter serializes it."""

    success: bool
    intent_id: uuid.UUID
    operation_id: uuid.UUID
    state: OntCommissioningState
    message: str
    waiting: bool = False
    failure_code: str | None = None
    ont_unit_id: uuid.UUID | None = None
    verification_dispatch_id: uuid.UUID | None = None
    management_steps: tuple[str, ...] = ()
    management_recovery: bool = False

    def to_transport(self) -> dict[str, object]:
        """Serialize domain values only at the Celery transport boundary."""

        return {
            "success": self.success,
            "waiting": self.waiting,
            "intent_id": str(self.intent_id),
            "ont_unit_id": (
                str(self.ont_unit_id) if self.ont_unit_id is not None else None
            ),
            "operation_id": str(self.operation_id),
            "verification_dispatch_id": (
                str(self.verification_dispatch_id)
                if self.verification_dispatch_id is not None
                else None
            ),
            "state": self.state.value,
            "failure_code": self.failure_code,
            "management_only": True,
            "management_steps": list(self.management_steps),
            "management_recovery": self.management_recovery,
            "message": self.message,
        }


@dataclass(frozen=True)
class RecordOntCommissioningExternalWriteFailure:
    """Typed reliability command for a worker lost after an OLT write."""

    context: CommandContext
    intent_id: uuid.UUID
    operation_id: uuid.UUID


@dataclass(frozen=True)
class ExternalWriteReconciliationOutcome:
    """Typed reliability result for fresh-session failure recording."""

    intent_id: uuid.UUID
    operation_id: uuid.UUID
    recorded: bool


@dataclass(frozen=True)
class _CommissioningPreflightOutcome:
    success: bool
    message: str


@dataclass(frozen=True)
class _CommissioningManagementPlan:
    """Immutable management values materialized before external device I/O."""

    fsp: OntFsp
    ont_id_on_olt: int
    mgmt_vlan_tag: int
    mgmt_gem_index: int
    ip_mode: str
    ip_address: str | None
    subnet_mask: str | None
    gateway: str | None
    ip_priority: int
    tr069_profile_id: int

    def to_adapter_spec(self) -> BatchedMgmtSpec:
        return BatchedMgmtSpec(
            fsp=self.fsp.value,
            ont_id_on_olt=self.ont_id_on_olt,
            mgmt_vlan_tag=self.mgmt_vlan_tag,
            mgmt_gem_index=self.mgmt_gem_index,
            ip_mode=self.ip_mode,
            ip_address=self.ip_address,
            subnet_mask=self.subnet_mask,
            gateway=self.gateway,
            ip_priority=self.ip_priority,
            ip_index=0,
            internet_config_ip_index=None,
            wan_config_profile_id=None,
            tr069_profile_id=self.tr069_profile_id,
        )


@dataclass(frozen=True)
class _CommissioningExecutionPlan:
    """Detached, immutable plan consumed after the database phase commits."""

    intent_id: uuid.UUID
    operation_id: uuid.UUID
    ont_unit_id: uuid.UUID
    target: OntAuthorizationTarget
    ont_id_on_olt: int
    olt: OltConnectionConfig
    management: _CommissioningManagementPlan
    verify_registration: bool
    management_recovery: bool


@dataclass(frozen=True)
class _LandedAuthorizationEvidence:
    """Validated operation-ledger provenance for a landed OLT registration."""

    source_operation_id: uuid.UUID
    target: OntAuthorizationTarget
    ont_id_on_olt: int

    def to_output_fragment(self) -> dict[str, object]:
        return {
            "completed_authorization": True,
            "device_authorization": {
                "olt_id": str(self.target.olt_id),
                "fsp": self.target.fsp.value,
                "serial_number": self.target.serial_number.value,
                "ont_id_on_olt": self.ont_id_on_olt,
            },
            "authorization_reused_from_operation_id": str(self.source_operation_id),
        }


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
    expected_fsp = request.expected_fsp.value
    expected_serial = request.expected_serial.value
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
    *,
    target: OntAuthorizationTarget,
    olt_config: OltConnectionConfig,
) -> _CommissioningPreflightOutcome:
    from app.services.network.olt_ssh_ont.autofind import query_ont_autofind

    ok, message, entries = query_ont_autofind(
        cast(OLTDevice, olt_config),
        port=target.fsp.value,
    )
    if not ok:
        return _CommissioningPreflightOutcome(False, message)
    match = next(
        (
            entry
            for entry in entries
            if entry.fsp.strip() == target.fsp.value
            and target.serial_number.value
            in {
                canonical_serial(entry.serial_number),
                canonical_serial(entry.serial_hex),
            }
        ),
        None,
    )
    if match is None:
        return _CommissioningPreflightOutcome(
            success=False,
            message=(
                "The ONT is no longer present in live autofind on the exact "
                f"target {target.fsp.value}; no OLT write was attempted."
            ),
        )
    return _CommissioningPreflightOutcome(
        True,
        "Exact live autofind target confirmed.",
    )


def _management_only_plan(
    db: Session,
    *,
    ont: OntUnit,
    olt: OLTDevice,
    fsp: str,
    ont_id_on_olt: int,
) -> _CommissioningManagementPlan:
    from app.services.network.effective_ont_config import resolve_effective_ont_config
    from app.services.network.iphost_priority import (
        resolve_management_iphost_priority,
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
    plan = _CommissioningManagementPlan(
        fsp=OntFsp.parse(fsp),
        ont_id_on_olt=ont_id_on_olt,
        mgmt_vlan_tag=int(mgmt_vlan),
        mgmt_gem_index=int(mgmt_gem),
        ip_mode=ip_mode,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        ip_priority=int(priority) if priority is not None else 0,
        tr069_profile_id=int(tr069_profile),
    )
    descriptions = {
        description
        for _command, description in build_management_command_batch(
            plan.to_adapter_spec()
        )
    }
    forbidden = descriptions.intersection({"activate_internet_config", "configure_wan"})
    if forbidden:
        raise _error(
            "service_config_forbidden",
            "Commissioning attempted to build customer service commands.",
            forbidden_steps=sorted(forbidden),
        )
    return plan


def _fail_execution(
    db: Session,
    *,
    command: ExecuteOntCommissioning,
    code: str,
    message: str,
) -> OntCommissioningExecutionOutcome:
    intent = db.scalars(
        select(OntCommissioningIntent)
        .where(OntCommissioningIntent.id == command.intent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if intent is None or intent.latest_operation_id != command.operation_id:
        raise _error(
            "intent_not_found",
            "Commissioning intent or operation ownership was not found.",
        )
    _transition(
        db,
        intent,
        OntCommissioningState.failed,
        actor=command.context.actor,
        failure_code=code,
        failure_message=message,
    )
    operation = db.get(NetworkOperation, command.operation_id)
    if operation is not None and operation.status in _OPERATION_ACTIVE_STATES:
        network_operations.mark_failed(
            db,
            str(command.operation_id),
            message,
            output_payload={
                **(operation.output_payload or {}),
                "success": False,
                "intent_id": str(command.intent_id),
                "management_only": True,
                "failure_code": code,
                "message": message,
            },
        )
    outcome = OntCommissioningExecutionOutcome(
        success=False,
        intent_id=command.intent_id,
        operation_id=command.operation_id,
        ont_unit_id=intent.ont_unit_id,
        state=OntCommissioningState.failed,
        failure_code=code,
        message=message,
    )
    db.commit()
    return outcome


def record_external_write_reconciliation_required(
    db: Session,
    command: RecordOntCommissioningExternalWriteFailure,
) -> ExternalWriteReconciliationOutcome:
    """Record partial external success using a fresh reliability session."""

    intent = db.scalars(
        select(OntCommissioningIntent)
        .where(OntCommissioningIntent.id == command.intent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        intent is None
        or intent.latest_operation_id != command.operation_id
        or intent.device_authorized_at is None
        or intent.state is not OntCommissioningState.authorizing
    ):
        db.rollback()
        return ExternalWriteReconciliationOutcome(
            intent_id=command.intent_id,
            operation_id=command.operation_id,
            recorded=False,
        )
    _transition(
        db,
        intent,
        OntCommissioningState.failed,
        actor=command.context.actor,
        failure_code="external_write_reconciliation_required",
        failure_message=(
            "OLT authorization completed, but commissioning lost its database "
            "session before management readiness was persisted."
        ),
    )
    operation = db.get(NetworkOperation, command.operation_id)
    if operation is not None and operation.status in _OPERATION_ACTIVE_STATES:
        network_operations.mark_failed(
            db,
            str(command.operation_id),
            intent.failure_message or "External write requires reconciliation.",
            output_payload={
                **(operation.output_payload or {}),
                "success": False,
                "failure_code": "external_write_reconciliation_required",
                "reconciliation_required": True,
            },
        )
    db.commit()
    return ExternalWriteReconciliationOutcome(
        intent_id=command.intent_id,
        operation_id=command.operation_id,
        recorded=True,
    )


def _verify_recovery_registration(
    *,
    target: OntAuthorizationTarget,
    ont_id_on_olt: int,
    adapter: OltProtocolAdapterContract,
) -> _CommissioningPreflightOutcome:
    """Fail closed unless recovery still targets the landed registration."""

    result = adapter.find_ont_by_serial(target.serial_number.value)
    registration = (result.data or {}).get("registration")
    if not result.success or registration is None:
        return _CommissioningPreflightOutcome(
            False,
            result.message
            or "The previously authorized ONT registration could not be confirmed.",
        )
    observed_fsp = str(getattr(registration, "fsp", "") or "").strip()
    observed_ont_id = getattr(registration, "onu_id", None)
    observed_serial = canonical_serial(getattr(registration, "real_serial", None))
    if (
        observed_fsp != target.fsp.value
        or observed_ont_id != ont_id_on_olt
        or observed_serial != target.serial_number.value
    ):
        return _CommissioningPreflightOutcome(
            False,
            "The live OLT registration no longer matches the exact commissioning "
            "serial, F/S/P, and ONT ID; management recovery was not attempted.",
        )
    return _CommissioningPreflightOutcome(
        True,
        "The landed OLT registration was confirmed for management recovery.",
    )


def execute_ont_commissioning(
    db: Session,
    command: ExecuteOntCommissioning,
) -> OntCommissioningExecutionOutcome:
    """Execute exact authorization and the restricted management-only baseline."""

    intent = db.scalars(
        select(OntCommissioningIntent)
        .where(OntCommissioningIntent.id == command.intent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if intent is None or intent.latest_operation_id != command.operation_id:
        raise _error(
            "intent_not_found",
            "Commissioning intent or operation ownership was not found.",
        )
    operation = db.scalars(
        select(NetworkOperation)
        .where(NetworkOperation.id == command.operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if operation is None:
        raise _error("intent_not_found", "Commissioning operation was not found.")
    if operation.status is NetworkOperationStatus.pending:
        network_operations.mark_running(db, str(command.operation_id))
    _transition(
        db,
        intent,
        OntCommissioningState.authorizing,
        actor=command.context.actor,
    )

    olt = db.get(OLTDevice, intent.olt_id)
    if olt is None or not olt.is_active:
        return _fail_execution(
            db,
            command=command,
            code="olt_unavailable",
            message="The exact commissioning OLT is no longer active.",
        )
    if _active_assignment(db, intent.ont_unit_id) is not None:
        return _fail_execution(
            db,
            command=command,
            code="assignment_exists",
            message="An assignment now exists; use the assigned authorization workflow.",
        )

    from app.services.network.olt_protocol_adapters import OltConnectionConfig

    authorization_olt_config = OltConnectionConfig.from_model(olt)
    authorization_serial = intent.canonical_serial
    authorization_fsp = intent.fsp
    authorization_intent_id = intent.id
    authorization_olt_id = intent.olt_id
    authorization_candidate_id = intent.autofind_candidate_id
    authorization_ont_unit_id = intent.ont_unit_id
    authorization_already_recorded = intent.device_authorized_at is not None
    authorization_target = OntAuthorizationTarget.from_transport(
        olt_id=authorization_olt_id,
        fsp=authorization_fsp,
        serial_number=authorization_serial,
    )
    operation_actor = operation.initiated_by or command.context.actor
    db.commit()

    ont_id_on_olt: int
    if not authorization_already_recorded:
        if db.in_transaction():
            raise _error(
                "unsafe_external_transaction",
                "Live OLT preflight cannot run inside a database transaction.",
            )
        preflight = _exact_live_autofind_preflight(
            target=authorization_target,
            olt_config=authorization_olt_config,
        )
        if not preflight.success:
            return _fail_execution(
                db,
                command=command,
                code="live_autofind_mismatch",
                message=preflight.message,
            )
        from app.services.network.ont_authorization import (
            register_ont_for_commissioning,
        )

        result = register_ont_for_commissioning(
            db,
            RegisterCommissioningOnt(
                context=CommandContext.system(
                    actor=operation_actor,
                    scope="network:ont:commission",
                    reason="execute durable management-only ONT commissioning",
                    command_id=command.operation_id,
                    correlation_id=command.context.correlation_id,
                    causation_id=authorization_intent_id,
                ),
                operation_id=command.operation_id,
                intent_id=authorization_intent_id,
                target=authorization_target,
            ),
        )
        if result.completed_authorization:
            # The device write is authoritative external evidence even when
            # the local inventory projection failed. Persist it before mapping
            # the workflow failure so expiry can never misclassify this as a
            # no-write intent and silently skip cleanup.
            intent = db.get(OntCommissioningIntent, authorization_intent_id)
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
                command=command,
                code=(
                    "local_inventory_failed"
                    if result.local_inventory_failed
                    else "authorization_failed"
                ),
                message=result.message,
            )
        intent = db.get(OntCommissioningIntent, authorization_intent_id)
        assert intent is not None
        intent.ont_unit_id = _uuid(result.ont_unit_id, "ont_unit_id")
        intent.device_authorized_at = intent.device_authorized_at or datetime.now(UTC)
        candidate = (
            db.get(OltAutofindCandidate, authorization_candidate_id)
            if authorization_candidate_id
            else None
        )
        if candidate is not None:
            candidate.ont_unit_id = intent.ont_unit_id
            candidate.resolution_reason = "commissioned"
        db.commit()
        ont_id_on_olt = result.ont_id_on_olt
    else:
        ont = db.get(OntUnit, authorization_ont_unit_id)
        if ont is None:
            return _fail_execution(
                db,
                command=command,
                code="inventory_missing",
                message="The commissioned ONT inventory row is missing.",
            )
        from app.services.network.serial_utils import parse_ont_id_on_olt

        parsed_ont_id = parse_ont_id_on_olt(ont.external_id)
        if parsed_ont_id is None:
            return _fail_execution(
                db,
                command=command,
                code="olt_ont_id_missing",
                message="The commissioned ONT ID on the OLT is unavailable.",
            )
        ont_id_on_olt = parsed_ont_id

    intent = db.scalars(
        select(OntCommissioningIntent)
        .where(OntCommissioningIntent.id == command.intent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if intent is None or intent.latest_operation_id != command.operation_id:
        raise _error(
            "execution_conflict",
            "Commissioning ownership changed before management planning.",
        )
    operation = db.scalars(
        select(NetworkOperation)
        .where(NetworkOperation.id == command.operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if operation is None or operation.status not in _OPERATION_ACTIVE_STATES:
        raise _error(
            "execution_conflict",
            "Commissioning operation changed before management planning.",
        )
    ont = db.get(OntUnit, intent.ont_unit_id)
    if ont is None:
        return _fail_execution(
            db,
            command=command,
            code="inventory_missing",
            message="The commissioned ONT inventory row is missing.",
        )
    management_olt = db.get(OLTDevice, authorization_olt_id, populate_existing=True)
    if management_olt is None or not management_olt.is_active:
        return _fail_execution(
            db,
            command=command,
            code="olt_unavailable",
            message="The exact commissioning OLT is no longer active.",
        )
    if _active_assignment(db, ont.id) is not None:
        return _fail_execution(
            db,
            command=command,
            code="assignment_exists",
            message=(
                "An assignment was created during authorization; continue through "
                "the assigned Authorize & provision workflow."
            ),
        )
    try:
        management = _management_only_plan(
            db,
            ont=ont,
            olt=management_olt,
            fsp=intent.fsp,
            ont_id_on_olt=ont_id_on_olt,
        )
    except OntCommissioningError as exc:
        return _fail_execution(
            db,
            command=command,
            code=exc.code.rsplit(".", 1)[-1],
            message=exc.message,
        )
    from app.services.network.olt_protocol_adapters import OltConnectionConfig

    plan = _CommissioningExecutionPlan(
        intent_id=command.intent_id,
        operation_id=command.operation_id,
        ont_unit_id=ont.id,
        target=authorization_target,
        ont_id_on_olt=ont_id_on_olt,
        olt=OltConnectionConfig.from_model(management_olt),
        management=management,
        verify_registration=authorization_already_recorded,
        management_recovery=(
            authorization_already_recorded or operation.redrive_of_id is not None
        ),
    )
    # IPAM reservation and the immutable execution plan must land before OLT I/O.
    db.commit()
    if db.in_transaction():
        raise _error(
            "unsafe_external_transaction",
            "Management configuration cannot run inside a database transaction.",
        )

    from app.services.network.olt_protocol_adapters import (
        get_protocol_adapter_from_config,
    )

    adapter = get_protocol_adapter_from_config(plan.olt)
    if plan.verify_registration:
        registration = _verify_recovery_registration(
            target=plan.target,
            ont_id_on_olt=plan.ont_id_on_olt,
            adapter=adapter,
        )
        if not registration.success:
            return _fail_execution(
                db,
                command=command,
                code="registration_not_confirmed",
                message=registration.message,
            )
        if db.in_transaction():
            raise _error(
                "unsafe_external_transaction",
                "Management recovery cannot run inside a database transaction.",
            )
    management_result = adapter.configure_management_batch(
        plan.management.to_adapter_spec()
    )
    completed_steps = tuple(
        str(step) for step in (management_result.data or {}).get("steps_completed", [])
    )
    forbidden_steps = [
        step
        for step in completed_steps
        if str(step).startswith(("activate_internet_config", "configure_wan"))
    ]
    if forbidden_steps:
        return _fail_execution(
            db,
            command=command,
            code="service_config_forbidden",
            message="Commissioning crossed the management-only command boundary.",
        )
    if not management_result.success:
        return _fail_execution(
            db,
            command=command,
            code="management_apply_failed",
            message=management_result.message,
        )

    intent = db.scalars(
        select(OntCommissioningIntent)
        .where(OntCommissioningIntent.id == plan.intent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        intent is None
        or intent.latest_operation_id != plan.operation_id
        or intent.state is not OntCommissioningState.authorizing
    ):
        raise _error(
            "execution_conflict",
            "Commissioning state changed while management configuration was running.",
        )
    operation = db.scalars(
        select(NetworkOperation)
        .where(NetworkOperation.id == plan.operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if operation is None or operation.status not in _OPERATION_ACTIVE_STATES:
        raise _error(
            "execution_conflict",
            "Commissioning operation changed while management configuration was running.",
        )
    _transition(
        db,
        intent,
        OntCommissioningState.awaiting_acs,
        actor=command.context.actor,
    )
    network_operations.merge_output_payload(
        db,
        str(plan.operation_id),
        {
            "success": True,
            "waiting": True,
            "intent_id": str(intent.id),
            "ont_unit_id": str(intent.ont_unit_id),
            "management_only": True,
            "management_steps": list(completed_steps),
            "management_recovery": plan.management_recovery,
        },
    )
    network_operations.mark_waiting(
        db,
        str(plan.operation_id),
        "Management path applied; waiting for the commissioned ONT to inform ACS.",
    )
    dispatch = stage_commissioning_verification(db, operation, attempt=0)
    outcome = OntCommissioningExecutionOutcome(
        success=True,
        waiting=True,
        intent_id=plan.intent_id,
        ont_unit_id=plan.ont_unit_id,
        operation_id=plan.operation_id,
        verification_dispatch_id=dispatch.id,
        state=OntCommissioningState.awaiting_acs,
        management_steps=completed_steps,
        management_recovery=plan.management_recovery,
        message="Management path applied; waiting for ACS.",
    )
    db.commit()
    return outcome


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
        operation_uuid = _uuid(operation_id, "operation_id")
        return _fail_execution(
            db,
            command=ExecuteOntCommissioning(
                context=CommandContext.system(
                    actor="ont_commissioning_verifier",
                    scope="network:ont:commission",
                    reason="record commissioning verification failure",
                    command_id=operation_uuid,
                    correlation_id=operation_uuid,
                    causation_id=_uuid(intent_id, "intent_id"),
                ),
                intent_id=_uuid(intent_id, "intent_id"),
                operation_id=operation_uuid,
            ),
            code="inventory_missing",
            message="The commissioned ONT inventory row is missing.",
        ).to_transport()
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


def _landed_authorization_evidence(
    intent: OntCommissioningIntent,
    operation: NetworkOperation,
) -> _LandedAuthorizationEvidence | None:
    """Normalize and validate durable landed-write evidence from the ledger."""

    payload = operation.output_payload
    if (
        not isinstance(payload, dict)
        or payload.get("completed_authorization") is not True
    ):
        return None
    device = payload.get("device_authorization")
    if not isinstance(device, dict):
        return None
    raw_ont_id = device.get("ont_id_on_olt")
    if isinstance(raw_ont_id, bool) or not isinstance(raw_ont_id, (int, str)):
        return None
    try:
        target = OntAuthorizationTarget.from_transport(
            olt_id=str(device.get("olt_id") or ""),
            fsp=str(device.get("fsp") or ""),
            serial_number=str(device.get("serial_number") or ""),
        )
        ont_id_on_olt = int(raw_ont_id)
    except (DomainError, TypeError, ValueError):
        return None
    if (
        target.olt_id != intent.olt_id
        or target.fsp.value != intent.fsp
        or target.serial_number.value != intent.canonical_serial
        or ont_id_on_olt < 0
    ):
        return None
    return _LandedAuthorizationEvidence(
        source_operation_id=operation.id,
        target=target,
        ont_id_on_olt=ont_id_on_olt,
    )


def _commissioning_recovery_head(
    intent: OntCommissioningIntent,
    operation: NetworkOperation,
    evidence: _LandedAuthorizationEvidence,
) -> str:
    """Fingerprint the locked state that makes management replay safe."""

    payload = {
        "contract_version": 1,
        "intent_id": str(intent.id),
        "operation_id": str(operation.id),
        "ont_unit_id": str(intent.ont_unit_id),
        "olt_id": str(evidence.target.olt_id),
        "fsp": evidence.target.fsp.value,
        "serial_number": evidence.target.serial_number.value,
        "ont_id_on_olt": evidence.ont_id_on_olt,
        "device_authorized_at": intent.device_authorized_at,
        "operation_status": operation.status.value,
        "retry_count": int(operation.retry_count or 0),
        "max_retries": int(operation.max_retries or 0),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stage_interrupted_management_recovery(
    db: Session,
    *,
    intent: OntCommissioningIntent,
    source: NetworkOperation,
    evidence: _LandedAuthorizationEvidence,
    context: CommandContext,
) -> bool:
    """Stage one bounded idempotent replay without reissuing authorization."""

    device_authorized_at = intent.device_authorized_at
    if device_authorized_at is None:
        raise _error(
            "interrupted_execution_review_required",
            "Management recovery requires durable landed-authorization evidence.",
        )
    reviewed_head = _commissioning_recovery_head(intent, source, evidence)
    recovery, replayed = network_operations.start_redrive(
        db,
        source,
        correlation_key=f"ont_commission_recovery:{intent.id}:{source.id}",
        input_payload={
            **(source.input_payload or {}),
            "intent_id": str(intent.id),
            "authorization_already_recorded": True,
            "recovery": {
                "source_operation_id": str(source.id),
                "reviewed_head": reviewed_head,
                "device_authorized_at": device_authorized_at.isoformat(),
                "authorization_reissue_allowed": False,
            },
        },
        reason="resume management after recorded OLT authorization",
        reviewed_head=reviewed_head,
        idempotency_key=f"commissioning-partial-success:{intent.id}:{source.id}",
        initiated_by=context.actor,
    )
    if not replayed:
        network_operations.merge_output_payload(
            db,
            str(recovery.id),
            evidence.to_output_fragment(),
        )
        stage_dispatch(
            db,
            recovery,
            NetworkOperationCommand.ont_commission_v1,
        )
    intent.latest_operation_id = recovery.id
    _transition(
        db,
        intent,
        OntCommissioningState.authorizing,
        actor=context.actor,
    )
    stage_audit_event(
        db,
        action="network.ont_commissioning.recovery_staged",
        entity_type="ont_commissioning_intent",
        entity_id=str(intent.id),
        actor_type=AuditActorType.system,
        actor_id=context.actor,
        metadata={
            "source_operation_id": str(source.id),
            "recovery_operation_id": str(recovery.id),
            "retry_count": int(recovery.retry_count or 0),
            "authorization_reissue_allowed": False,
        },
    )
    return not replayed


def _fail_interrupted_execution(
    db: Session,
    *,
    intent: OntCommissioningIntent,
    context: CommandContext,
    code: str,
    message: str,
) -> None:
    _transition(
        db,
        intent,
        OntCommissioningState.failed,
        actor=context.actor,
        failure_code=code,
        failure_message=message,
    )


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
    assigned = provisioned = 0
    recovery_staged = recovery_failed_closed = 0
    cleanup_staged = expired_without_write = 0
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
        prior = (
            db.scalars(
                select(NetworkOperation)
                .where(NetworkOperation.id == intent.latest_operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if intent.latest_operation_id
            else None
        )
        if (
            _aware_utc(intent.expires_at) > now
            and prior is not None
            and prior.status is NetworkOperationStatus.failed
            and (
                intent.state is OntCommissioningState.authorizing
                or intent.failure_code == "external_write_reconciliation_required"
            )
        ):
            from app.services.network.parsers import normalize_fsp
            from app.services.network.serial_utils import parse_ont_id_on_olt

            unknown_delivery = any(
                dispatch.status is NetworkOperationDispatchStatus.reconciliation_needed
                for dispatch in prior.dispatches
            )
            explicitly_recorded = (
                intent.failure_code == "external_write_reconciliation_required"
            )
            evidence = _landed_authorization_evidence(intent, prior)
            if (
                not (unknown_delivery or explicitly_recorded)
                or intent.device_authorized_at is None
                or intent.ont_unit_id is None
                or evidence is None
            ):
                _fail_interrupted_execution(
                    db,
                    intent=intent,
                    context=context,
                    code="interrupted_execution_review_required",
                    message=(
                        "Commissioning stopped with unknown delivery and lacks the "
                        "complete durable evidence required for automatic recovery."
                    ),
                )
                recovery_failed_closed += 1
                continue
            ont = db.get(OntUnit, intent.ont_unit_id)
            current_fsp = (
                normalize_fsp(f"{ont.board}/{ont.port}")
                if ont is not None and ont.board is not None and ont.port is not None
                else None
            )
            projected_ont_id = (
                parse_ont_id_on_olt(ont.external_id) if ont is not None else None
            )
            if (
                ont is None
                or canonical_serial(ont.serial_number) != intent.canonical_serial
                or ont.olt_device_id != intent.olt_id
                or current_fsp != intent.fsp
                or projected_ont_id != evidence.ont_id_on_olt
            ):
                _transition(
                    db,
                    intent,
                    OntCommissioningState.cleanup_pending,
                    actor=context.actor,
                    failure_code="cleanup_identity_mismatch",
                    failure_message=(
                        "Recorded authorization evidence conflicts with current "
                        "ONT inventory; automatic recovery stopped."
                    ),
                )
                recovery_failed_closed += 1
                continue
            if int(prior.retry_count or 0) >= int(prior.max_retries or 0):
                _fail_interrupted_execution(
                    db,
                    intent=intent,
                    context=context,
                    code="management_recovery_exhausted",
                    message=(
                        "Commissioning management recovery reached its retry limit; "
                        "the landed authorization requires operator review."
                    ),
                )
                recovery_failed_closed += 1
                continue
            if _stage_interrupted_management_recovery(
                db,
                intent=intent,
                source=prior,
                evidence=evidence,
                context=context,
            ):
                recovery_staged += 1
            continue
        if (
            intent.state is OntCommissioningState.authorizing
            and prior is None
            and _aware_utc(intent.expires_at) > now
        ):
            _fail_interrupted_execution(
                db,
                intent=intent,
                context=context,
                code="operation_missing",
                message="The authorizing intent has no durable operation to reconcile.",
            )
            recovery_failed_closed += 1
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
        recovery_staged=recovery_staged,
        recovery_failed_closed=recovery_failed_closed,
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
    from app.services.network.parsers import normalize_fsp

    current_fsp = (
        normalize_fsp(f"{ont.board}/{ont.port}")
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
    cleanup_ont_id = ont.id
    db.commit()

    from app.services.network.ont_inventory import return_ont_to_inventory

    result = return_ont_to_inventory(db, str(cleanup_ont_id))
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
            "ont_unit_id": str(cleanup_ont_id),
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
    from app.services.network.parsers import normalize_fsp

    candidates = db.scalars(
        select(OltAutofindCandidate)
        .where(
            OltAutofindCandidate.olt_id == ont.olt_device_id,
            OltAutofindCandidate.is_active.is_(True),
        )
        .order_by(OltAutofindCandidate.last_seen_at.desc())
    ).all()
    expected_fsp = (
        normalize_fsp(f"{ont.board}/{ont.port}")
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
