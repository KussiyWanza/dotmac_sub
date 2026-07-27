from __future__ import annotations

import math
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


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


class VendorSubmissionConfirm(BaseModel):
    confirmation_token: str = Field(min_length=1, max_length=131_072)


class VendorReview(BaseModel):
    review_notes: str | None = Field(default=None, max_length=2000)
