from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.audit import AuditActorType


class AuditEventBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    actor_type: AuditActorType = AuditActorType.system
    actor_id: str | None = None
    actor_label: str | None = None
    actor_party_id: UUID | None = None
    action: str
    entity_type: str
    entity_id: str | None = None
    status_code: int | None = None
    is_success: bool = True
    is_active: bool = True
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    details: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_actor_pair(self) -> "AuditEventBase":
        """Keep Sub writes inside the kernel's closed actor contract."""

        if self.actor_type is AuditActorType.system:
            return self
        if self.actor_id is None or not self.actor_id.strip():
            raise ValueError(
                f"audit actor type {self.actor_type.value!r} needs a non-empty actor_id"
            )
        return self


class AuditEventCreate(AuditEventBase):
    occurred_at: datetime | None = None


class AuditEventRead(AuditEventBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    occurred_at: datetime
    created_at: datetime | None = None
