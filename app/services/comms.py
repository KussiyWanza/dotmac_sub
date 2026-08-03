import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.comms import (
    CustomerNotificationEvent,
    CustomerNotificationStatus,
    EtaUpdate,
)
from app.models.domain_settings import SettingDomain
from app.schemas.comms import (
    CustomerNotificationCreate,
    CustomerNotificationUpdate,
    EtaUpdateCreate,
)
from app.services import settings_spec
from app.services.common import (
    apply_ordering,
    apply_pagination,
)
from app.services.customer_notification_policy import (
    resolve_subscriber_id_for_recipient,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class CustomerNotifications(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CustomerNotificationCreate):
        data = payload.model_dump()
        data["subscriber_id"] = data.get(
            "subscriber_id"
        ) or resolve_subscriber_id_for_recipient(
            db,
            data.get("recipient"),
        )
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.comms, "default_notification_status"
            )
            if default_status:
                data["status"] = CustomerNotificationStatus(default_status)
        event = CustomerNotificationEvent(**data)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def get(db: Session, event_id: str):
        event = db.get(CustomerNotificationEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Notification event not found")
        return event

    @staticmethod
    def list(
        db: Session,
        entity_type: str | None,
        entity_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CustomerNotificationEvent)
        if entity_type:
            query = query.filter(CustomerNotificationEvent.entity_type == entity_type)
        if entity_id:
            query = query.filter(CustomerNotificationEvent.entity_id == entity_id)
        if status:
            try:
                query = query.filter(
                    CustomerNotificationEvent.status
                    == CustomerNotificationStatus(status)
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid status") from exc
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": CustomerNotificationEvent.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, event_id: str, payload: CustomerNotificationUpdate):
        event = db.get(CustomerNotificationEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Notification event not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(event, key, value)
        db.commit()
        db.refresh(event)
        return event


class EtaUpdates(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: EtaUpdateCreate):
        update = EtaUpdate(**payload.model_dump())
        db.add(update)
        db.commit()
        db.refresh(update)
        return update

    @staticmethod
    def get(db: Session, update_id: str):
        update = db.get(EtaUpdate, update_id)
        if not update:
            raise HTTPException(status_code=404, detail="ETA update not found")
        return update

    @staticmethod
    def list(
        db: Session,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        query = db.query(EtaUpdate)
        query = apply_ordering(
            query, order_by, order_dir, {"created_at": EtaUpdate.created_at}
        )
        return apply_pagination(query, limit, offset).all()


customer_notifications = CustomerNotifications()
eta_updates = EtaUpdates()
