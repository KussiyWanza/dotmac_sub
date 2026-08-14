"""User-specific Selfcare dashboard attendance widget service."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.models.system_user import SystemUser
from app.schemas.workforce_attendance import DashboardAttendanceLocation
from app.services.audit_adapter import record_audit_event
from app.services.rate_limiter_adapter import allow_operation
from app.services.workforce_attendance import (
    AttendanceAction,
    AttendanceState,
    AttendanceView,
    BrowserLocation,
    WorkforceAttendanceError,
    WorkforceAttendanceService,
)

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")


def _intended_state(action: AttendanceAction) -> AttendanceState:
    return (
        AttendanceState.CHECKED_IN
        if action == AttendanceAction.CHECK_IN
        else AttendanceState.CHECKED_OUT
    )


def _reconcile_punch(
    db: Session, *, subject: UUID, request_id: str
) -> AttendanceView | None:
    """Read ERP after an ambiguous punch without inferring a local result."""
    try:
        return WorkforceAttendanceService(db).today(subject, request_id=request_id)
    except WorkforceAttendanceError:
        return None


def _subject(request: Request) -> UUID | None:
    auth = getattr(request.state, "auth", None)
    user = getattr(request.state, "user", None)
    if not isinstance(auth, dict) or auth.get("principal_type") != "system_user":
        return None
    if not isinstance(user, SystemUser) or not user.is_active:
        return None
    try:
        principal_id = UUID(str(auth.get("principal_id")))
    except (TypeError, ValueError):
        return None
    return principal_id if principal_id == user.id else None


def _render(
    request: Request,
    *,
    attendance: AttendanceView | None = None,
    error_message: str | None = None,
    unavailable: bool = False,
):
    return templates.TemplateResponse(
        "admin/dashboard/_attendance.html",
        {
            "request": request,
            "attendance": attendance,
            "attendance_state": AttendanceState,
            "error_message": error_message,
            "attendance_unavailable": unavailable,
        },
    )


def load(request: Request, db: Session):
    subject = _subject(request)
    if subject is None:
        return _render(
            request, error_message="Attendance is not available for this account."
        )
    try:
        attendance = WorkforceAttendanceService(db).today(
            subject,
            request_id=str(getattr(request.state, "request_id", "attendance-read")),
        )
    except WorkforceAttendanceError as exc:
        return _render(request, error_message=exc.message, unavailable=exc.unavailable)
    except Exception:
        logger.exception("Dashboard attendance read failed")
        return _render(
            request,
            error_message="Attendance is temporarily unavailable. Please try again.",
            unavailable=True,
        )
    return _render(request, attendance=attendance)


def punch(
    request: Request,
    db: Session,
    *,
    action: AttendanceAction,
    payload: DashboardAttendanceLocation,
    idempotency_key: str,
):
    subject = _subject(request)
    if subject is None:
        return _render(
            request, error_message="Attendance is not available for this account."
        )
    decision = allow_operation(
        f"dashboard-attendance:{subject}", limit=12, window_seconds=60
    )
    if not decision.allowed:
        return _render(
            request,
            error_message="Too many attendance attempts. Please try again shortly.",
        )

    request_id = str(getattr(request.state, "request_id", idempotency_key))
    try:
        attendance = WorkforceAttendanceService(db).punch(
            action,
            subject,
            BrowserLocation(
                latitude=payload.latitude,
                longitude=payload.longitude,
                accuracy_m=payload.accuracy_m,
                observed_at=payload.observed_at,
            ),
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    except WorkforceAttendanceError as exc:
        duplicate_for_action = {
            AttendanceAction.CHECK_IN: "already_checked_in",
            AttendanceAction.CHECK_OUT: "already_checked_out",
        }[action]
        is_uncertain_transport = exc.code == "attendance_unavailable"
        if exc.code == duplicate_for_action or is_uncertain_transport:
            attendance = _reconcile_punch(db, subject=subject, request_id=request_id)
            if attendance is not None and attendance.state == _intended_state(action):
                _audit(
                    request,
                    db,
                    subject,
                    action,
                    "reconciled_success",
                    payload.accuracy_m,
                    True,
                )
                return _render(request, attendance=attendance)
            if is_uncertain_transport and attendance is not None:
                _audit(
                    request,
                    db,
                    subject,
                    action,
                    "attendance_unconfirmed",
                    payload.accuracy_m,
                    False,
                )
                return _render(
                    request,
                    attendance=attendance,
                    error_message=(
                        "Attendance outcome was not confirmed. Please try again."
                    ),
                )
        _audit(request, db, subject, action, exc.code, payload.accuracy_m, False)
        return _render(request, error_message=exc.message, unavailable=exc.unavailable)
    except Exception:
        logger.exception("Dashboard attendance punch failed")
        _audit(
            request,
            db,
            subject,
            action,
            "attendance_unavailable",
            payload.accuracy_m,
            False,
        )
        return _render(
            request,
            error_message="Attendance is temporarily unavailable. Please try again.",
            unavailable=True,
        )

    _audit(request, db, subject, action, "success", payload.accuracy_m, True)
    return _render(request, attendance=attendance)


def _audit(
    request: Request,
    db: Session,
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
            actor_id=str(subject),
            metadata={
                "source": "SELFCARE",
                "outcome": outcome,
                "location_accuracy_m": accuracy_m,
            },
            status_code=200 if success else 400,
            is_success=success,
            request_id=str(getattr(request.state, "request_id", "")) or None,
            defer_until_commit=False,
        )
    except Exception:
        logger.warning("Selfcare attendance audit failed", exc_info=True)
