"""Typed ERP-owned expense form and payment-destination observations."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExpenseDestinationMode(StrEnum):
    ERP_PROFILE = "erp_profile"
    EXPENSE_OVERRIDE = "expense_override"


class ExpenseApproverOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_id: UUID
    display_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=255)


class ExpenseBankOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    bank_code: str = Field(min_length=2, max_length=20)
    bank_name: str = Field(min_length=1, max_length=100)


class ExpenseProfileDestination(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool
    bank_code: str | None = None
    bank_name: str | None = None
    masked_account_number: str | None = None
    beneficiary_name: str | None = None


class VerifyExpenseDestination(BaseModel):
    """Sensitive input used once by the server-side ERP connector."""

    model_config = ConfigDict(frozen=True)

    requested_by_email: str = Field(min_length=3, max_length=255)
    source_claim_id: UUID
    mode: ExpenseDestinationMode
    bank_code: str | None = Field(default=None, min_length=2, max_length=20)
    account_number: str | None = Field(default=None, min_length=6, max_length=30)
    beneficiary_name: str | None = Field(default=None, min_length=2, max_length=150)

    @model_validator(mode="after")
    def validate_override(self) -> VerifyExpenseDestination:
        values = (self.bank_code, self.account_number, self.beneficiary_name)
        if self.mode is ExpenseDestinationMode.EXPENSE_OVERRIDE and any(
            not str(value or "").strip() for value in values
        ):
            raise ValueError("Bank, account number, and beneficiary name are required")
        if self.mode is ExpenseDestinationMode.ERP_PROFILE and any(
            value is not None for value in values
        ):
            raise ValueError("ERP profile mode does not accept replacement details")
        return self


class VerifiedExpenseDestination(BaseModel):
    """ERP-verified destination safe for masked display and later submission."""

    model_config = ConfigDict(frozen=True)

    destination_token: str = Field(min_length=20, max_length=4096, repr=False)
    mode: ExpenseDestinationMode
    bank_code: str = Field(min_length=2, max_length=20)
    bank_name: str = Field(min_length=1, max_length=100)
    masked_account_number: str = Field(min_length=6, max_length=30)
    verified_beneficiary_name: str = Field(min_length=1, max_length=150)
    verified_at: datetime
    expires_at: datetime


class InspectExpenseDestination(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_by_email: str = Field(min_length=3, max_length=255)
    source_claim_id: UUID
    destination_token: str = Field(min_length=20, max_length=4096, repr=False)
