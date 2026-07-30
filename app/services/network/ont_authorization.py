"""ONT authorization service - OLT serial registration with DB state tracking.

Authorization runs synchronously because the OLT work is OMCI/CLI-driven. The
workflow registers the autofind serial and persists local inventory state before
returning. Follow-up service configuration is applied explicitly after
authorization.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.network import (
    OntAuthorizationStatus,
    OntProvisioningStatus,
    OntUnit,
    OnuOnlineStatus,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationType,
)
from app.services.network._common import normalize_mac_address
from app.services.network.equipment_identity import normalize_ont_equipment_id
from app.services.network.huawei_cli_response import (
    is_huawei_serial_already_registered,
    project_huawei_result_evidence,
)
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.serial_utils import (
    build_huawei_external_id,
    normalized_serial_sql,
)
from app.services.network.serial_utils import (
    normalize as normalize_serial,
)
from app.services.network.serial_utils import (
    search_candidates as serial_search_candidates,
)

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

# One canonical operator-facing headline for "the OLT accepted the command but
# the local projection did not land". Storage, audit, and rendering all reuse
# this string so a genuine device rejection can never be reported the same way.
LOCAL_INVENTORY_FAILED_HEADLINE = "OLT authorization succeeded; local inventory failed"

# Terminal operation states that can still carry a landed device authorization.
_TERMINAL_OPERATION_STATUSES = (
    NetworkOperationStatus.succeeded,
    NetworkOperationStatus.warning,
    NetworkOperationStatus.failed,
    NetworkOperationStatus.canceled,
)


@dataclass
class AuthorizationStepResult:
    """Result of one ONT authorization step."""

    step: int
    name: str
    success: bool
    message: str
    duration_ms: int = 0
    details: dict[str, object] | None = None


@dataclass
class AuthorizationWorkflowResult:
    """Compatibility result shape for OLT authorization callers."""

    success: bool
    message: str
    steps: list[AuthorizationStepResult] = field(default_factory=list)
    ont_unit_id: str | None = None
    ont_id_on_olt: int | None = None
    status: str = "error"
    completed_authorization: bool = False
    partial_success: bool = False
    #: The OLT accepted the command but the local inventory/assignment
    #: projection failed. Distinct from a device rejection and from a failed
    #: post-authorization OLT service baseline.
    local_inventory_failed: bool = False
    #: Verbatim adapter/CLI evidence for the device leg, preserved so a genuine
    #: OLT rejection is never flattened into a generic local-failure string.
    device_message: str | None = None
    #: Set when this run reused a previously landed device authorization and
    #: repaired only the local projection instead of re-issuing the command.
    device_authorization_reused_from: str | None = None
    baseline_applied: bool | None = None
    duration_ms: int = 0
    phase_timings: list[dict[str, object]] = field(default_factory=list)

    @property
    def ont_id(self) -> str | None:
        """Backward-compatible alias for callers expecting an ONT unit ID."""
        return self.ont_unit_id

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "message": self.message,
            "ont_unit_id": self.ont_unit_id,
            "ont_id_on_olt": self.ont_id_on_olt,
            "status": self.status,
            "completed_authorization": self.completed_authorization,
            "partial_success": self.partial_success,
            "local_inventory_failed": self.local_inventory_failed,
            "device_message": self.device_message,
            "device_authorization_reused_from": self.device_authorization_reused_from,
            "baseline_applied": self.baseline_applied,
            "duration_ms": self.duration_ms,
            "phase_timings": self.phase_timings,
            "steps": [
                {
                    "step": step.step,
                    "name": step.name,
                    "success": step.success,
                    "message": step.message,
                    "duration_ms": step.duration_ms,
                    **({"details": step.details} if step.details is not None else {}),
                }
                for step in self.steps
            ],
        }


def _is_serial_already_registered_message(message: str | None) -> bool:
    return is_huawei_serial_already_registered(message)


def _build_initial_ont_description(serial_number: str) -> str:
    """Default description applied at ``ont add`` time.

    The customer/service binding usually happens after authorization, so this is
    a stub that at minimum keeps the OLT row identifiable (no more
    ``ONT_NO_DESCRIPTION`` entries) and dates the authorization. Operators can
    override with ``ont modify ... desc`` later.
    """
    from datetime import UTC, datetime

    return f"{serial_number}_authd_{datetime.now(UTC).strftime('%Y%m%d')}"


def _validate_authorization_dependencies(
    db: Session,
    *,
    olt_id: str,
) -> str | None:
    """Return a blocking message when OLT profile dependencies are invalid."""
    from app.services.network.olt_dependency_preflight import (
        validate_olt_profile_dependencies,
    )

    result = validate_olt_profile_dependencies(
        db,
        olt_id=olt_id,
        operation="authorization",
    )
    if result.success:
        return None
    return result.message


def _serial_predicates(serial_number: str) -> list[str]:
    return [
        candidate
        for candidate in dict.fromkeys(
            normalize_serial(candidate)
            for candidate in serial_search_candidates(serial_number)
        )
        if candidate
    ]


def _constraint_detail(exc: IntegrityError) -> str:
    """Return the driver's constraint text so unique violations stay readable."""
    detail = str(getattr(exc, "orig", None) or exc).strip().replace("\n", " ")
    return detail[:200] if detail else "database constraint violation"


def _local_inventory_failure_message(detail: str) -> str:
    return f"{LOCAL_INVENTORY_FAILED_HEADLINE}: {detail}"


def _commit_without_expiring(db: Session) -> None:
    """Commit before slow device I/O without forcing ORM reloads afterwards."""
    previous = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = previous


# ---------------------------------------------------------------------------
# Durable device-authorization fact
#
# The OLT write is an observation: once the adapter confirms it, the fact must
# survive every later local-projection failure, rollback, or worker crash. It is
# therefore committed to the tracked operation *before* any local projection is
# attempted, and merged (never replaced) so nothing downstream can erase it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorDeviceAuthorization:
    """A previously landed OLT authorization recorded on a tracked operation."""

    operation_id: str
    ont_id_on_olt: int | None
    device_message: str | None


def record_device_authorization_landed(
    db: Session,
    operation_id: str | None,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int | None,
    device_message: str | None,
) -> None:
    """Commit ``completed_authorization`` as soon as the OLT accepts the write."""
    if not operation_id:
        return
    from app.services.network_operations import network_operations

    try:
        network_operations.merge_output_payload(
            db,
            operation_id,
            {
                "completed_authorization": True,
                "device_authorization": {
                    "olt_id": str(olt_id),
                    "fsp": fsp,
                    "serial_number": serial_number,
                    "ont_id_on_olt": ont_id_on_olt,
                    "message": device_message,
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            },
        )
        _commit_without_expiring(db)
    except SQLAlchemyError:
        db.rollback()
        logger.error(
            "Failed to persist landed ONT authorization for operation %s "
            "(olt=%s fsp=%s serial=%s)",
            operation_id,
            olt_id,
            fsp,
            serial_number,
            exc_info=True,
        )


def find_completed_device_authorization(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> PriorDeviceAuthorization | None:
    """Return the landed-but-unprojected device authorization, if any.

    Retry must never re-issue a device write that already succeeded. This
    inspects the most recent terminal authorization operation for the exact
    OLT/port/serial and reports a reusable device authorization only when that
    attempt recorded ``completed_authorization`` and then failed locally.
    """
    from app.services.network.ont_provisioning_commands import (
        ont_authorization_correlation_key,
    )

    correlation_key = ont_authorization_correlation_key(
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
    )
    operation = db.scalars(
        select(NetworkOperation)
        .where(
            NetworkOperation.operation_type.in_(
                {
                    NetworkOperationType.ont_authorize,
                    NetworkOperationType.ont_commission,
                }
            ),
            NetworkOperation.correlation_key == correlation_key,
            NetworkOperation.status.in_(_TERMINAL_OPERATION_STATUSES),
        )
        .order_by(NetworkOperation.created_at.desc(), NetworkOperation.id.desc())
        .limit(1)
    ).first()
    if operation is None:
        return None
    payload = operation.output_payload or {}
    if not isinstance(payload, dict):
        return None
    if not payload.get("completed_authorization"):
        return None
    if payload.get("success"):
        # The attempt completed end to end; nothing is left to repair locally.
        return None
    if _local_authorization_was_revoked(db, olt_id=olt_id, serial_number=serial_number):
        return None
    device_leg = payload.get("device_authorization")
    ont_id_on_olt = payload.get("ont_id_on_olt")
    if ont_id_on_olt is None and isinstance(device_leg, dict):
        ont_id_on_olt = device_leg.get("ont_id_on_olt")
    device_message = payload.get("device_message")
    if device_message is None and isinstance(device_leg, dict):
        device_message = device_leg.get("message")
    try:
        parsed_ont_id = int(ont_id_on_olt) if ont_id_on_olt is not None else None
    except (TypeError, ValueError):
        parsed_ont_id = None
    return PriorDeviceAuthorization(
        operation_id=str(operation.id),
        ont_id_on_olt=parsed_ont_id,
        device_message=str(device_message) if device_message else None,
    )


def _local_authorization_was_revoked(
    db: Session,
    *,
    olt_id: str,
    serial_number: str,
) -> bool:
    """True when an operator explicitly removed the local authorization."""
    existing = _find_ont_for_olt_serial(db, olt_id=olt_id, serial_number=serial_number)
    return existing is not None and existing.authorization_status in {
        OntAuthorizationStatus.deauthorized,
        OntAuthorizationStatus.failed,
    }


# ---------------------------------------------------------------------------
# Autofind candidate helpers
# ---------------------------------------------------------------------------


def get_autofind_candidate_by_serial(
    db: Session,
    olt_id: str,
    serial_number: str | None,
    *,
    fsp: str | None = None,
):
    """Return the active autofind candidate matching a serial on an OLT."""
    from app.models.ont_autofind import OltAutofindCandidate

    clean_serials = {
        normalize_serial(candidate)
        for candidate in serial_search_candidates(serial_number)
    }
    candidates = db.scalars(
        select(OltAutofindCandidate).where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.is_active.is_(True),
        )
    ).all()
    clean_fsp = (fsp or "").strip()
    return next(
        (
            candidate
            for candidate in candidates
            if clean_serials.intersection(
                {
                    normalize_serial(value)
                    for serial in (candidate.serial_number, candidate.serial_hex)
                    for value in serial_search_candidates(serial)
                }
            )
            and (not clean_fsp or (candidate.fsp or "").strip() == clean_fsp)
        ),
        None,
    )


def _authorization_model_hint(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> str | None:
    """Return the best known model before writing the ONT to the OLT."""
    clean_serials = _serial_predicates(serial_number)
    existing = db.scalars(
        select(OntUnit).where(
            normalized_serial_sql(OntUnit.serial_number).in_(clean_serials),
        )
    ).first()
    if existing and getattr(existing, "model", None):
        return normalize_ont_equipment_id(existing.model)

    candidate = get_autofind_candidate_by_serial(
        db,
        olt_id,
        serial_number,
        fsp=fsp,
    )
    candidate_model = getattr(candidate, "model", None)
    if candidate_model:
        return normalize_ont_equipment_id(candidate_model)

    candidate_ont = getattr(candidate, "ont_unit", None)
    if candidate_ont and getattr(candidate_ont, "model", None):
        return normalize_ont_equipment_id(candidate_ont.model)

    return None


def _resolve_authorized_autofind_candidate(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> tuple[bool, str]:
    """Best-effort candidate cleanup after OLT authorization is verified."""
    from app.services import (
        web_network_ont_autofind as web_network_ont_autofind_service,
    )

    try:
        web_network_ont_autofind_service.resolve_candidate_authorized(
            db,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=serial_number,
        )
        return True, "Marked the discovered ONT as authorized."
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning(
            "Failed to resolve autofind candidate for %s on %s %s: %s",
            serial_number,
            olt_id,
            fsp,
            exc,
        )
        return True, "Authorization succeeded; autofind cleanup will run later."


# ---------------------------------------------------------------------------
# ONT record management
# ---------------------------------------------------------------------------


def _as_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _find_ont_for_olt_serial(
    db: Session,
    *,
    olt_id: str,
    serial_number: str,
) -> OntUnit | None:
    """Return the row this OLT owns for a serial, or an unclaimed legacy row.

    ``uq_ont_units_olt_serial_number`` scopes serial uniqueness to
    ``olt_device_id``, so the same serial on a *different* OLT is a different
    ONT and must never be reused: doing so flips that OLT's ONT to
    active/authorized and overwrites its ``external_id``. Rows that predate the
    scoped constraint can still carry a NULL ``olt_device_id``; those are
    unclaimed and are adopted by the authorizing OLT rather than duplicated.
    """
    clean_serials = _serial_predicates(serial_number)
    if not clean_serials:
        return None
    target_olt = _as_uuid(olt_id)
    rows = db.scalars(
        select(OntUnit)
        .where(normalized_serial_sql(OntUnit.serial_number).in_(clean_serials))
        .order_by(OntUnit.created_at.asc(), OntUnit.id)
    ).all()
    scoped = next(
        (row for row in rows if _as_uuid(row.olt_device_id) == target_olt),
        None,
    )
    if scoped is not None:
        return scoped
    return next((row for row in rows if row.olt_device_id is None), None)


def create_or_find_ont_for_authorized_serial(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int | None = None,
    olt_run_state: str | None = None,
) -> tuple[str | None, str]:
    """Create or find an OntUnit for a just-authorized ONT serial."""
    from app.models.ont_autofind import OltAutofindCandidate
    from app.services.network.ont_status import (
        apply_resolved_status_for_model,
        set_authorization_status,
    )

    clean_serials = _serial_predicates(serial_number)
    olt = get_olt_or_none(db, olt_id)
    if olt is None:
        # Without the owning OLT the scoped unique constraint cannot be
        # honoured, so a new row would be an unscoped duplicate.
        return None, f"OLT {olt_id} not found for local ONT inventory."
    observed_olt_status = (
        OnuOnlineStatus.online
        if str(olt_run_state or "").strip().lower() == "online"
        else None
    )
    scoped_external_id = build_huawei_external_id(fsp, ont_id_on_olt)

    existing = _find_ont_for_olt_serial(
        db,
        olt_id=str(olt.id),
        serial_number=serial_number,
    )
    if existing:
        try:
            existing.is_active = True
            # ``strict=True``: an illegal authorization transition is a real
            # local-inventory failure, not something to force through with a
            # log line the operator never sees.
            set_authorization_status(existing, OntAuthorizationStatus.authorized)
            if existing.olt_device_id is None:
                from app.services.network.ont_assignment_alignment import (
                    project_ont_topology_from_fsp_observation,
                )

                topology = project_ont_topology_from_fsp_observation(
                    db,
                    ont=existing,
                    olt_id=olt.id,
                    fsp=fsp,
                )
                if topology is None or existing.olt_device_id is None:
                    db.rollback()
                    return None, (
                        "The canonical topology owner could not adopt the legacy "
                        f"ONT record for OLT observation {fsp}."
                    )
            if ont_id_on_olt is not None:
                existing.external_id = scoped_external_id or str(ont_id_on_olt)
            if observed_olt_status is not None:
                existing.olt_status = observed_olt_status
                existing.offline_reason = None
                existing.last_seen_at = datetime.now(UTC)
                existing.last_sync_source = "olt_authorization"
                existing.last_sync_at = datetime.now(UTC)
            apply_resolved_status_for_model(existing)
            db.flush()
            return (
                str(existing.id),
                f"Using existing ONT record {existing.serial_number}.",
            )
        except ValueError as exc:
            db.rollback()
            return None, f"Existing ONT record rejected the status change: {exc}"
        except IntegrityError as exc:
            db.rollback()
            return None, (
                "Existing ONT record violates a database constraint: "
                f"{_constraint_detail(exc)}"
            )
        except SQLAlchemyError as exc:
            db.rollback()
            return None, f"Failed to update existing ONT record: {exc}"

    candidates = db.scalars(
        select(OltAutofindCandidate).where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.is_active.is_(True),
        )
    ).all()
    matched_candidate = next(
        (
            candidate
            for candidate in candidates
            if set(clean_serials).intersection(
                {
                    normalize_serial(value)
                    for serial in (candidate.serial_number, candidate.serial_hex)
                    for value in serial_search_candidates(serial)
                }
            )
        ),
        None,
    )

    display_serial = normalize_serial(serial_number)
    vendor = "Huawei" if display_serial.upper().startswith(("HWTC", "HWTT")) else None

    new_ont = OntUnit(
        id=uuid.uuid4(),
        serial_number=display_serial,
        # Populate the constraint-scoping column at creation time. Postgres
        # treats NULL != NULL, so a row created without it is invisible to
        # ``uq_ont_units_olt_serial_number`` and dedupes nothing.
        olt_device_id=olt.id,
        external_id=scoped_external_id if ont_id_on_olt is not None else None,
        vendor=vendor,
        model=getattr(matched_candidate, "model", None),
        mac_address=normalize_mac_address(getattr(matched_candidate, "mac", None)),
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
        provisioning_status=OntProvisioningStatus.unprovisioned,
        olt_status=observed_olt_status or OnuOnlineStatus.offline,
        offline_reason=None,
        last_seen_at=datetime.now(UTC) if observed_olt_status else None,
        last_sync_source="olt_authorization" if observed_olt_status else None,
        last_sync_at=datetime.now(UTC) if observed_olt_status else None,
        pon_type="gpon",
        name=display_serial,
        desired_config={},
    )
    try:
        db.add(new_ont)
        from app.services.network.ont_status import apply_resolved_status_for_model

        apply_resolved_status_for_model(new_ont)
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        return None, (
            f"ONT record violates a database constraint: {_constraint_detail(exc)}"
        )
    except SQLAlchemyError as exc:
        db.rollback()
        return None, f"Failed to create ONT record: {exc}"

    return str(new_ont.id), f"Created ONT record for {display_serial}."


def record_topology_observation_for_authorized_ont(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
) -> tuple[bool, str]:
    """Record non-conflicting PON topology without inferring an assignment."""
    from app.services.network.ont_assignment_alignment import (
        project_ont_topology_from_fsp_observation,
    )

    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        return False, "ONT record not found."

    try:
        result = project_ont_topology_from_fsp_observation(
            db,
            ont=ont,
            olt_id=olt_id,
            fsp=fsp,
        )
        if result is None:
            return False, f"Invalid OLT F/S/P: {fsp}."
        if result.review_required:
            if result.review_reason == (
                "observed PON has no exact active modeled port"
            ):
                return True, (
                    f"Recorded OLT observation {fsp} without a customer assignment; "
                    "the PON is not yet modeled and remains an explicit topology gap."
                )
            return False, (
                f"ONT topology needs reviewed identity repair: {result.review_reason}."
            )
        db.flush()
        if result.pon_port is None:
            return False, "ONT topology did not resolve to a modeled PON port."
        return True, (
            f"Recorded ONT PON port {result.pon_port.name}; customer assignment "
            "requires explicit provisioning."
        )
    except IntegrityError as exc:
        # IntegrityError subclasses SQLAlchemyError; unique/foreign-key
        # violations carry the actionable detail and must not be flattened into
        # the generic "check server logs" string.
        db.rollback()
        logger.warning(
            "Database constraint blocked PON link for ONT %s on OLT %s %s",
            ont_unit_id,
            olt_id,
            fsp,
            exc_info=True,
        )
        return False, (
            "Linking the ONT to its PON port violates a database constraint: "
            f"{_constraint_detail(exc)}"
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning(
            "Failed to link assignment/PON port for ONT %s on OLT %s %s",
            ont_unit_id,
            olt_id,
            fsp,
            exc_info=True,
        )
        message = str(exc).casefold()
        if "locknotavailable" in message or "lock timeout" in message:
            return False, (
                "The ONT was authorized, but the system was busy linking it to the "
                "PON port. Retry ONT reconcile."
            )
        return False, "Failed to link ONT to PON port. Check server logs."


# ---------------------------------------------------------------------------
# Core authorization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalProjectionResult:
    """Outcome of projecting a landed device authorization onto local state."""

    ont_unit_id: str | None
    create_message: str
    topology_message: str
    #: ``inventory`` and ``topology`` name the failed stage; ``complete`` means
    #: the whole local projection landed.
    stage: str

    @property
    def success(self) -> bool:
        return self.stage == "complete"

    @property
    def failure_detail(self) -> str:
        if self.stage == "topology":
            return self.topology_message
        return self.create_message


def project_local_authorization_state(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int | None,
) -> LocalProjectionResult:
    """Project one landed device authorization onto local inventory.

    This never touches the OLT: it is the only local-inventory/assignment
    projection path for an authorized serial, shared by first-attempt
    authorization and by projection repair.
    """
    ont_unit_id, create_msg = create_or_find_ont_for_authorized_serial(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
        ont_id_on_olt=ont_id_on_olt,
    )
    if ont_unit_id is None:
        return LocalProjectionResult(None, create_msg, "", "inventory")

    topology_ok, topology_msg = record_topology_observation_for_authorized_ont(
        db,
        ont_unit_id=ont_unit_id,
        olt_id=olt_id,
        fsp=fsp,
    )
    if not topology_ok:
        return LocalProjectionResult(ont_unit_id, create_msg, topology_msg, "topology")

    _resolve_authorized_autofind_candidate(
        db, olt_id=olt_id, fsp=fsp, serial_number=serial_number
    )
    return LocalProjectionResult(ont_unit_id, create_msg, topology_msg, "complete")


def repair_local_authorization_projection(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    prior: PriorDeviceAuthorization,
) -> AuthorizationWorkflowResult:
    """Repair local inventory for an ONT the OLT already authorized.

    No device write is issued: the authorization command landed on a previous
    attempt and re-issuing it is exactly the blind retry this path exists to
    prevent.
    """
    started_at = monotonic()
    normalized_serial = normalize_serial(serial_number)
    projection = project_local_authorization_state(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=normalized_serial,
        ont_id_on_olt=prior.ont_id_on_olt,
    )
    detail = (
        projection.failure_detail
        if not projection.success
        else (projection.topology_message or projection.create_message)
    )
    step = AuthorizationStepResult(
        step=1,
        name="Repair Local ONT Inventory",
        success=projection.success,
        message=(
            f"Reused the OLT authorization from operation {prior.operation_id}; "
            f"{detail}"
        ),
        duration_ms=max(0, int((monotonic() - started_at) * 1000)),
    )
    if not projection.success:
        return AuthorizationWorkflowResult(
            success=False,
            message=_local_inventory_failure_message(detail),
            steps=[step],
            ont_unit_id=projection.ont_unit_id,
            ont_id_on_olt=prior.ont_id_on_olt,
            status="error",
            completed_authorization=True,
            partial_success=True,
            local_inventory_failed=True,
            device_message=prior.device_message,
            device_authorization_reused_from=prior.operation_id,
            duration_ms=max(0, int((monotonic() - started_at) * 1000)),
        )
    return AuthorizationWorkflowResult(
        success=True,
        message=(
            "Local ONT inventory repaired for an ONT already authorized on the OLT."
        ),
        steps=[step],
        ont_unit_id=projection.ont_unit_id,
        ont_id_on_olt=prior.ont_id_on_olt,
        status="success",
        completed_authorization=True,
        device_message=prior.device_message,
        device_authorization_reused_from=prior.operation_id,
        duration_ms=max(0, int((monotonic() - started_at) * 1000)),
    )


def authorize_autofind_ont(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
    operation_id: str | None = None,
    allow_registration_move: bool = True,
) -> AuthorizationWorkflowResult:
    """Authorize an ONT on an OLT and persist ONT inventory state."""
    from app.services.network.olt_profile_resolution import (
        AuthorizationProfileResolution,
        resolve_authorization_profiles_from_import,
    )
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.olt_write_reconciliation import verify_ont_absent

    steps: list[AuthorizationStepResult] = []
    started_at = monotonic()

    def add_step(
        name: str,
        success: bool,
        message: str,
        step_started: float,
        *,
        adapter_result: object | None = None,
    ) -> None:
        details = project_huawei_result_evidence(adapter_result)
        steps.append(
            AuthorizationStepResult(
                step=len(steps) + 1,
                name=name,
                success=success,
                message=message,
                duration_ms=max(0, int((monotonic() - step_started) * 1000)),
                details=details,
            )
        )

    def finish(
        *,
        success: bool,
        message: str,
        status: str,
        ont_unit_id: str | None = None,
        ont_id_on_olt: int | None = None,
        completed_authorization: bool = False,
        partial_success: bool = False,
        local_inventory_failed: bool = False,
        device_message: str | None = None,
    ) -> AuthorizationWorkflowResult:
        return AuthorizationWorkflowResult(
            success=success,
            message=message,
            steps=steps,
            ont_unit_id=ont_unit_id,
            ont_id_on_olt=ont_id_on_olt,
            status=status,
            completed_authorization=completed_authorization,
            partial_success=partial_success,
            local_inventory_failed=local_inventory_failed,
            device_message=device_message,
            duration_ms=max(0, int((monotonic() - started_at) * 1000)),
        )

    olt = get_olt_or_none(db, olt_id)
    if olt is None:
        return finish(success=False, message="OLT not found", status="error")

    normalized_serial = normalize_serial(serial_number)

    # Never blind-retry a device write that already landed. A forced
    # reauthorization is an explicit operator decision to re-issue and is the
    # only way past this gate.
    if not force_reauthorize:
        # The correlation key is built from the identifiers the command owner
        # received, so look it up with the same raw ``olt_id`` argument.
        prior = find_completed_device_authorization(
            db,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=serial_number,
        )
        if prior is not None:
            logger.info(
                "Repairing local ONT inventory instead of re-issuing authorization "
                "(olt=%s fsp=%s serial=%s prior_operation=%s)",
                olt_id,
                fsp,
                normalized_serial,
                prior.operation_id,
            )
            # Carry the landed device fact onto this attempt first, so this
            # operation is self-describing even if the repair fails again.
            record_device_authorization_landed(
                db,
                operation_id,
                olt_id=str(olt.id),
                fsp=fsp,
                serial_number=normalized_serial,
                ont_id_on_olt=prior.ont_id_on_olt,
                device_message=prior.device_message,
            )
            return repair_local_authorization_projection(
                db,
                olt_id=str(olt.id),
                fsp=fsp,
                serial_number=normalized_serial,
                prior=prior,
            )

    dependency_error = _validate_authorization_dependencies(db, olt_id=str(olt.id))
    if dependency_error is not None:
        add_step(
            "Validate OLT Profile Dependencies", False, dependency_error, started_at
        )
        return finish(success=False, message=dependency_error, status="error")

    adapter = get_protocol_adapter(olt)
    _commit_without_expiring(db)

    # Handle force reauthorize - remove existing registration first
    if force_reauthorize:
        force_started = monotonic()
        find_result = adapter.find_ont_by_serial(normalized_serial)
        existing = find_result.data.get("registration") if find_result.success else None
        if not find_result.success:
            add_step(
                "Activate ONT",
                False,
                find_result.message,
                force_started,
                adapter_result=find_result,
            )
            return finish(success=False, message=find_result.message, status="error")
        if existing:
            delete_result = adapter.deauthorize_ont(existing.fsp, existing.onu_id)
            if not delete_result.success:
                add_step(
                    "Activate ONT",
                    False,
                    delete_result.message,
                    force_started,
                    adapter_result=delete_result,
                )
                return finish(
                    success=False, message=delete_result.message, status="error"
                )
            absence = verify_ont_absent(
                olt,
                fsp=existing.fsp,
                ont_id=existing.onu_id,
                serial_number=normalized_serial,
            )
            if not absence.success:
                add_step("Activate ONT", False, absence.message, force_started)
                return finish(success=False, message=absence.message, status="error")

    # Resolve authorization profiles
    activation_started = monotonic()
    authorization_profiles: AuthorizationProfileResolution | None = None

    model_hint = _authorization_model_hint(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=normalized_serial,
    )

    if authorization_profiles is None:
        profiles_ok, profiles_msg, authorization_profiles = (
            resolve_authorization_profiles_from_import(
                db,
                olt,
                equipment_id=model_hint,
            )
        )
        if not profiles_ok or authorization_profiles is None:
            add_step("Activate ONT", False, profiles_msg, activation_started)
            return finish(success=False, message=profiles_msg, status="error")

    # Authorize on OLT
    _commit_without_expiring(db)
    auth_description = _build_initial_ont_description(normalized_serial)
    auth_result = adapter.authorize_ont(
        fsp,
        normalized_serial,
        line_profile_id=authorization_profiles.line_profile_id,
        service_profile_id=authorization_profiles.service_profile_id,
        description=auth_description,
    )
    ont_id = auth_result.ont_id

    # Handle "serial already exists" case
    if not auth_result.success or ont_id is None:
        if _is_serial_already_registered_message(auth_result.message):
            find_result = adapter.find_ont_by_serial(normalized_serial)
            existing = (
                find_result.data.get("registration") if find_result.success else None
            )
            if (
                existing is not None
                and str(getattr(existing, "fsp", "")).strip() == fsp
            ):
                # Already on this port, reuse
                raw_ont_id = getattr(existing, "onu_id", None)
                ont_id = int(raw_ont_id) if raw_ont_id is not None else None
                add_step(
                    "Activate ONT",
                    True,
                    "ONT serial was already registered on the OLT; reusing registration.",
                    activation_started,
                    adapter_result=auth_result,
                )
            else:
                # Moving a registration is a separate, destructive topology
                # decision. Temporary commissioning always disables it.
                if not find_result.success or existing is None:
                    msg = "ONT serial already exists, but existing registration not found."
                    add_step("Activate ONT", False, msg, activation_started)
                    return finish(success=False, message=msg, status="error")
                if not allow_registration_move:
                    msg = (
                        "ONT serial is already registered on "
                        f"{existing.fsp}; commissioning will not move it to {fsp}."
                    )
                    add_step("Activate ONT", False, msg, activation_started)
                    return finish(success=False, message=msg, status="error")

                delete_result = adapter.deauthorize_ont(existing.fsp, existing.onu_id)
                if not delete_result.success:
                    add_step(
                        "Activate ONT",
                        False,
                        delete_result.message,
                        activation_started,
                        adapter_result=delete_result,
                    )
                    return finish(
                        success=False, message=delete_result.message, status="error"
                    )

                absence = verify_ont_absent(
                    olt,
                    fsp=existing.fsp,
                    ont_id=existing.onu_id,
                    serial_number=normalized_serial,
                )
                if not absence.success:
                    add_step("Activate ONT", False, absence.message, activation_started)
                    return finish(
                        success=False, message=absence.message, status="error"
                    )

                auth_result = adapter.authorize_ont(
                    fsp,
                    normalized_serial,
                    line_profile_id=authorization_profiles.line_profile_id,
                    service_profile_id=authorization_profiles.service_profile_id,
                    description=auth_description,
                )
                ont_id = auth_result.ont_id
                if not auth_result.success or ont_id is None:
                    msg = f"Removed old registration, but authorization failed: {auth_result.message}"
                    add_step(
                        "Activate ONT",
                        False,
                        msg,
                        activation_started,
                        adapter_result=auth_result,
                    )
                    return finish(success=False, message=msg, status="error")
                auth_result.message = (
                    f"Removed existing ONT registration on {existing.fsp}; "
                    f"authorized on {fsp}."
                )
        else:
            # A genuine device rejection. ``olt_ssh_ont.lifecycle`` preserves the
            # last 200 characters of real CLI output here; keep it verbatim and
            # never mark the authorization as completed.
            message = auth_result.message or "Authorization failed"
            add_step(
                "Activate ONT",
                False,
                message,
                activation_started,
                adapter_result=auth_result,
            )
            return finish(
                success=False,
                message=message,
                status="error",
                device_message=message,
            )

    device_message = auth_result.message

    # The OLT accepted the write. Commit that fact before any local projection
    # is attempted so a later rollback, constraint violation, or worker crash
    # cannot leave the ledger claiming the device was never touched.
    record_device_authorization_landed(
        db,
        operation_id,
        olt_id=str(olt.id),
        fsp=fsp,
        serial_number=normalized_serial,
        ont_id_on_olt=ont_id,
        device_message=device_message,
    )

    projection = project_local_authorization_state(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=normalized_serial,
        ont_id_on_olt=ont_id,
    )
    if not projection.success:
        detail = projection.failure_detail
        add_step(
            (
                "Link ONT Assignment"
                if projection.stage == "topology"
                else "Create Local ONT Record"
            ),
            False,
            detail,
            activation_started,
        )
        return finish(
            success=False,
            message=_local_inventory_failure_message(detail),
            status="error",
            ont_unit_id=projection.ont_unit_id,
            ont_id_on_olt=ont_id,
            completed_authorization=True,
            partial_success=True,
            local_inventory_failed=True,
            device_message=device_message,
        )

    activation_message = (
        f"{getattr(authorization_profiles, 'message', '')} "
        f"{auth_result.message} {projection.create_message} "
        f"{projection.topology_message}".strip()
    )
    add_step(
        "Activate ONT",
        True,
        activation_message,
        activation_started,
        adapter_result=auth_result,
    )

    return finish(
        success=True,
        message="ONT authorization completed.",
        status="success",
        ont_unit_id=projection.ont_unit_id,
        ont_id_on_olt=ont_id,
        completed_authorization=True,
        device_message=device_message,
    )


def authorize_ont(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
    request: Request | None = None,
    provision: bool = True,
    operation_id: str | None = None,
    allow_registration_move: bool = True,
) -> AuthorizationWorkflowResult:
    """Authorize ONT on the OLT, apply the OLT baseline, and audit log.

    This is the main entry point for ONT authorization. It:
    1. Registers the ONT serial on the OLT (line/service profiles)
    2. Creates/updates the OntUnit record
    3. Applies OLT-side internet and ACS reachability config
    4. Logs the action for audit

    After TR-069 binding, the ONT reboots and connects to ACS automatically.

    Args:
        db: Database session.
        olt_id: OLT device ID.
        fsp: Frame/Slot/Port (e.g., "0/1/0").
        serial_number: ONT serial number.
        force_reauthorize: Remove existing registration before authorizing.
        preset_id: Optional preset ID (unused, kept for compatibility).
        request: Optional request for audit logging.
        provision: If True, apply OLT baseline after authorization (default True).
        operation_id: Tracked operation to record the landed device
            authorization against, so a later reader can tell "OLT authorized,
            local projection failed" from "OLT rejected the command".
    """
    from app.services.network.ont_provision_steps import apply_authorization_baseline

    started_at = monotonic()
    phase_timings: list[dict[str, object]] = []

    def record_phase(name: str, phase_started: float, **details: object) -> None:
        phase_timings.append(
            {
                "phase": name,
                "duration_ms": max(0, int((monotonic() - phase_started) * 1000)),
                **details,
            }
        )

    # Step 1: Core OLT authorization (register serial, create record, link PON)
    phase_started = monotonic()
    result = authorize_autofind_ont(
        db,
        olt_id,
        fsp,
        serial_number,
        force_reauthorize=force_reauthorize,
        preset_id=preset_id,
        operation_id=operation_id,
        allow_registration_move=allow_registration_move,
    )
    record_phase("core_authorization", phase_started, success=result.success)
    result.phase_timings = phase_timings

    if not result.success:
        phase_started = monotonic()
        _audit_authorization(
            db, request, olt_id, fsp, serial_number, force_reauthorize, result
        )
        record_phase("audit", phase_started)
        result.duration_ms = max(0, int((monotonic() - started_at) * 1000))
        return result

    phase_started = monotonic()
    db.commit()
    record_phase("post_authorization_commit", phase_started)

    # Step 2: Apply OLT baseline (internet service port + ACS reachability)
    if provision and result.ont_unit_id:
        phase_started = monotonic()
        provision_result = apply_authorization_baseline(db, result.ont_unit_id)
        provision_data = provision_result.data or {}
        record_phase(
            "authorization_baseline",
            phase_started,
            success=provision_result.success,
            subphases=provision_data.get("phase_timings", []),
        )
        step_details: dict[str, object] = {
            key: provision_data[key]
            for key in ("phase_timings", "command_timings", "domain_outcomes")
            if key in provision_data
        }
        if provision_result.success:
            result.baseline_applied = True
            result.steps.append(
                AuthorizationStepResult(
                    step=len(result.steps) + 1,
                    name="Apply Authorization Baseline",
                    success=True,
                    message=provision_result.message,
                    duration_ms=provision_result.duration_ms,
                    details=step_details,
                )
            )
            phase_started = monotonic()
            db.commit()
            record_phase("post_baseline_commit", phase_started)
        else:
            # Provisioning failed but authorization succeeded - partial success
            result.baseline_applied = False
            result.steps.append(
                AuthorizationStepResult(
                    step=len(result.steps) + 1,
                    name="Apply Authorization Baseline",
                    success=False,
                    message=provision_result.message,
                    duration_ms=provision_result.duration_ms,
                    details=step_details,
                )
            )
            result.status = "warning"
            result.partial_success = True
            result.message = (
                "ONT authorized, but OLT service baseline failed: "
                f"{provision_result.message}"
            )

    phase_started = monotonic()
    _audit_authorization(
        db, request, olt_id, fsp, serial_number, force_reauthorize, result
    )
    record_phase("audit", phase_started)
    result.phase_timings = phase_timings
    result.duration_ms = max(0, int((monotonic() - started_at) * 1000))
    return result


def _audit_authorization(
    db: Session,
    request: Request | None,
    olt_id: str,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool,
    result: AuthorizationWorkflowResult,
) -> None:
    """Log authorization action for audit trail."""
    from app.services.network.action_logging import log_network_action_result

    status = (
        "success"
        if result.success
        else ("warning" if result.partial_success else "error")
    )
    log_olt_audit_event(
        db,
        request=request,
        action="force_authorize_ont" if force_reauthorize else "authorize_ont",
        entity_id=olt_id,
        metadata={
            "result": status,
            "message": result.message,
            "fsp": fsp,
            "serial_number": serial_number,
            "force_reauthorize": force_reauthorize,
            "completed_authorization": result.completed_authorization,
            "local_inventory_failed": result.local_inventory_failed,
            "device_authorization_reused_from": (
                result.device_authorization_reused_from
            ),
        },
        status_code=200 if result.success or result.partial_success else 500,
        is_success=result.success,
    )
    log_network_action_result(
        request=request,
        resource_type="olt",
        resource_id=olt_id,
        action="Force Authorize ONT" if force_reauthorize else "Authorize ONT",
        success=result.success,
        message=result.message,
        metadata={
            "fsp": fsp,
            "serial_number": serial_number,
            "force_reauthorize": force_reauthorize,
        },
    )
