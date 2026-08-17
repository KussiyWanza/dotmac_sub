from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.catalog import DunningAction
from app.models.collections import DunningCaseStatus


class DunningCaseBase(BaseModel):
    account_id: UUID
    policy_set_id: UUID | None = None
    status: DunningCaseStatus = DunningCaseStatus.open
    current_step: int | None = None
    started_at: datetime | None = None
    resolved_at: datetime | None = None
    notes: str | None = None


class DunningCaseCreate(DunningCaseBase):
    pass


class DunningCaseUpdate(BaseModel):
    account_id: UUID | None = None
    policy_set_id: UUID | None = None
    status: DunningCaseStatus | None = None
    current_step: int | None = None
    started_at: datetime | None = None
    resolved_at: datetime | None = None
    notes: str | None = None


class DunningCaseRead(DunningCaseBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class DunningActionLogBase(BaseModel):
    case_id: UUID
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    step_day: int | None = None
    action: DunningAction
    outcome: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    executed_at: datetime | None = None


class DunningActionLogCreate(DunningActionLogBase):
    pass


class DunningActionLogUpdate(BaseModel):
    case_id: UUID | None = None
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    step_day: int | None = None
    action: DunningAction | None = None
    outcome: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    executed_at: datetime | None = None


class DunningActionLogRead(DunningActionLogBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    access_consequence_id: UUID | None = None


class DunningRunRequest(BaseModel):
    run_at: datetime | None = None
    dry_run: bool = False


class DunningRunResponse(BaseModel):
    run_at: datetime
    accounts_scanned: int
    cases_created: int
    actions_created: int
    skipped: int
    errors: int = 0


class BillingEnforcementRunRequest(BaseModel):
    run_at: datetime | None = None
    dry_run: bool = False


class BillingEnforcementRunResponse(BaseModel):
    run_at: datetime
    accounts_scanned: int
    cases_created: int
    actions_created: int
    skipped: int
    dunning_accounts_scanned: int
    dunning_cases_created: int
    dunning_actions_created: int
    dunning_skipped: int
    dunning_errors: int = 0
    credit_accounts_scanned: int = 0
    credit_accounts_settled: int = 0
    credit_invoices_touched: int = 0
    credit_settlement_errors: int = 0
    credit_applied: str = "0.00"
