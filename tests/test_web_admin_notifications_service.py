import uuid
from pathlib import Path
from types import SimpleNamespace

from app.models.admin_alert import AdminNotification
from app.models.system_user import SystemUser
from app.services import web_admin_notifications


def _system_user(db_session, label: str) -> SystemUser:
    user = SystemUser(
        first_name=label,
        last_name="Staff",
        email=f"{label.lower()}-{uuid.uuid4()}@example.com",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    return user


def test_notifications_menu_has_no_global_fallback(db_session, monkeypatch):
    request = SimpleNamespace()
    monkeypatch.setattr(
        web_admin_notifications.web_admin_service,
        "get_current_user",
        lambda _request: {"principal_type": "subscriber", "id": "subscriber-1"},
    )

    response = web_admin_notifications.notifications_menu(request, db_session)

    assert "No notifications yet." in response.body.decode()


def test_notifications_menu_only_renders_current_staff_inbox(db_session, monkeypatch):
    current_user = _system_user(db_session, "Current")
    other_user = _system_user(db_session, "Other")
    db_session.add_all(
        [
            AdminNotification(
                system_user_id=current_user.id,
                title="My assignment",
                body="Assigned to you",
                target_url="/admin/projects/8",
            ),
            AdminNotification(
                system_user_id=other_user.id,
                title="Other staff secret",
                body="Not for this user",
                target_url="/admin/projects/9",
            ),
        ]
    )
    db_session.commit()
    request = SimpleNamespace()
    monkeypatch.setattr(
        web_admin_notifications.web_admin_service,
        "get_current_user",
        lambda _request: {
            "principal_type": "system_user",
            "id": str(current_user.id),
        },
    )

    response = web_admin_notifications.notifications_menu(request, db_session)
    body = response.body.decode()

    assert "My assignment" in body
    assert "Other staff secret" not in body
    assert "Recent notifications" not in body
    assert "1 unread notifications" in body
    assert response.headers["cache-control"] == "private, no-store"


def test_notification_badge_loads_once_without_recurring_poll() -> None:
    layout = (
        Path(__file__).parents[1] / "templates" / "layouts" / "admin.html"
    ).read_text(encoding="utf-8")

    assert 'hx-get="/admin/notifications"' in layout
    assert 'hx-trigger="load"' in layout
    assert 'hx-trigger="load, every 30s"' not in layout
    assert 'hx-trigger="intersect once"' not in layout


def test_operation_tracker_initializes_only_for_queued_operations() -> None:
    base = (Path(__file__).parents[1] / "templates" / "base.html").read_text(
        encoding="utf-8"
    )

    assert "window.initOperationTracker();" not in base
    assert (
        "const tracker = window.initOperationTracker "
        "? window.initOperationTracker() : window.operationTracker;"
    ) in base
    assert "tracker.track(data.operationQueued.operation_id" in base
