"""Typed contracts for fiber drop-cost configuration and estimation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.models.audit import AuditActorType
from app.schemas.fiber_cost_calculation import FiberCostUnit
from app.services.owner_commands import CommandContext


@dataclass(frozen=True, slots=True)
class FiberCostItemCode:
    """Stable normalized identity for one estimator component."""

    value: str


@dataclass(frozen=True, slots=True)
class CreateFiberCostItemCommand:
    context: CommandContext
    actor_id: UUID
    actor_type: AuditActorType
    code: FiberCostItemCode
    label: str
    unit: FiberCostUnit
    amount: Decimal | None
    sort_order: int
    description: str | None


@dataclass(frozen=True, slots=True)
class UpdateFiberCostItemCommand:
    context: CommandContext
    actor_id: UUID
    actor_type: AuditActorType
    item_id: UUID
    expected_version: int
    label: str
    unit: FiberCostUnit
    amount: Decimal | None
    is_active: bool
    sort_order: int
    description: str | None


@dataclass(frozen=True, slots=True)
class FiberCostItemOutcome:
    item_id: UUID
    code: FiberCostItemCode
    label: str
    unit: FiberCostUnit
    amount: Decimal | None
    is_active: bool
    sort_order: int
    description: str | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EstimateLine:
    """One priced component applied to one route."""

    code: FiberCostItemCode
    label: str
    unit: FiberCostUnit
    amount: Decimal
    quantity: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class FiberCostEstimate:
    """A complete estimate, or an explicit statement that there cannot be one."""

    currency: str
    lines: tuple[EstimateLine, ...]
    total: Decimal
    unpriced: tuple[FiberCostItemCode, ...]

    @property
    def is_complete(self) -> bool:
        return not self.unpriced and bool(self.lines)


@dataclass(frozen=True, slots=True)
class FiberPricingState:
    currency: str
    item_count: int
    unpriced: tuple[FiberCostItemCode, ...]

    @property
    def is_complete(self) -> bool:
        return self.item_count > 0 and not self.unpriced


@dataclass(frozen=True, slots=True)
class FiberCostUnitOption:
    value: FiberCostUnit
    label: str


@dataclass(frozen=True, slots=True)
class FiberCostItemListState:
    items: tuple[FiberCostItemOutcome, ...]
    units: tuple[FiberCostUnitOption, ...]
    pricing: FiberPricingState
