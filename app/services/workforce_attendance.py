"""Provider-neutral workforce attendance capability facade.

ERP is the source of truth. This module owns no attendance persistence or
business calculations; it validates the edge contract and delegates through
the configured capability binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.dotmac_erp.client import DotMacERPError, DotMacERPTransientError
from app.services.integrations.erp_capability import capability_client


class AttendanceAction(StrEnum):
    CHECK_IN = "check_in"
    CHECK_OUT = "check_out"


class AttendanceState(StrEnum):
    NOT_CHECKED_IN = "not_checked_in"
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"
    INELIGIBLE = "ineligible"


class WorkforceAttendanceError(Exception):
    def __init__(self, code: str, message: str, *, unavailable: bool = False) -> None:
        self.code = code
        self.message = message
        self.unavailable = unavailable
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BrowserLocation:
    latitude: float
    longitude: float
    accuracy_m: float | None
    observed_at: datetime | None

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90:
            raise ValueError("latitude out of range")
        if not -180 <= self.longitude <= 180:
            raise ValueError("longitude out of range")
        if self.accuracy_m is not None and self.accuracy_m < 0:
            raise ValueError("accuracy_m must be non-negative")

    def as_payload(self) -> dict:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "accuracy_m": self.accuracy_m,
            "observed_at": self.observed_at.isoformat()
            if self.observed_at is not None
            else None,
        }


@dataclass(frozen=True, slots=True)
class AttendanceView:
    state: AttendanceState
    attendance_date: str
    timezone: str
    check_in_at: datetime | None
    check_out_at: datetime | None
    working_hours: Decimal | None
    status: str | None
    allowed_actions: tuple[AttendanceAction, ...]
    reason: str | None = None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise WorkforceAttendanceError(
            "invalid_provider_response",
            "Attendance is temporarily unavailable.",
            unavailable=True,
        ) from exc


def _normalize(response: dict) -> AttendanceView:
    try:
        state = AttendanceState(str(response["state"]))
        attendance_date = str(response["attendance_date"])
        timezone = str(response["timezone"])
        allowed_actions_raw = response.get("allowed_actions", [])
        if not isinstance(allowed_actions_raw, (list, tuple)):
            raise TypeError("allowed_actions must be a collection")
        allowed_actions = tuple(
            AttendanceAction(str(item)) for item in allowed_actions_raw
        )
        working_hours_raw = response.get("working_hours")
        return AttendanceView(
            state=state,
            attendance_date=attendance_date,
            timezone=timezone,
            check_in_at=_parse_datetime(response.get("check_in_at")),
            check_out_at=_parse_datetime(response.get("check_out_at")),
            working_hours=(
                Decimal(str(working_hours_raw))
                if working_hours_raw is not None
                else None
            ),
            status=str(response["status"]) if response.get("status") else None,
            allowed_actions=allowed_actions,
            reason=str(response["reason"]) if response.get("reason") else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkforceAttendanceError(
            "invalid_provider_response",
            "Attendance is temporarily unavailable.",
            unavailable=True,
        ) from exc


class WorkforceAttendanceService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def today(self, subject: UUID, *, request_id: str) -> AttendanceView:
        try:
            response = capability_client(self.db).get_attendance_today(
                str(subject), request_id
            )
        except DotMacERPTransientError as exc:
            raise WorkforceAttendanceError(
                "attendance_unavailable",
                "Attendance is temporarily unavailable. Please try again.",
                unavailable=True,
            ) from exc
        except DotMacERPError as exc:
            raise self._map_provider_error(exc) from exc
        return _normalize(response)

    def punch(
        self,
        action: AttendanceAction,
        subject: UUID,
        location: BrowserLocation,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> AttendanceView:
        if action not in {AttendanceAction.CHECK_IN, AttendanceAction.CHECK_OUT}:
            raise ValueError("unsupported attendance action")
        try:
            response = capability_client(self.db).punch_attendance(
                action.value,
                str(subject),
                location.as_payload(),
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
        except DotMacERPTransientError as exc:
            raise WorkforceAttendanceError(
                "attendance_unavailable",
                "Attendance is temporarily unavailable. Please try again.",
                unavailable=True,
            ) from exc
        except DotMacERPError as exc:
            raise self._map_provider_error(exc) from exc
        return _normalize(response)

    @staticmethod
    def _map_provider_error(exc: DotMacERPError) -> WorkforceAttendanceError:
        code = str(exc)
        messages = {
            "employee_not_linked": "Attendance is not available for this account.",
            "employee_mapping_ambiguous": "Attendance is not available for this account.",
            "employee_inactive": "Attendance is not available for inactive employees.",
            "attendance_disabled": "Attendance is not enabled for this account.",
            "outside_geofence": "You are outside the permitted attendance location.",
            "invalid_location": "The supplied location could not be validated.",
            "location_required": "Location access is required to record attendance.",
            "already_checked_in": "You are already checked in.",
            "already_checked_out": "You are already checked out.",
            "check_in_required": "Check in is required before checkout.",
            "overnight_shift_not_supported": "Selfcare attendance is not yet available for overnight shifts.",
            "authorization_failed": "Attendance is not available for this account.",
        }
        return WorkforceAttendanceError(
            code,
            messages.get(code, "Attendance could not be recorded. Please try again."),
        )
