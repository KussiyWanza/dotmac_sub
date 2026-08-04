import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.schemas.lifecycle import (
    SubscriptionLifecycleEventCreate,
)
from app.services import settings_spec
from app.services.common import (
    apply_ordering,
    apply_pagination,
    validate_enum,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class SubscriptionLifecycleEvents(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SubscriptionLifecycleEventCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "event_type" not in fields_set:
            default_type = settings_spec.resolve_value(
                db, SettingDomain.lifecycle, "default_event_type"
            )
            if default_type:
                data["event_type"] = validate_enum(
                    default_type, LifecycleEventType, "event_type"
                )
        event = SubscriptionLifecycleEvent(**data)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def get(db: Session, event_id: str):
        event = db.get(SubscriptionLifecycleEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Lifecycle event not found")
        return event

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        event_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriptionLifecycleEvent)
        if subscription_id:
            query = query.filter(
                SubscriptionLifecycleEvent.subscription_id == subscription_id
            )
        if event_type:
            query = query.filter(
                SubscriptionLifecycleEvent.event_type
                == validate_enum(event_type, LifecycleEventType, "event_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": SubscriptionLifecycleEvent.created_at,
                "event_type": SubscriptionLifecycleEvent.event_type,
            },
        )
        return apply_pagination(query, limit, offset).all()

    # `update` and `delete` are deliberately absent. They set any attribute
    # from a partial payload — including to_status and created_at — and hard
    # deleted rows, so a customer's entitlement history could be rewritten
    # after a contractual period had been scored against it. Migration 468
    # enforces append-only in the database, because a service can be re-added
    # and a migration cannot be argued with. Corrections are new transitions,
    # never edits to old ones.


subscription_lifecycle_events = SubscriptionLifecycleEvents()
