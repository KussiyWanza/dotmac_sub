"""Browser-facing workforce attendance transport schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DashboardAttendanceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    observed_at: datetime | None = None


class FieldAttendanceLocation(BaseModel):
    """Fresh device observation submitted by the authenticated field app."""

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    observed_at: datetime


class FieldAttendanceRead(BaseModel):
    """Provider-neutral attendance projection returned to a field client."""

    model_config = ConfigDict(extra="forbid")

    state: Literal[
        "not_checked_in",
        "checked_in",
        "checked_out",
        "ineligible",
    ]
    attendance_date: str
    timezone: str
    check_in_at: datetime | None = None
    check_out_at: datetime | None = None
    working_hours: Decimal | None = None
    status: str | None = None
    allowed_actions: tuple[Literal["check_in", "check_out"], ...]
    reason: str | None = None
    resolution: Literal["direct", "reconciled"] | None = None
