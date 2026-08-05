from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.catalog import SubscriptionStatus
from app.models.lifecycle import LifecycleEventType


class SubscriptionLifecycleEventBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    subscription_id: UUID
    event_type: LifecycleEventType = LifecycleEventType.other
    from_status: SubscriptionStatus | None = None
    to_status: SubscriptionStatus | None = None
    reason: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    actor: str | None = Field(default=None, max_length=120)


class SubscriptionLifecycleEventRead(SubscriptionLifecycleEventBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
