from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models.network_monitoring import AlertSeverity, AlertStatus
from app.models.notification import (
    DeliveryStatus,
    NotificationChannel,
    NotificationStatus,
    SuppressionReason,
    SuppressionScope,
)


class NotificationDeliveryLatency(enum.StrEnum):
    immediate = "immediate"
    normal = "normal"
    batch = "batch"


class NotificationTemplateBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=120)
    channel: NotificationChannel
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1)
    conditions: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class NotificationTemplateCreate(NotificationTemplateBase):
    pass


class NotificationTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, min_length=1, max_length=120)
    channel: NotificationChannel | None = None
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, min_length=1)
    conditions: dict[str, Any] | None = None
    is_active: bool | None = None


class NotificationTemplateRead(NotificationTemplateBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class NotificationBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    template_id: UUID | None = None
    subscriber_id: UUID | None = None
    communication_intent_id: UUID | None = None
    audience_type: str | None = Field(default=None, max_length=40)
    audience_id: UUID | None = None
    channel: NotificationChannel
    event_type: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=40)
    recipient: str = Field(min_length=1, max_length=255)
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = None
    metadata_: dict[str, Any] = Field(
        default_factory=dict,
        # ORM models also inherit SQLAlchemy's ``metadata`` schema object, so
        # prefer the mapped attribute before accepting the public JSON key.
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    status: NotificationStatus = NotificationStatus.queued
    send_at: datetime | None = None
    sent_at: datetime | None = None
    last_error: str | None = None
    retry_count: int = Field(default=0, ge=0)
    is_active: bool = True
    delivery_latency: NotificationDeliveryLatency = NotificationDeliveryLatency.normal


class NotificationCreate(NotificationBase):
    pass


class NotificationUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    template_id: UUID | None = None
    subscriber_id: UUID | None = None
    communication_intent_id: UUID | None = None
    audience_type: str | None = Field(default=None, max_length=40)
    audience_id: UUID | None = None
    channel: NotificationChannel | None = None
    event_type: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=40)
    recipient: str | None = Field(default=None, max_length=255)
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = None
    metadata_: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    status: NotificationStatus | None = None
    send_at: datetime | None = None
    sent_at: datetime | None = None
    last_error: str | None = None
    retry_count: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class NotificationRead(NotificationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class CustomerInboxNotificationRead(NotificationRead):
    """A notification plus the signed-in customer's server-owned read state."""

    is_read: bool = False


class CustomerNotificationReadRequest(BaseModel):
    """Mark selected notifications, or the customer's entire visible inbox."""

    notification_ids: list[UUID] = Field(default_factory=list, max_length=500)
    all_visible: bool = False


class CustomerNotificationReadResponse(BaseModel):
    marked: int = Field(ge=0)


class CommunicationIntentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscriber_id: UUID | None
    event_type: str
    category: str
    communication_class: str
    template_id: UUID | None
    template_code: str | None
    subject: str | None
    body: str | None
    channels: list[str]
    include_reseller: bool
    status: str
    suppression_reasons: list[str]
    dedupe_key: str | None
    scheduled_for: datetime | None
    metadata_: dict[str, Any] = Field(serialization_alias="metadata")
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CommunicationSuppressionCreate(BaseModel):
    subscriber_id: UUID | None = None
    channel: NotificationChannel
    address: str = Field(min_length=1, max_length=320)
    scope: SuppressionScope = SuppressionScope.marketing
    reason: SuppressionReason = SuppressionReason.unsubscribe
    note: str | None = None
    created_by: str | None = Field(default=None, max_length=120)


class CommunicationSuppressionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscriber_id: UUID | None
    channel: NotificationChannel
    address: str
    raw_address: str | None
    scope: SuppressionScope
    reason: SuppressionReason
    note: str | None
    created_at: datetime
    created_by: str | None


class NotificationBulkCreateRequest(BaseModel):
    template_id: UUID | None = None
    channel: NotificationChannel
    recipients: list[str]
    event_type: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=40)
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = None
    status: NotificationStatus = NotificationStatus.queued
    send_at: datetime | None = None


class NotificationBulkCreateResponse(BaseModel):
    created: int
    notification_ids: list[UUID]


class NotificationDeliveryBulkUpdateRequest(BaseModel):
    delivery_ids: list[UUID]
    status: DeliveryStatus
    response_code: str | None = Field(default=None, max_length=60)
    response_body: str | None = None
    provider_message_id: str | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None


class NotificationDeliveryBulkUpdateResponse(BaseModel):
    updated: int


class NotificationDeliveryBase(BaseModel):
    notification_id: UUID
    provider: str | None = Field(default=None, max_length=120)
    provider_message_id: str | None = Field(default=None, max_length=200)
    status: DeliveryStatus
    response_code: str | None = Field(default=None, max_length=60)
    response_body: str | None = None
    occurred_at: datetime | None = None
    is_active: bool = True


class NotificationDeliveryCreate(NotificationDeliveryBase):
    pass


class NotificationDeliveryUpdate(BaseModel):
    notification_id: UUID | None = None
    provider: str | None = Field(default=None, max_length=120)
    provider_message_id: str | None = Field(default=None, max_length=200)
    status: DeliveryStatus | None = None
    response_code: str | None = Field(default=None, max_length=60)
    response_body: str | None = None
    occurred_at: datetime | None = None
    is_active: bool | None = None


class NotificationDeliveryRead(NotificationDeliveryBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class AlertNotificationPolicyBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    channel: NotificationChannel
    recipient: str = Field(min_length=1, max_length=255)
    template_id: UUID | None = None
    connector_config_id: UUID | None = None
    rule_id: UUID | None = None
    device_id: UUID | None = None
    interface_id: UUID | None = None
    severity_min: AlertSeverity = AlertSeverity.warning
    status: AlertStatus = AlertStatus.open
    is_active: bool = True
    notes: str | None = None


class AlertNotificationPolicyCreate(AlertNotificationPolicyBase):
    pass


class AlertNotificationPolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    channel: NotificationChannel | None = None
    recipient: str | None = Field(default=None, max_length=255)
    template_id: UUID | None = None
    connector_config_id: UUID | None = None
    rule_id: UUID | None = None
    device_id: UUID | None = None
    interface_id: UUID | None = None
    severity_min: AlertSeverity | None = None
    status: AlertStatus | None = None
    is_active: bool | None = None
    notes: str | None = None


class AlertNotificationPolicyRead(AlertNotificationPolicyBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class AlertNotificationLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    alert_id: UUID
    policy_id: UUID
    notification_id: UUID | None
    created_at: datetime


class OnCallRotationBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    timezone: str = Field(default="Africa/Lagos", max_length=60)
    is_active: bool = True
    notes: str | None = None


class OnCallRotationCreate(OnCallRotationBase):
    pass


class OnCallRotationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    timezone: str | None = Field(default=None, max_length=60)
    is_active: bool | None = None
    notes: str | None = None


class OnCallRotationRead(OnCallRotationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OnCallRotationMemberBase(BaseModel):
    rotation_id: UUID
    name: str = Field(min_length=1, max_length=120)
    contact: str = Field(min_length=1, max_length=255)
    priority: int = 0
    last_used_at: datetime | None = None
    is_active: bool = True


class OnCallRotationMemberCreate(OnCallRotationMemberBase):
    pass


class OnCallRotationMemberUpdate(BaseModel):
    rotation_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    contact: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int | None = None
    last_used_at: datetime | None = None
    is_active: bool | None = None


class OnCallRotationMemberRead(OnCallRotationMemberBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class AlertNotificationPolicyStepBase(BaseModel):
    policy_id: UUID
    step_index: int = 0
    delay_minutes: int = Field(default=0, ge=0)
    channel: NotificationChannel
    recipient: str | None = Field(default=None, max_length=255)
    template_id: UUID | None = None
    connector_config_id: UUID | None = None
    rotation_id: UUID | None = None
    severity_min: AlertSeverity = AlertSeverity.warning
    status: AlertStatus = AlertStatus.open
    is_active: bool = True


class AlertNotificationPolicyStepCreate(AlertNotificationPolicyStepBase):
    pass


class AlertNotificationPolicyStepUpdate(BaseModel):
    policy_id: UUID | None = None
    step_index: int | None = None
    delay_minutes: int | None = Field(default=None, ge=0)
    channel: NotificationChannel | None = None
    recipient: str | None = Field(default=None, max_length=255)
    template_id: UUID | None = None
    connector_config_id: UUID | None = None
    rotation_id: UUID | None = None
    severity_min: AlertSeverity | None = None
    status: AlertStatus | None = None
    is_active: bool | None = None


class AlertNotificationPolicyStepRead(AlertNotificationPolicyStepBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PushTokenRegister(BaseModel):
    """Mobile push-token registration from the app (POST /me/push-tokens)."""

    token: str = Field(min_length=1, max_length=512)
    platform: str | None = Field(default=None, max_length=16)


class PushTokenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    platform: str | None = None
    is_active: bool
