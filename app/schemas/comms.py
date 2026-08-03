from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.models.comms import (
    CustomerNotificationStatus,
    SurveyStatus,
    SurveyTriggerType,
)


class CustomerNotificationBase(BaseModel):
    entity_type: str = Field(min_length=1, max_length=40)
    entity_id: UUID
    subscriber_id: UUID | None = None
    channel: str = Field(min_length=1, max_length=40)
    recipient: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1)
    status: CustomerNotificationStatus = CustomerNotificationStatus.pending
    sent_at: datetime | None = None


class CustomerNotificationCreate(CustomerNotificationBase):
    pass


class CustomerNotificationUpdate(BaseModel):
    status: CustomerNotificationStatus | None = None
    sent_at: datetime | None = None


class CustomerNotificationRead(CustomerNotificationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class EtaUpdateBase(BaseModel):
    service_order_id: UUID = Field(
        validation_alias=AliasChoices("service_order_id", "work_order_id")
    )
    eta_at: datetime
    note: str | None = None


class EtaUpdateCreate(EtaUpdateBase):
    pass


class EtaUpdateRead(EtaUpdateBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class SurveyQuestionType(StrEnum):
    rating = "rating"
    nps = "nps"
    multiple_choice = "multiple_choice"
    free_text = "free_text"


_QUESTION_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PUBLIC_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_public_slug(value: object) -> str | None:
    """Normalize only approved word separators; malformed hyphens stay invalid."""

    if value is None:
        return None
    normalized = re.sub(r"[\s_]+", "-", str(value).strip().lower())
    if not normalized:
        return None
    return normalized


class SurveyQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=80)
    type: SurveyQuestionType = SurveyQuestionType.rating
    label: str = Field(min_length=1, max_length=500)
    required: bool = True
    options: list[str] | None = None

    @field_validator("key", "label", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not _QUESTION_KEY_PATTERN.fullmatch(value):
            raise ValueError(
                "Use a letter first, followed only by letters, numbers, hyphens, "
                "or underscores."
            )
        return value

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        if self.type is not SurveyQuestionType.multiple_choice:
            self.options = None
            return self
        if self.options is None or not 2 <= len(self.options) <= 50:
            raise ValueError("Multiple Choice questions require 2 to 50 options.")
        cleaned: list[str] = []
        seen: set[str] = set()
        for option in self.options:
            value = option.strip()
            if not value:
                raise ValueError("Multiple Choice options cannot be blank.")
            if len(value) > 200:
                raise ValueError(
                    "Multiple Choice options cannot exceed 200 characters."
                )
            duplicate_key = value.casefold()
            if duplicate_key in seen:
                raise ValueError("Multiple Choice options must be unique.")
            seen.add(duplicate_key)
            cleaned.append(value)
        self.options = cleaned
        return self


def _validate_unique_question_keys(
    questions: list[SurveyQuestion] | None,
) -> list[SurveyQuestion] | None:
    if questions is None:
        return None
    seen: set[str] = set()
    for question in questions:
        if question.key in seen:
            raise ValueError(f'Question key "{question.key}" is duplicated.')
        seen.add(question.key)
    return questions


class SurveyCreate(BaseModel):
    """Typed editable fields; lifecycle, creator, and metrics are owner defaults."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    trigger_type: SurveyTriggerType = SurveyTriggerType.manual
    public_slug: str | None = Field(default=None, max_length=120)
    thank_you_message: str | None = None
    questions: list[SurveyQuestion] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("description", "thank_you_message", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator("public_slug", mode="before")
    @classmethod
    def normalize_slug(cls, value: object) -> str | None:
        return normalize_public_slug(value)

    @field_validator("public_slug")
    @classmethod
    def validate_slug(cls, value: str | None) -> str | None:
        if value is not None and not _PUBLIC_SLUG_PATTERN.fullmatch(value):
            raise ValueError("Use lowercase letters, numbers, and single hyphens only.")
        return value

    @field_validator("questions")
    @classmethod
    def validate_question_keys(
        cls, value: list[SurveyQuestion]
    ) -> list[SurveyQuestion]:
        return _validate_unique_question_keys(value) or []


class SurveyBase(BaseModel):
    name: str
    description: str | None = None
    trigger_type: SurveyTriggerType
    public_slug: str | None = None
    thank_you_message: str | None = None
    questions: list[SurveyQuestion]
    status: SurveyStatus
    is_active: bool


class SurveyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    trigger_type: SurveyTriggerType | None = None
    public_slug: str | None = Field(default=None, max_length=120)
    thank_you_message: str | None = None
    questions: list[SurveyQuestion] | None = None
    is_active: bool | None = None

    @field_validator("name", mode="before")
    @classmethod
    def strip_optional_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("description", "thank_you_message", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator("public_slug", mode="before")
    @classmethod
    def normalize_slug(cls, value: object) -> str | None:
        return normalize_public_slug(value)

    @field_validator("public_slug")
    @classmethod
    def validate_slug(cls, value: str | None) -> str | None:
        if value is not None and not _PUBLIC_SLUG_PATTERN.fullmatch(value):
            raise ValueError("Use lowercase letters, numbers, and single hyphens only.")
        return value

    @field_validator("questions")
    @classmethod
    def validate_question_keys(
        cls, value: list[SurveyQuestion] | None
    ) -> list[SurveyQuestion] | None:
        return _validate_unique_question_keys(value)


class SurveyRead(SurveyBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_by_id: UUID | None
    expires_at: datetime | None
    segment_filter: dict[str, object] | None
    total_invited: int
    total_responses: int
    avg_rating: float | None
    nps_score: float | None
    created_at: datetime
    updated_at: datetime


class SurveyResponseBase(BaseModel):
    survey_id: UUID
    invitation_id: UUID | None = None
    work_order_id: UUID | None = None
    ticket_id: UUID | None = None
    responses: dict[str, str] | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    nps_value: int | None = Field(default=None, ge=0, le=10)


class SurveyResponseCreate(SurveyResponseBase):
    pass


class SurveyResponseRead(SurveyResponseBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
