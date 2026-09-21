from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.api.field import attendance as field_attendance
from app.api.field.attendance import router
from app.api.field.principals import require_field_principal
from app.services.workforce_attendance import (
    AttendanceAction,
    AttendancePunchOutcome,
    AttendancePunchResolution,
    AttendanceState,
    AttendanceView,
    WorkforceAttendanceError,
)


def _principal() -> dict[str, object]:
    principal_id = uuid4()
    return {
        "principal_id": str(principal_id),
        "person_id": str(principal_id),
        "principal_type": "system_user",
        "field_actor": "technician",
    }


def _view(state: AttendanceState) -> AttendanceView:
    checked_in = datetime(2026, 9, 9, 7, 30, tzinfo=UTC)
    return AttendanceView(
        state=state,
        attendance_date="2026-09-09",
        timezone="Africa/Lagos",
        check_in_at=(checked_in if state != AttendanceState.NOT_CHECKED_IN else None),
        check_out_at=(
            datetime(2026, 9, 9, 16, 30, tzinfo=UTC)
            if state == AttendanceState.CHECKED_OUT
            else None
        ),
        working_hours=(
            Decimal("9.0") if state == AttendanceState.CHECKED_OUT else None
        ),
        status="PRESENT" if state != AttendanceState.NOT_CHECKED_IN else None,
        allowed_actions=(
            (AttendanceAction.CHECK_IN,)
            if state == AttendanceState.NOT_CHECKED_IN
            else (
                (AttendanceAction.CHECK_OUT,)
                if state == AttendanceState.CHECKED_IN
                else ()
            )
        ),
    )


def _client(monkeypatch, service: MagicMock, principal: dict[str, object]):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/field")
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_field_principal] = lambda: principal
    monkeypatch.setattr(
        field_attendance, "WorkforceAttendanceService", lambda _db: service
    )
    monkeypatch.setattr(
        field_attendance,
        "allow_operation",
        lambda *_args, **_kwargs: type("Decision", (), {"allowed": True})(),
    )
    monkeypatch.setattr(field_attendance, "record_audit_event", MagicMock())
    return TestClient(app)


def test_field_attendance_today_returns_typed_erp_projection(monkeypatch):
    principal = _principal()
    service = MagicMock()
    service.today.return_value = _view(AttendanceState.CHECKED_IN)

    response = _client(monkeypatch, service, principal).get("/api/v1/field/attendance")

    assert response.status_code == 200
    assert response.json()["state"] == "checked_in"
    assert response.json()["allowed_actions"] == ["check_out"]
    service.today.assert_called_once_with(
        subject=UUID(str(principal["principal_id"])),
        request_id="field-attendance-read",
    )


def test_field_check_in_forwards_fresh_location_and_idempotency(monkeypatch):
    principal = _principal()
    service = MagicMock()
    service.punch_confirmed.return_value = AttendancePunchOutcome(
        attendance=_view(AttendanceState.CHECKED_IN),
        resolution=AttendancePunchResolution.DIRECT,
    )

    response = _client(monkeypatch, service, principal).post(
        "/api/v1/field/attendance/check-in",
        headers={"Idempotency-Key": "mobile-punch-1"},
        json={
            "latitude": 9.0765,
            "longitude": 7.3986,
            "accuracy_m": 8.5,
            "observed_at": "2026-09-09T07:29:58Z",
        },
    )

    assert response.status_code == 200
    call = service.punch_confirmed.call_args.kwargs
    assert call["action"] == AttendanceAction.CHECK_IN
    assert str(call["subject"]) == principal["principal_id"]
    assert call["location"].latitude == 9.0765
    assert call["location"].longitude == 7.3986
    assert call["location"].accuracy_m == 8.5
    assert (
        service.punch_confirmed.call_args.kwargs["idempotency_key"] == "mobile-punch-1"
    )


def test_field_attendance_maps_stable_provider_failure(monkeypatch):
    principal = _principal()
    service = MagicMock()
    service.punch_confirmed.side_effect = WorkforceAttendanceError(
        "outside_geofence",
        "You are outside the permitted attendance location.",
    )

    response = _client(monkeypatch, service, principal).post(
        "/api/v1/field/attendance/check-out",
        headers={"Idempotency-Key": "mobile-punch-2"},
        json={
            "latitude": 9.0765,
            "longitude": 7.3986,
            "accuracy_m": 8.5,
            "observed_at": "2026-09-09T16:30:00Z",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "outside_geofence"
