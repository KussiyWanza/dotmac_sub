"""The owner of what a fiber drop installation costs.

One place decides which components exist, what they cost, and whether the
estimate can be produced at all. Before this, that was split across a settings
module, a service reader and a template's JavaScript, and none of them owned it
— which is how the estimate came to quote ₦85 for an ONT with nothing to say so.

The estimate itself is deliberately computed HERE rather than in the browser.
The old page shipped four numbers to JavaScript and did the arithmetic there, so
the breakdown a user saw was assembled by the layer least able to explain it and
could not be tested without a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.fiber_cost_item import FiberCostItem, FiberCostUnit
from app.services import settings_spec


@dataclass(frozen=True, slots=True)
class EstimateLine:
    """One priced component applied to one route."""

    code: str
    label: str
    unit: FiberCostUnit
    amount: Decimal
    #: What the unit multiplied by — metres for `per_meter`, 1 for `flat`. Kept
    #: so the screen can show the working rather than only the answer.
    quantity: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class FiberCostEstimate:
    """A complete estimate, or an explicit statement that there cannot be one."""

    currency: str
    lines: tuple[EstimateLine, ...]
    total: Decimal
    #: Active components with no price. Non-empty means the estimate is
    #: incomplete and the screen must say so rather than quietly omit them.
    unpriced: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.unpriced and bool(self.lines)


def active_items(db: Session) -> list[FiberCostItem]:
    """Every component the estimator applies, in display order."""

    return list(
        db.scalars(
            select(FiberCostItem)
            .where(FiberCostItem.is_active.is_(True))
            .order_by(FiberCostItem.sort_order, FiberCostItem.label)
        )
    )


def all_items(db: Session) -> list[FiberCostItem]:
    """Every component, active or not — the CRUD screen's list."""

    return list(
        db.scalars(
            select(FiberCostItem).order_by(
                FiberCostItem.sort_order, FiberCostItem.label
            )
        )
    )


def estimate_for_distance(db: Session, distance_meters: float) -> FiberCostEstimate:
    """Price one drop of `distance_meters`.

    An active component with no amount does NOT contribute and is named in
    `unpriced`. Treating it as zero would produce a total that looks like an
    answer, which is the failure this whole change exists to remove: a number
    nobody chose, presented as a price.
    """

    currency = (
        str(
            settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
            or ""
        ).strip()
        or "NGN"
    )
    metres = Decimal(str(max(distance_meters, 0)))

    lines: list[EstimateLine] = []
    unpriced: list[str] = []
    for item in active_items(db):
        if item.amount is None:
            unpriced.append(item.code)
            continue
        quantity = metres if item.unit is FiberCostUnit.PER_METER else Decimal(1)
        lines.append(
            EstimateLine(
                code=item.code,
                label=item.label,
                unit=item.unit,
                amount=item.amount,
                quantity=quantity,
                total=(item.amount * quantity).quantize(Decimal("0.01")),
            )
        )

    return FiberCostEstimate(
        currency=currency,
        lines=tuple(lines),
        total=sum((line.total for line in lines), Decimal("0.00")),
        unpriced=tuple(unpriced),
    )


def estimate_as_dict(db: Session, distance_meters: float) -> dict[str, object]:
    """The estimate as the map screen consumes it.

    A dict rather than the dataclass because this crosses into a template, and
    the shape it crosses with is the thing a screen must not invent for itself.
    """

    estimate = estimate_for_distance(db, distance_meters)
    return {
        "currency": estimate.currency,
        "is_complete": estimate.is_complete,
        "unpriced": list(estimate.unpriced),
        "total": str(estimate.total),
        "lines": [
            {
                "code": line.code,
                "label": line.label,
                "unit": line.unit.value,
                "amount": str(line.amount),
                "quantity": str(line.quantity),
                "total": str(line.total),
            }
            for line in estimate.lines
        ],
    }


def pricing_state(db: Session) -> dict[str, object]:
    """What the map page needs to describe its own pricing, without prices.

    The page renders whether an estimate is possible and which components are
    missing a price; the amounts themselves reach it only inside an estimate it
    asked for, alongside the distance they price. That keeps the arithmetic in
    one place and stops a screen from quietly acquiring its own copy of the
    rule — which is how the four hardcoded components ended up living in a
    template in the first place.
    """

    items = active_items(db)
    unpriced = [item.code for item in items if item.amount is None]
    currency = (
        str(
            settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
            or ""
        ).strip()
        or "NGN"
    )
    return {
        "currency": currency,
        "item_count": len(items),
        "unpriced": unpriced,
        # False when a component is active but unpriced, or when there are no
        # components at all. Either way the screen must say so rather than show
        # a total assembled from the components that happen to have a price.
        "is_complete": bool(items) and not unpriced,
    }


class FiberCostItemError(ValueError):
    """A cost item could not be created or changed as asked."""


def _parse_amount(raw: str | None) -> Decimal | None:
    """A price, or None for "not priced yet".

    An empty field means unpriced, and that is NOT zero: a free component is a
    real answer an operator may give, and only one of the two should leave the
    estimate incomplete.
    """

    text = (raw or "").strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (ArithmeticError, ValueError) as exc:
        raise FiberCostItemError(f"{text!r} is not a number") from exc
    if amount < 0:
        raise FiberCostItemError("a cost cannot be negative")
    return amount.quantize(Decimal("0.01"))


def create_item(
    db: Session,
    *,
    code: str,
    label: str,
    unit: str,
    amount: str | None = None,
    sort_order: int = 100,
    description: str | None = None,
) -> FiberCostItem:
    """Add a component. The code is its stable identity and cannot repeat."""

    normalised = code.strip().lower().replace(" ", "_")
    if not normalised:
        raise FiberCostItemError("a code is required")
    if not label.strip():
        raise FiberCostItemError("a label is required")
    try:
        parsed_unit = FiberCostUnit(unit)
    except ValueError as exc:
        raise FiberCostItemError(
            f"{unit!r} is not a unit this estimator can apply"
        ) from exc
    if db.scalar(select(FiberCostItem).where(FiberCostItem.code == normalised)):
        raise FiberCostItemError(f"a cost item with code {normalised!r} already exists")

    item = FiberCostItem(
        code=normalised,
        label=label.strip(),
        unit=parsed_unit,
        amount=_parse_amount(amount),
        sort_order=sort_order,
        description=(description or "").strip() or None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_item(
    db: Session,
    item_id: str,
    *,
    label: str | None = None,
    unit: str | None = None,
    amount: str | None = None,
    is_active: bool | None = None,
    sort_order: int | None = None,
    description: str | None = None,
) -> FiberCostItem:
    """Change a component. `code` is deliberately not editable — see the model."""

    item = db.get(FiberCostItem, item_id)
    if item is None:
        raise FiberCostItemError("cost item not found")
    if label is not None:
        if not label.strip():
            raise FiberCostItemError("a label is required")
        item.label = label.strip()
    if unit is not None:
        try:
            item.unit = FiberCostUnit(unit)
        except ValueError as exc:
            raise FiberCostItemError(
                f"{unit!r} is not a unit this estimator can apply"
            ) from exc
    if amount is not None:
        item.amount = _parse_amount(amount)
    if is_active is not None:
        item.is_active = is_active
    if sort_order is not None:
        item.sort_order = sort_order
    if description is not None:
        item.description = description.strip() or None
    db.commit()
    db.refresh(item)
    return item


def list_data(db: Session) -> dict[str, object]:
    """The CRUD screen's state, including why an estimate may be impossible."""

    items = all_items(db)
    state = pricing_state(db)
    return {
        "items": items,
        "units": [
            (member.value, member.name.replace("_", " ").title())
            for member in FiberCostUnit
        ],
        "currency": state["currency"],
        "is_complete": state["is_complete"],
        "unpriced": state["unpriced"],
    }
