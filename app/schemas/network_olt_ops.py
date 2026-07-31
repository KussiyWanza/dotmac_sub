"""Schemas for OLT operational endpoints — SSH actions, discovery, profiles."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# ── Response wrapper ───────────────────────────────────────────────────


class OltOperationResponse(BaseModel):
    """Standard response for OLT SSH operations."""

    success: bool
    message: str
    data: Any | None = None


class OltAuthorizationAdmissionRead(BaseModel):
    """Typed durable-command evidence for assigned authorization admission."""

    operation_id: UUID | None
    dispatch_id: UUID | None
    waiting: bool
    duplicate: bool


class OltAuthorizationOperationResponse(BaseModel):
    """Typed HTTP response for assigned ONT authorization."""

    success: bool
    message: str
    data: OltAuthorizationAdmissionRead


# ── Request schemas ────────────────────────────────────────────────────


class OltAuthorizeOntRequest(BaseModel):
    ont_id: UUID = Field(
        description=(
            "Exact assigned ONT identity. Assignment-free devices use Commission ONT."
        ),
    )
    fsp: str = Field(description="Frame/Slot/Port e.g. 0/1/0")
    serial_number: str = Field(min_length=8, max_length=32)
    force_reauthorize: bool = False


class OltServicePortCreateRequest(BaseModel):
    fsp: str
    ont_id: int
    gem_index: int
    vlan_id: int
    user_vlan: int | None = None
    tag_transform: str = "translate"


class OltServicePortDeleteRequest(BaseModel):
    index: int


class OltTr069ProfileCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    acs_url: str = Field(min_length=1, max_length=500)
    username: str = ""
    password: str = ""
    inform_interval: int = Field(default=300, ge=60, le=86400)


class OltCliCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=500)


class OltOntStatusBySerialRequest(BaseModel):
    serial_number: str = Field(min_length=1, max_length=64)


# ── Read schemas ───────────────────────────────────────────────────────


class OltDiscoveredOntRead(BaseModel):
    fsp: str
    serial_number: str
    serial_hex: str | None = None
    vendor_id: str | None = None
    model: str | None = None
    software_version: str | None = None
    mac: str | None = None


class OltServicePortRead(BaseModel):
    index: int
    vlan_id: int
    ont_id: int | None = None
    gem_index: int | None = None
    flow_type: str | None = None
    state: str | None = None


class OltProfileRead(BaseModel):
    profile_id: int
    name: str


class OltTr069ProfileRead(BaseModel):
    profile_id: int
    name: str
    acs_url: str | None = None
    username: str | None = None
