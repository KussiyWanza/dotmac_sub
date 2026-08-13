"""Browser-facing workforce attendance transport schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DashboardAttendanceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    observed_at: datetime | None = None
