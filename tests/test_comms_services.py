from datetime import UTC, datetime

from app.schemas.comms import CustomerNotificationCreate, EtaUpdateCreate
from app.services import comms as comms_service


def test_eta_update(db_session, work_order):
    eta = comms_service.eta_updates.create(
        db_session,
        EtaUpdateCreate(
            work_order_id=work_order.id,
            eta_at=datetime(2099, 1, 1, tzinfo=UTC),
        ),
    )
    assert eta.work_order_id == work_order.id


def test_customer_notification(db_session, work_order):
    event = comms_service.customer_notifications.create(
        db_session,
        CustomerNotificationCreate(
            entity_type="work_order",
            entity_id=work_order.id,
            channel="sms",
            recipient="+123456789",
            message="Technician is on the way",
        ),
    )
    assert event.status.value == "pending"
