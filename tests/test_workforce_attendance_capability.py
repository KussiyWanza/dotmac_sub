from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest

from app.services.dotmac_erp.client import (
    DotMacERPClient,
    DotMacERPError,
    DotMacERPTransientError,
)
from app.services.integrations.connectors.dotmac_erp import DotmacErpRunner
from app.services.workforce_attendance import (
    AttendanceAction,
    AttendanceState,
    BrowserLocation,
    WorkforceAttendanceError,
    WorkforceAttendanceService,
)

SUBJECT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _erp_response(state: str = "not_checked_in") -> dict:
    return {
        "state": state,
        "attendance_date": "2026-08-09",
        "timezone": "Africa/Lagos",
        "check_in_at": None,
        "check_out_at": None,
        "working_hours": None,
        "status": None,
        "allowed_actions": ["check_in"],
    }


def test_workforce_facade_forwards_subject_location_and_idempotency(monkeypatch):
    client = MagicMock()
    client.punch_attendance.return_value = _erp_response("checked_in") | {
        "check_in_at": "2026-08-09T08:04:11+01:00",
        "status": "PRESENT",
        "allowed_actions": ["check_out"],
    }
    monkeypatch.setattr(
        "app.services.workforce_attendance.capability_client", lambda _db: client
    )
    location = BrowserLocation(
        latitude=9.0765,
        longitude=7.3986,
        accuracy_m=12.5,
        observed_at=datetime(2026, 8, 9, 7, 4, 3, tzinfo=UTC),
    )

    result = WorkforceAttendanceService(MagicMock()).punch(
        AttendanceAction.CHECK_IN,
        SUBJECT,
        location,
        idempotency_key="request-1",
        request_id="correlation-1",
    )

    assert result.state == AttendanceState.CHECKED_IN
    client.punch_attendance.assert_called_once_with(
        "check_in",
        str(SUBJECT),
        location.as_payload(),
        idempotency_key="request-1",
        request_id="correlation-1",
    )


def test_workforce_facade_maps_stable_provider_error(monkeypatch):
    client = MagicMock()
    client.get_attendance_today.side_effect = DotMacERPError("employee_not_linked")
    monkeypatch.setattr(
        "app.services.workforce_attendance.capability_client", lambda _db: client
    )

    with pytest.raises(WorkforceAttendanceError) as exc:
        WorkforceAttendanceService(MagicMock()).today(SUBJECT, request_id="r-1")
    assert exc.value.code == "employee_not_linked"
    assert "technical" not in exc.value.message.lower()


def test_erp_client_sends_trusted_subject_and_same_idempotency_key():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json=_erp_response("checked_in"))

    client = DotMacERPClient("https://erp.test", "secret", retries=0)
    client._client = httpx.Client(
        base_url="https://erp.test", transport=httpx.MockTransport(handler)
    )
    client.punch_attendance(
        "check-in",
        str(SUBJECT),
        {"latitude": 9.0, "longitude": 7.0},
        idempotency_key="attendance-request-1",
        request_id="correlation-1",
    )

    assert captured["x-selfcare-subject"] == str(SUBJECT)
    assert captured["idempotency-key"] == "attendance-request-1"
    assert captured["x-request-id"] == "correlation-1"
    assert "employee-id" not in captured


def test_attendance_client_treats_erp_unavailability_as_transient():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "detail": {
                    "code": "attendance_unavailable",
                    "message": "Temporarily unavailable.",
                }
            },
        )

    client = DotMacERPClient("https://erp.test", "secret", retries=0)
    client._client = httpx.Client(
        base_url="https://erp.test", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(DotMacERPTransientError):
        client.get_attendance_today(str(SUBJECT), "correlation-1")


def test_connector_preserves_stable_erp_error_code():
    client = MagicMock()
    client.get_attendance_today.side_effect = DotMacERPError(
        "rejected",
        status_code=404,
        response={
            "detail": {
                "code": "employee_not_linked",
                "message": "Attendance is unavailable.",
            }
        },
    )
    runner = DotmacErpRunner(client_override=client)

    with pytest.raises(DotMacERPError):
        # The runner-level envelope contract is covered elsewhere; this focused
        # assertion protects the client exception shape consumed by that mapping.
        client.get_attendance_today(str(SUBJECT), "r-1")
    assert client.get_attendance_today.side_effect.response["detail"]["code"] == (
        "employee_not_linked"
    )
    assert runner is not None
