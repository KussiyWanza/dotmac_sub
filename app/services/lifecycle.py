from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.services.common import (
    apply_ordering,
    apply_pagination,
    validate_enum,
)
from app.services.response import ListResponseMixin


class SubscriptionLifecycleEvents(ListResponseMixin):
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

    # `create`, `update` and `delete` are deliberately absent. Generic creation
    # cannot prove a status transition or reserve a source identity, while edits
    # set arbitrary attributes and deletes erase contractual evidence.
    #
    # The lifecycle owner appends through subscription_lifecycle_evidence;
    # corrections are later transitions or prospective baselines, never CRUD.
    # The database trigger remains the final append-only enforcement boundary.
    #
    # The retired update path set any attribute
    # from a partial payload — including to_status and created_at — and hard
    # deleted rows, so a customer's entitlement history could be rewritten
    # after a contractual period had been scored against it. Migration 468
    # enforces append-only in the database, because a service can be re-added
    # and a migration cannot be argued with. Corrections are new transitions,
    # never edits to old ones.


subscription_lifecycle_events = SubscriptionLifecycleEvents()
