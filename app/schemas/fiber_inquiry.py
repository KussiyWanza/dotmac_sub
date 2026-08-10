"""Typed contract for signed fiber.dotmac.ng inquiry ingress."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class FiberInquiryInterest(StrEnum):
    new_connection = "new_connection"
    technical_support = "technical_support"
    billing = "billing"
    enterprise_services = "enterprise_services"
    academy = "academy"
    other = "other"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class FiberInquiryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    form_version: str = Field(pattern=r"^fiber-contact-v1$")
    full_name: str = Field(min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr
    interest: FiberInquiryInterest
    message: str | None = Field(default=None, max_length=4000)
    submitted_at: datetime

    @field_validator("phone", "message", mode="before")
    @classmethod
    def empty_text_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("submitted_at")
    @classmethod
    def submitted_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("submitted_at must include a timezone")
        return value


class FiberInquiryReceipt(BaseModel):
    observation_id: UUID
    conversation_id: UUID
    message_id: UUID
    replayed: bool
    resolution_status: str
