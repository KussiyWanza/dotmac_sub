from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.system_user import SystemUser
from app.schemas.workforce_attendance import DashboardAttendanceLocation
from app.services import web_admin_attendance
from app.services.workforce_attendance import (
    AttendanceAction,
    AttendanceState,
    AttendanceView,
    WorkforceAttendanceError,
)


def _request(user: SystemUser):
    return SimpleNamespace(
        state=SimpleNamespace(
            auth={"principal_type": "system_user", "principal_id": str(user.id)},
            user=user,
            request_id="correlation-1",
        )
    )


def _user(active: bool = True) -> SystemUser:
    return SystemUser(
        id=uuid4(),
        first_name="Ada",
        last_name="Lovelace",
        email=f"{uuid4().hex}@example.test",
        is_active=active,
    )


def _view(state: AttendanceState) -> AttendanceView:
    checked_in = datetime(2026, 8, 9, 8, 4, tzinfo=UTC)
    return AttendanceView(
        state=state,
        attendance_date="2026-08-09",
        timezone="Africa/Lagos",
        check_in_at=checked_in if state != AttendanceState.NOT_CHECKED_IN else None,
        check_out_at=(
            datetime(2026, 8, 9, 17, 11, tzinfo=UTC)
            if state == AttendanceState.CHECKED_OUT
            else None
        ),
        working_hours=None,
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


def test_attendance_partial_is_user_specific_and_independent(monkeypatch):
    captured = {}
    user = _user()
    service = MagicMock()
    service.today.return_value = _view(AttendanceState.CHECKED_IN)
    monkeypatch.setattr(
        web_admin_attendance, "WorkforceAttendanceService", lambda _db: service
    )
    monkeypatch.setattr(
        web_admin_attendance.templates,
        "TemplateResponse",
        lambda name, context: captured.update(name=name, context=context) or context,
    )

    result = web_admin_attendance.load(_request(user), MagicMock())

    assert captured["name"] == "admin/dashboard/_attendance.html"
    assert result["attendance"].state == AttendanceState.CHECKED_IN
    service.today.assert_called_once_with(user.id, request_id="correlation-1")


def test_inactive_system_user_never_calls_provider(monkeypatch):
    captured = {}
    provider = MagicMock()
    monkeypatch.setattr(web_admin_attendance, "WorkforceAttendanceService", provider)
    monkeypatch.setattr(
        web_admin_attendance.templates,
        "TemplateResponse",
        lambda _name, context: captured.update(context) or context,
    )

    web_admin_attendance.load(_request(_user(active=False)), MagicMock())

    provider.assert_not_called()
    assert "not available" in captured["error_message"].lower()


def test_dashboard_punch_forwards_only_browser_location(monkeypatch):
    captured = {}
    user = _user()
    service = MagicMock()
    service.punch.return_value = _view(AttendanceState.CHECKED_IN)
    monkeypatch.setattr(
        web_admin_attendance, "WorkforceAttendanceService", lambda _db: service
    )
    monkeypatch.setattr(
        web_admin_attendance,
        "allow_operation",
        lambda *_a, **_k: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(web_admin_attendance, "_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        web_admin_attendance.templates,
        "TemplateResponse",
        lambda _name, context: captured.update(context) or context,
    )
    payload = DashboardAttendanceLocation(
        latitude=9.0765,
        longitude=7.3986,
        accuracy_m=12.5,
        observed_at=datetime(2026, 8, 9, 7, 4, tzinfo=UTC),
    )

    web_admin_attendance.punch(
        _request(user),
        MagicMock(),
        action=AttendanceAction.CHECK_IN,
        payload=payload,
        idempotency_key="request-1",
    )

    args, kwargs = service.punch.call_args
    assert args[0:2] == (AttendanceAction.CHECK_IN, user.id)
    assert args[2].latitude == payload.latitude
    assert args[2].longitude == payload.longitude
    assert kwargs["idempotency_key"] == "request-1"
    assert not hasattr(args[2], "employee_id")
    assert captured["attendance"].state == AttendanceState.CHECKED_IN


def test_uncertain_checkout_is_confirmed_only_by_erp_postcondition(monkeypatch):
    captured = {}
    user = _user()
    service = MagicMock()
    service.punch.side_effect = WorkforceAttendanceError(
        "attendance_unavailable",
        "Attendance is temporarily unavailable.",
        unavailable=True,
    )
    service.today.return_value = _view(AttendanceState.CHECKED_OUT)
    monkeypatch.setattr(
        web_admin_attendance, "WorkforceAttendanceService", lambda _db: service
    )
    monkeypatch.setattr(
        web_admin_attendance,
        "allow_operation",
        lambda *_a, **_k: SimpleNamespace(allowed=True),
    )
    audit = MagicMock()
    monkeypatch.setattr(web_admin_attendance, "_audit", audit)
    monkeypatch.setattr(
        web_admin_attendance.templates,
        "TemplateResponse",
        lambda _name, context: captured.update(context) or context,
    )

    web_admin_attendance.punch(
        _request(user),
        MagicMock(),
        action=AttendanceAction.CHECK_OUT,
        payload=DashboardAttendanceLocation(latitude=9.0, longitude=7.0),
        idempotency_key="checkout-request-1",
    )

    assert captured["attendance"].state == AttendanceState.CHECKED_OUT
    assert captured["error_message"] is None
    assert audit.call_args.args[4] == "reconciled_success"
    assert audit.call_args.args[6] is True


def test_uncertain_checkout_that_remains_checked_in_is_not_fabricated(monkeypatch):
    captured = {}
    user = _user()
    service = MagicMock()
    service.punch.side_effect = WorkforceAttendanceError(
        "attendance_unavailable",
        "Attendance is temporarily unavailable.",
        unavailable=True,
    )
    service.today.return_value = _view(AttendanceState.CHECKED_IN)
    monkeypatch.setattr(
        web_admin_attendance, "WorkforceAttendanceService", lambda _db: service
    )
    monkeypatch.setattr(
        web_admin_attendance,
        "allow_operation",
        lambda *_a, **_k: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(web_admin_attendance, "_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        web_admin_attendance.templates,
        "TemplateResponse",
        lambda _name, context: captured.update(context) or context,
    )

    web_admin_attendance.punch(
        _request(user),
        MagicMock(),
        action=AttendanceAction.CHECK_OUT,
        payload=DashboardAttendanceLocation(latitude=9.0, longitude=7.0),
        idempotency_key="checkout-request-1",
    )

    assert captured["attendance"].state == AttendanceState.CHECKED_IN
    assert (
        captured["error_message"]
        == "Attendance outcome was not confirmed. Please try again."
    )


def test_duplicate_checkout_is_success_only_when_erp_confirms_checked_out(monkeypatch):
    captured = {}
    user = _user()
    service = MagicMock()
    service.punch.side_effect = WorkforceAttendanceError(
        "already_checked_out", "You are already checked out."
    )
    service.today.return_value = _view(AttendanceState.CHECKED_OUT)
    monkeypatch.setattr(
        web_admin_attendance, "WorkforceAttendanceService", lambda _db: service
    )
    monkeypatch.setattr(
        web_admin_attendance,
        "allow_operation",
        lambda *_a, **_k: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(web_admin_attendance, "_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        web_admin_attendance.templates,
        "TemplateResponse",
        lambda _name, context: captured.update(context) or context,
    )

    web_admin_attendance.punch(
        _request(user),
        MagicMock(),
        action=AttendanceAction.CHECK_OUT,
        payload=DashboardAttendanceLocation(latitude=9.0, longitude=7.0),
        idempotency_key="checkout-request-1",
    )

    assert captured["attendance"].state == AttendanceState.CHECKED_OUT
    assert captured["error_message"] is None


def test_attendance_audit_uses_the_sanctioned_writer_without_adapter_commit(
    monkeypatch,
):
    user = _user()
    db = MagicMock()
    audit = MagicMock()
    monkeypatch.setattr(web_admin_attendance, "record_audit_event", audit)

    web_admin_attendance._audit(
        _request(user),
        db,
        user.id,
        AttendanceAction.CHECK_IN,
        "success",
        12.5,
        True,
    )

    audit.assert_called_once()
    assert audit.call_args.kwargs["action"] == "attendance_check_in"
    assert audit.call_args.kwargs["defer_until_commit"] is False
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_dashboard_templates_and_script_preserve_lazy_safe_states():
    root = Path(__file__).resolve().parents[1]
    index = (root / "templates/admin/dashboard/index.html").read_text()
    layout = (root / "templates/layouts/admin.html").read_text()
    partial = (root / "templates/admin/dashboard/_attendance.html").read_text()
    script = (root / "static/js/admin-attendance.js").read_text()
    reminder = (root / "static/js/admin-attendance-reminder.js").read_text()

    assert 'hx-get="/admin/dashboard/attendance"' in index
    assert index.index("Add Customer") < index.index('id="attendance-widget"')
    assert 'can(request, "attendance:self:use")' in layout
    assert "/static/js/admin-attendance-reminder.js" in layout
    assert "/static/js/session-refresh.js" in layout
    assert 'refreshUrl: "/admin/session/refresh"' in layout
    assert "_dashboard_global_cache" not in partial
    assert "Ready to check in" in partial
    assert "Check In" in partial and "Check Out" in partial
    assert "data-attendance-state" in partial
    assert "data-attendance-date" in partial
    assert "data-attendance-can-check-in" in partial
    assert "data-attendance-start" in partial
    assert "data-attendance-end" in partial
    assert "formatElapsed" in script
    assert "clearAttendanceTimer" in script
    assert "maximumAge: 0" in script
    assert "enableHighAccuracy: true" in script
    assert "X-CSRF-Token" in script
    assert "employee_id" not in script
    assert "fetch(attendanceUrl" in reminder
    assert "checkIntervalMs = 10 * 60 * 1000" in reminder
    assert "unavailableRetryMs = 5 * 60 * 1000" in reminder
    assert "snoozeMs = 10 * 60 * 1000" in reminder
    assert "dismissedDateKey" in reminder
    assert "window.location.href = dashboardUrl" in reminder
    assert "/admin/dashboard/attendance/check-in" not in reminder
