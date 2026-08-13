"""Schemas for the native team-inbox live-chat broker endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator


class FiberChatSessionCreate(BaseModel):
    """Anonymous fiber-site visitor command that starts a native chat."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    form_version: str = Field(pattern=r"^fiber-chat-v1$")
    client_session_id: UUID
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    message: str = Field(min_length=1, max_length=2000)
    page_url: HttpUrl
    referrer_url: HttpUrl | None = None
    started_at: datetime
    company_website: str = Field(default="", max_length=0)

    @field_validator("phone", mode="before")
    @classmethod
    def empty_phone_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("started_at")
    @classmethod
    def started_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("started_at must include a timezone")
        return value


class ChatSessionResponse(BaseModel):
    """Everything a client needs to drive one native team-inbox chat session."""

    session_id: str
    visitor_token: str
    conversation_id: str | None = None
    # Native widget endpoints the client calls with X-Visitor-Token.
    ws_url: str
    api_base: str


class FiberChatSessionResponse(ChatSessionResponse):
    """Native Team Inbox session plus the committed first visitor message."""

    conversation_id: str
    message_id: str
    resolution_status: str
    replayed: bool
