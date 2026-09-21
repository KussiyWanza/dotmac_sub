"""Bearer-authenticated field attendance adapters.

Dotmac ERP remains the sole attendance owner. These routes authenticate the
field technician, validate the mobile observation, and translate the existing
provider-neutral attendance contract into JSON for the native client.
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.principals import require_field_principal
from app.schemas.workforce_attendance import (
    FieldAttendanceLocation,
    FieldAttendanceRead,
)
from app.services.audit_adapter import AuditActor, record_audit_event
from app.services.rate_limiter_adapter import allow_operation
from app.services.workforce_attendance import (
    AttendanceAction,
    AttendancePunchResolution,
    AttendanceView,
    BrowserLocation,
    WorkforceAttendanceError,
    WorkforceAttendanceService,
)

router = APIRouter(prefix="/attendance", tags=["field-attendance"])
logger = logging.getLogger(__name__)


def _subject(principal: dict) -> UUID:
    if principal.get("principal_type") != "system_user":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "authorization_failed",
                "message": "Attendance requires a technician staff account.",
            },
        )
    try:
        return UUID(str(principal["principal_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "authorization_failed",
                "message": "Attendance is not available for this account.",
            },
        ) from exc


def _request_id(request: Request, fallback: str) -> str:
    return str(getattr(request.state, "request_id", fallback))[:160]


def _resolution_value(
    resolution: AttendancePunchResolution,
) -> Literal["direct", "reconciled"]:
    if resolution is AttendancePunchResolution.DIRECT:
        return "direct"
    return "reconciled"


def _response(
    view: AttendanceView,
    *,
    resolution: AttendancePunchResolution | None = None,
) -> FieldAttendanceRead:
    return FieldAttendanceRead(
        state=view.state.value,
        attendance_date=view.attendance_date,
        timezone=view.timezone,
        check_in_at=view.check_in_at,
        check_out_at=view.check_out_at,
        working_hours=view.working_hours,
        status=view.status,
        allowed_actions=tuple(action.value for action in view.allowed_actions),
        reason=view.reason,
        resolution=_resolution_value(resolution) if resolution is not None else None,
    )


def _http_error(exc: WorkforceAttendanceError) -> HTTPException:
    status_code = 503 if exc.unavailable else 409
    if exc.code in {"invalid_location", "location_required", "outside_geofence"}:
        status_code = 422
    if exc.code == "authorization_failed":
        status_code = 403
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message},
    )


def _audit(
    request: Request,
    db: Session,
    *,
    subject: UUID,
    action: AttendanceAction,
    outcome: str,
    accuracy_m: float | None,
    success: bool,
) -> None:
    try:
        record_audit_event(
            db=db,
            action=f"attendance_{action.value}",
            entity_type="workforce_attendance_transport",
            entity_id=str(subject),
            actor=AuditActor.user(str(subject)),
            metadata={
                "source": "FIELD_MOBILE",
                "outcome": outcome,
                "location_accuracy_m": accuracy_m,
            },
            status_code=200 if success else 400,
            is_success=success,
            request_id=_request_id(request, "") or None,
            defer_until_commit=False,
        )
    except Exception:
        # ERP remains authoritative; audit availability cannot reinterpret a
        # confirmed provider result at this transport boundary.
        logger.warning("Field attendance audit failed", exc_info=True)


@router.get("", response_model=FieldAttendanceRead)
def attendance_today(
    request: Request,
    principal: dict = Depends(require_field_principal),
    db: Session = Depends(get_db),
) -> FieldAttendanceRead:
    try:
        view = WorkforceAttendanceService(db).today(
            subject=_subject(principal),
            request_id=_request_id(request, "field-attendance-read"),
        )
    except WorkforceAttendanceError as exc:
        raise _http_error(exc) from exc
    return _response(view)


def _punch(
    request: Request,
    db: Session,
    principal: dict,
    payload: FieldAttendanceLocation,
    *,
    action: AttendanceAction,
    idempotency_key: str,
) -> FieldAttendanceRead:
    subject = _subject(principal)
    decision = allow_operation(
        f"field-attendance:{subject}", limit=12, window_seconds=60
    )
    if not decision.allowed:
        exc = WorkforceAttendanceError(
            "attendance_rate_limited",
            "Too many attendance attempts. Please try again shortly.",
        )
        _audit(
            request,
            db,
            subject=subject,
            action=action,
            outcome=exc.code,
            accuracy_m=payload.accuracy_m,
            success=False,
        )
        raise HTTPException(
            status_code=429,
            detail={"code": exc.code, "message": exc.message},
        )
    try:
        outcome = WorkforceAttendanceService(db).punch_confirmed(
            action=action,
            subject=subject,
            location=BrowserLocation(
                latitude=payload.latitude,
                longitude=payload.longitude,
                accuracy_m=payload.accuracy_m,
                observed_at=payload.observed_at,
            ),
            idempotency_key=idempotency_key,
            request_id=_request_id(request, idempotency_key),
        )
    except WorkforceAttendanceError as exc:
        _audit(
            request,
            db,
            subject=subject,
            action=action,
            outcome=exc.code,
            accuracy_m=payload.accuracy_m,
            success=False,
        )
        raise _http_error(exc) from exc
    _audit(
        request,
        db,
        subject=subject,
        action=action,
        outcome=outcome.resolution.value,
        accuracy_m=payload.accuracy_m,
        success=True,
    )
    return _response(
        outcome.attendance,
        resolution=outcome.resolution,
    )


@router.post("/check-in", response_model=FieldAttendanceRead)
def attendance_check_in(
    request: Request,
    payload: FieldAttendanceLocation,
    idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=200),
    principal: dict = Depends(require_field_principal),
    db: Session = Depends(get_db),
) -> FieldAttendanceRead:
    return _punch(
        request,
        db,
        principal,
        payload,
        action=AttendanceAction.CHECK_IN,
        idempotency_key=idempotency_key,
    )


@router.post("/check-out", response_model=FieldAttendanceRead)
def attendance_check_out(
    request: Request,
    payload: FieldAttendanceLocation,
    idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=200),
    principal: dict = Depends(require_field_principal),
    db: Session = Depends(get_db),
) -> FieldAttendanceRead:
    return _punch(
        request,
        db,
        principal,
        payload,
        action=AttendanceAction.CHECK_OUT,
        idempotency_key=idempotency_key,
    )
