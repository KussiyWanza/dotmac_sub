"""Self-service device-command transport schemas."""

from uuid import UUID

from pydantic import BaseModel, Field

from app.services.customer_device_commands import (
    CustomerDeviceCommandKind,
    CustomerDeviceCommandStatus,
)


class CustomerWifiUpdateRequest(BaseModel):
    ssid: str = Field(min_length=1, max_length=32)
    password: str | None = Field(default=None, min_length=8, max_length=63)
    idempotency_key: str = Field(min_length=1, max_length=160)


class CustomerDeviceCommandOutcomeRead(BaseModel):
    command: CustomerDeviceCommandKind
    status: CustomerDeviceCommandStatus
    subscription_id: UUID
    device_id: UUID | None
    operation_id: UUID | None
    message: str
