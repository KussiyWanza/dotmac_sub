from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


class ErpStaffLeaveRestrictionEvent(BaseModel):
    """Normalized owner input used inside Selfcare."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=240)
    restriction_id: str = Field(min_length=1, max_length=200)
    erp_employee_id: str = Field(min_length=1, max_length=200)
    system_user_id: UUID
    effective_from: datetime
    effective_until: datetime | None = None
    status: Literal["active", "cancelled", "revoked"]
    version: int = Field(ge=1)
    updated_at: datetime
    source_system: str = Field(default="dotmac_erp", min_length=1, max_length=80)


class ErpStaffAccountStatusEvent(BaseModel):
    """Normalized owner input used inside Selfcare."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=240)
    erp_employee_id: str = Field(min_length=1, max_length=200)
    system_user_id: UUID
    account_status: Literal["active", "inactive"]
    version: int = Field(ge=1)
    updated_at: datetime
    source_system: str = Field(default="dotmac_erp", min_length=1, max_length=80)
    reason: str | None = Field(default=None, max_length=240)


class ErpLeaveSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["leave_application"]
    id: UUID
    status: str = Field(min_length=1, max_length=30)


def _inclusive_date_range(
    effective_from: date,
    effective_until: date,
    organization_timezone: str,
) -> tuple[datetime, datetime]:
    """Translate inclusive ERP-local dates to half-open UTC timestamps."""

    organization_zone = ZoneInfo(organization_timezone)
    start = datetime.combine(effective_from, time.min, tzinfo=organization_zone)
    end = datetime.combine(
        effective_until + timedelta(days=1),
        time.min,
        tzinfo=organization_zone,
    )
    return start.astimezone(UTC), end.astimezone(UTC)


def _validate_organization_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("organization_timezone must be a valid IANA timezone") from exc
    return value


class ErpStaffLeaveRestrictionWebhook(BaseModel):
    """The authoritative flat ``staff.leave_restriction.v1`` ERP payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["staff.leave_restriction.v1"]
    event_type: Literal["hr.staff_leave_restriction.changed"]
    restriction_id: UUID
    organization_id: UUID
    employee_id: UUID
    person_id: UUID
    selfcare_user_id: UUID | None = None
    source: ErpLeaveSource
    organization_timezone: str = Field(default="UTC", min_length=1, max_length=64)
    effective_from: date
    effective_until: date
    status: Literal["ACTIVE", "CANCELLED"]
    version: int = Field(ge=1)
    updated_at: datetime
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = Field(default=None, max_length=100)

    _valid_organization_timezone = field_validator("organization_timezone")(
        _validate_organization_timezone
    )

    @model_validator(mode="after")
    def _valid_date_range(self) -> ErpStaffLeaveRestrictionWebhook:
        if self.effective_until < self.effective_from:
            raise ValueError("effective_until cannot precede effective_from")
        return self

    def to_owner_event(self, *, event_id: str) -> ErpStaffLeaveRestrictionEvent | None:
        if self.selfcare_user_id is None:
            return None
        effective_from, effective_until = _inclusive_date_range(
            self.effective_from,
            self.effective_until,
            self.organization_timezone,
        )
        return ErpStaffLeaveRestrictionEvent(
            event_id=event_id,
            restriction_id=str(self.restriction_id),
            erp_employee_id=str(self.employee_id),
            system_user_id=self.selfcare_user_id,
            effective_from=effective_from,
            effective_until=effective_until,
            status="active" if self.status == "ACTIVE" else "cancelled",
            version=self.version,
            updated_at=self.updated_at,
        )


class ErpStaffAccountStatusWebhook(BaseModel):
    """The authoritative flat ``staff.account_status.v1`` ERP payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["staff.account_status.v1"]
    event_type: Literal["hr.staff_account_status.changed"]
    projection_id: UUID
    organization_id: UUID
    employee_id: UUID
    person_id: UUID
    selfcare_user_id: UUID | None = None
    erp_employee_status: str = Field(min_length=1, max_length=30)
    state: Literal["ACTIVE", "INACTIVE"]
    source_reason: str = Field(min_length=1, max_length=50)
    ownership: Literal["erp_employee_status"]
    downstream_semantics: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=1)
    updated_at: datetime

    def to_owner_event(self, *, event_id: str) -> ErpStaffAccountStatusEvent | None:
        if self.selfcare_user_id is None:
            return None
        return ErpStaffAccountStatusEvent(
            event_id=event_id,
            erp_employee_id=str(self.employee_id),
            system_user_id=self.selfcare_user_id,
            account_status="active" if self.state == "ACTIVE" else "inactive",
            version=self.version,
            updated_at=self.updated_at,
            reason=self.source_reason,
        )


ErpStaffAccessWebhook = Annotated[
    ErpStaffLeaveRestrictionWebhook | ErpStaffAccountStatusWebhook,
    Field(discriminator="event_type"),
]
ERP_STAFF_ACCESS_WEBHOOK_ADAPTER = TypeAdapter(ErpStaffAccessWebhook)


class ErpStaffLeaveRestrictionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity: Literal["leave_restriction"]
    restriction_id: UUID
    organization_id: UUID
    employee_id: UUID
    person_id: UUID
    selfcare_user_id: UUID | None = None
    leave_application_id: UUID
    organization_timezone: str = Field(default="UTC", min_length=1, max_length=64)
    effective_from: date
    effective_until: date
    status: Literal["ACTIVE", "CANCELLED"]
    source_leave_status: str = Field(min_length=1, max_length=30)
    version: int = Field(ge=1)
    updated_at: datetime
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = Field(default=None, max_length=100)

    _valid_organization_timezone = field_validator("organization_timezone")(
        _validate_organization_timezone
    )

    @model_validator(mode="after")
    def _valid_date_range(self) -> ErpStaffLeaveRestrictionProjection:
        if self.effective_until < self.effective_from:
            raise ValueError("effective_until cannot precede effective_from")
        return self

    def to_owner_event(self) -> ErpStaffLeaveRestrictionEvent | None:
        if self.selfcare_user_id is None:
            return None
        effective_from, effective_until = _inclusive_date_range(
            self.effective_from,
            self.effective_until,
            self.organization_timezone,
        )
        return ErpStaffLeaveRestrictionEvent(
            event_id=f"reconcile:leave:{self.restriction_id}:v{self.version}",
            restriction_id=str(self.restriction_id),
            erp_employee_id=str(self.employee_id),
            system_user_id=self.selfcare_user_id,
            effective_from=effective_from,
            effective_until=effective_until,
            status="active" if self.status == "ACTIVE" else "cancelled",
            version=self.version,
            updated_at=self.updated_at,
        )


class ErpStaffAccountStatusProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity: Literal["account_status"]
    projection_id: UUID
    organization_id: UUID
    employee_id: UUID
    person_id: UUID
    selfcare_user_id: UUID | None = None
    erp_employee_status: str = Field(min_length=1, max_length=30)
    state: Literal["ACTIVE", "INACTIVE"]
    source_reason: str = Field(min_length=1, max_length=50)
    ownership: Literal["erp_employee_status"]
    version: int = Field(ge=1)
    updated_at: datetime

    def to_owner_event(self) -> ErpStaffAccountStatusEvent | None:
        if self.selfcare_user_id is None:
            return None
        return ErpStaffAccountStatusEvent(
            event_id=f"reconcile:account:{self.projection_id}:v{self.version}",
            erp_employee_id=str(self.employee_id),
            system_user_id=self.selfcare_user_id,
            account_status="active" if self.state == "ACTIVE" else "inactive",
            version=self.version,
            updated_at=self.updated_at,
            reason=self.source_reason,
        )


ErpStaffAccessProjectionRecord = Annotated[
    ErpStaffLeaveRestrictionProjection | ErpStaffAccountStatusProjection,
    Field(discriminator="entity"),
]


class ErpStaffAccessProjectionPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["staff.access.projection.v1"]
    entity: Literal["leave_restriction", "account_status"]
    items: tuple[ErpStaffAccessProjectionRecord, ...]

    @model_validator(mode="after")
    def _items_match_page_entity(self) -> ErpStaffAccessProjectionPage:
        if any(item.entity != self.entity for item in self.items):
            raise ValueError("projection item does not match page entity")
        return self


class ErpStaffAccessReceipt(BaseModel):
    event_id: str
    event_type: str
    applied: bool
    replayed: bool
    status: str
