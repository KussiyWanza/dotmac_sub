from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.network import FiberSplicePlanDiffRead, FiberSplicePlanRead


class VendorQuoteCreate(BaseModel):
    project_id: UUID
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    vat_rate_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class VendorQuoteLineCreate(BaseModel):
    item_type: str | None = Field(default=None, max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    cable_type: str | None = Field(default=None, max_length=120)
    fiber_count: int | None = Field(default=None, ge=1)
    splice_count: int | None = Field(default=None, ge=0)
    quantity: Decimal = Field(default=Decimal("1"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0"), ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class VendorQuoteLineUpdate(BaseModel):
    item_type: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    cable_type: str | None = Field(default=None, max_length=120)
    fiber_count: int | None = Field(default=None, ge=1)
    splice_count: int | None = Field(default=None, ge=0)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class VendorRouteRevisionCreate(BaseModel):
    geojson: dict[str, object]
    length_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("geojson")
    @classmethod
    def validate_linestring(cls, value: dict[str, object]) -> dict[str, object]:
        if value.get("type") != "LineString":
            raise ValueError("Route geometry must be a GeoJSON LineString")
        coordinates = value.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise ValueError("Route geometry requires at least two coordinates")

        normalized: list[list[float]] = []
        for coordinate in coordinates:
            if (
                not isinstance(coordinate, (list, tuple))
                or len(coordinate) != 2
                or isinstance(coordinate[0], bool)
                or isinstance(coordinate[1], bool)
            ):
                raise ValueError(
                    "Each route coordinate must contain longitude and latitude"
                )
            try:
                longitude = float(coordinate[0])
                latitude = float(coordinate[1])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Route coordinates must contain numeric values"
                ) from exc
            if not math.isfinite(longitude) or not math.isfinite(latitude):
                raise ValueError("Route coordinates must be finite")
            if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                raise ValueError("Route coordinates are outside valid bounds")
            normalized.append([longitude, latitude])

        return {"type": "LineString", "coordinates": normalized}


class VendorAsBuiltLineCreate(VendorQuoteLineCreate):
    pass


class VendorAsBuiltCreate(BaseModel):
    project_id: UUID
    proposed_revision_id: UUID | None = None
    geojson: dict | None = None
    actual_length_meters: float | None = Field(default=None, ge=0)
    variation_type: str | None = Field(default=None, max_length=40)
    variation_reason: str | None = Field(default=None, max_length=2000)
    work_order_ref: str | None = Field(default=None, max_length=120)
    line_items: list[VendorAsBuiltLineCreate] = Field(default_factory=list)


class VendorSpliceCreate(BaseModel):
    work_order_id: str = Field(min_length=1, max_length=64)
    closure_id: UUID
    from_strand_id: UUID
    from_strand_end: Literal["a", "b"]
    to_strand_id: UUID
    to_strand_end: Literal["a", "b"]
    tray_id: UUID | None = None
    position: int | None = Field(default=None, ge=1)
    splice_type: str = Field(min_length=1, max_length=80)
    loss_db: float | None = Field(default=None, ge=0, le=5)
    note: str | None = Field(default=None, max_length=2000)
    plan_item_id: UUID | None = None


class VendorSplicePlanResponse(BaseModel):
    """The assigned work order's live cut sheet and diff (None when absent)."""

    work_order_id: str
    plan: FiberSplicePlanRead | None = None
    diff: FiberSplicePlanDiffRead | None = None


class VendorCableRegistrationCreate(BaseModel):
    work_order_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    fiber_count: int = Field(ge=1, le=1728)
    segment_type: Literal["feeder", "distribution", "drop"] | None = None
    cable_type: str | None = Field(default=None, max_length=40)
    fibers_per_tube: int | None = Field(default=None, ge=1, le=48)
    color_standard: str | None = Field(default=None, max_length=40)
    length_m: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class VendorCableRegistrationResponse(BaseModel):
    change_request_id: UUID
    status: str
    name: str
    fiber_count: int
    work_order_public_id: str | None = None


class VendorStrandDamageCreate(BaseModel):
    work_order_id: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=2000)
    strand_id: UUID | None = None
    segment_id: UUID | None = None
    tube_number: int | None = Field(default=None, ge=1)


class VendorStrandDamageResponse(BaseModel):
    change_request_ids: list[UUID] = Field(default_factory=list)
    strand_ids: list[UUID] = Field(default_factory=list)
    tube_number: int | None = None
    work_order_public_id: str | None = None


class VendorSpliceProposalCountsRead(BaseModel):
    pending: int
    applied: int
    rejected: int


class VendorJobPlanSummaryRead(BaseModel):
    plan_id: UUID
    status: str
    item_count: int
    executed_count: int
    unexecuted_count: int


class VendorJobEvidenceRead(BaseModel):
    work_order_id: UUID
    work_order_public_id: str
    fiber_test_count: int
    derived_failed_count: int
    assertion_conflict_count: int
    source_observation_count: int
    splice_proposals: VendorSpliceProposalCountsRead
    unplanned_splice_count: int
    plan: VendorJobPlanSummaryRead | None = None
    attachment_count: int
    pending_inventory_proposals: int
    as_built_required: bool
    as_built_satisfied: bool


class VendorStrandColorRead(BaseModel):
    strand_number: int
    color_standard: str
    tube_number: int | None = None
    tube_color: str | None = None
    core_number_in_tube: int
    core_color: str


class VendorSpliceProposalResponse(BaseModel):
    change_request_id: UUID
    status: str
    replayed: bool
    closure_id: UUID
    from_strand_id: UUID
    from_strand_end: Literal["a", "b"]
    to_strand_id: UUID
    to_strand_end: Literal["a", "b"]
    work_order_public_id: str | None = None
    from_strand_colors: VendorStrandColorRead | None = None
    to_strand_colors: VendorStrandColorRead | None = None
    plan_id: UUID | None = None
    plan_item_id: UUID | None = None


class VendorSpliceProposalStatusRead(BaseModel):
    change_request_id: UUID
    status: str
    operation: str
    closure_id: UUID | None = None
    from_strand_id: UUID | None = None
    from_strand_end: Literal["a", "b"] | None = None
    to_strand_id: UUID | None = None
    to_strand_end: Literal["a", "b"] | None = None
    splice_type: str | None = None
    loss_db: float | None = None
    work_order_public_id: str | None = None
    from_strand_colors: VendorStrandColorRead | None = None
    to_strand_colors: VendorStrandColorRead | None = None
    plan_id: UUID | None = None
    plan_item_id: UUID | None = None
    review_notes: str | None = None
    reviewed_at: datetime | None = None
    applied_at: datetime | None = None
    created_at: datetime


class VendorSubmissionConfirm(BaseModel):
    confirmation_token: str = Field(min_length=1, max_length=131_072)


class VendorReview(BaseModel):
    review_notes: str | None = Field(default=None, max_length=2000)


class VendorMaterialReleaseItemCreate(BaseModel):
    """One material line a vendor is asking Dotmac to release.

    ``item_code`` is provider-neutral: Sub does not hold the stock catalogue,
    so the code is correlation evidence for whoever issues the material.
    """

    description: str = Field(min_length=1, max_length=255)
    quantity: int = Field(gt=0)
    unit: str | None = Field(default=None, max_length=40)
    item_code: str | None = Field(default=None, max_length=80)
    notes: str | None = None


class VendorMaterialReleaseCreate(BaseModel):
    project_id: UUID
    items: list[VendorMaterialReleaseItemCreate] = Field(min_length=1)
    notes: str | None = None


class VendorAdvanceCreate(BaseModel):
    project_id: UUID
    amount: Decimal = Field(gt=0)
    reason: str | None = None
