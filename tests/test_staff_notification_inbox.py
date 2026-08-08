import uuid

from app.models.admin_alert import AdminNotification
from app.models.notification import Notification, NotificationChannel
from app.models.system_user import SystemUser
from app.services.staff_notifications import queue_staff_push


def test_staff_push_creates_linked_inbox_item(db_session):
    user = SystemUser(
        first_name="Inbox",
        last_name="User",
        email=f"inbox-{uuid.uuid4()}@example.com",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    queue_staff_push(
        db_session,
        recipient=str(user.id),
        subject="Ticket assigned",
        body="You were assigned ticket 42.",
        target_url="/admin/support/tickets/42",
    )
    db_session.flush()

    delivery = db_session.query(Notification).one()
    inbox_item = db_session.query(AdminNotification).one()
    assert delivery.channel == NotificationChannel.push
    assert inbox_item.source_notification_id == delivery.id
    assert inbox_item.system_user_id == user.id
    assert inbox_item.target_url == "/admin/support/tickets/42"


def test_non_uuid_push_recipient_is_not_added_to_staff_inbox(db_session):
    queue_staff_push(
        db_session,
        recipient="legacy@example.com",
        subject="Legacy delivery",
        body="Transport only",
    )
    db_session.flush()

    assert db_session.query(Notification).count() == 1
    assert db_session.query(AdminNotification).count() == 0
