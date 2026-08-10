from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ErpMaterialStatusLine(BaseModel):
    sequence: int = Field(ge=1)
    serial_numbers: tuple[str, ...] = ()


class ErpMaterialStatusWebhook(BaseModel):
    omni_id: UUID
    request_id: str = Field(min_length=1, max_length=120)
    request_number: str | None = Field(default=None, max_length=120)
    old_status: str | None = Field(default=None, max_length=40)
    new_status: str = Field(min_length=1, max_length=40)
    updated_at: datetime | None = None
    items: tuple[ErpMaterialStatusLine, ...] = ()


class ErpMaterialStatusReceipt(BaseModel):
    material_request_id: UUID
    status: str
    replayed: bool
