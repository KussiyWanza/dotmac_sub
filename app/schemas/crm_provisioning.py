from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.subscriber import Gender


class CRMSubscriberProvisionRequest(BaseModel):
    """Validated CRM command payload for one new Sub customer."""

    model_config = ConfigDict(extra="forbid")

    crm_person_id: str = Field(min_length=1, max_length=80)
    crm_project_id: str | None = Field(default=None, max_length=80)
    crm_quote_id: str | None = Field(default=None, max_length=80)
    crm_sales_order_id: str | None = Field(default=None, max_length=80)
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    date_of_birth: date | None = None
    gender: Gender = Gender.unknown
    nin: str | None = Field(default=None, max_length=11)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
