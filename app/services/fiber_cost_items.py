"""Authoritative owner for fiber drop-cost components and estimates.

The write commands in this module own one complete transaction: the current
component row, immutable audit evidence, and the durable domain event are
staged together and committed once by ``execute_owner_command``. Read models
remain typed until a web adapter serializes them for HTML or JSON.
"""

from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.fiber_cost_item import FiberCostItem
from app.schemas.fiber_cost_items import (
    CreateFiberCostItemCommand,
    EstimateLine,
    FiberCostEstimate,
    FiberCostItemCode,
    FiberCostItemListState,
    FiberCostItemOutcome,
    FiberCostUnit,
    FiberCostUnitOption,
    FiberPricingState,
    UpdateFiberCostItemCommand,
)
from app.services import settings_spec
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command

OWNER = "network.fiber_cost_items"
WRITE_SCOPE = "network:fiber:write"
WRITE_CONCERN = "fiber drop-cost components and their prices"

_CREATE = OwnerCommandDefinition(
    owner=OWNER,
    concern=WRITE_CONCERN,
    name="create_fiber_cost_item",
)
_UPDATE = OwnerCommandDefinition(
    owner=OWNER,
    concern=WRITE_CONCERN,
    name="update_fiber_cost_item",
)

_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z")
_MAX_AMOUNT = Decimal("999999999999.99")
_MAX_DESCRIPTION_LENGTH = 2000
_MIN_SORT_ORDER = 0
_MAX_SORT_ORDER = 10000


class FiberCostItemError(DomainError):
    """Stable transport-neutral refusal from the fiber-cost owner."""


def _error(code: str, message: str, **details: object) -> FiberCostItemError:
    return FiberCostItemError(
        code=f"{OWNER}.{code}",
        message=message,
        details=details,
    )


def parse_code(raw: str) -> FiberCostItemCode:
    """Normalize and validate an untrusted component code at the adapter edge."""

    normalized = re.sub(r"\s+", "_", raw.strip().lower())
    if not normalized:
        raise _error("code_required", "A code is required.", field="code")
    if len(normalized) > 60 or _CODE_PATTERN.fullmatch(normalized) is None:
        raise _error(
            "invalid_code",
            "Use at most 60 lowercase letters, numbers, and underscores for the code.",
            field="code",
        )
    return FiberCostItemCode(normalized)


def parse_unit(raw: str) -> FiberCostUnit:
    """Validate the closed arithmetic vocabulary at the adapter edge."""

    try:
        return FiberCostUnit(raw)
    except ValueError as exc:
        raise _error(
            "unknown_unit",
            f"{raw!r} is not a unit this estimator can apply.",
            field="unit",
        ) from exc


def parse_amount(raw: str | None) -> Decimal | None:
    """Return a finite non-negative price, or ``None`` for not priced."""

    text = (raw or "").strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
        if not amount.is_finite():
            raise ArithmeticError
        amount = amount.quantize(Decimal("0.01"))
    except (ArithmeticError, ValueError) as exc:
        raise _error(
            "invalid_amount",
            f"{text!r} is not a valid monetary amount.",
            field="amount",
        ) from exc
    if amount < 0:
        raise _error(
            "negative_amount",
            "A cost cannot be negative.",
            field="amount",
        )
    if amount > _MAX_AMOUNT:
        raise _error(
            "amount_too_large",
            "The cost exceeds the supported monetary range.",
            field="amount",
        )
    return amount


def _validated_amount(value: Decimal | None) -> Decimal | None:
    """Defend the owner boundary even when a non-web caller builds a command."""

    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise _error(
            "invalid_amount",
            "The cost must be a finite decimal monetary amount.",
            field="amount",
        )
    try:
        amount = value.quantize(Decimal("0.01"))
    except ArithmeticError as exc:
        raise _error(
            "invalid_amount",
            "The cost must be a finite decimal monetary amount.",
            field="amount",
        ) from exc
    if amount < 0:
        raise _error(
            "negative_amount",
            "A cost cannot be negative.",
            field="amount",
        )
    if amount > _MAX_AMOUNT:
        raise _error(
            "amount_too_large",
            "The cost exceeds the supported monetary range.",
            field="amount",
        )
    return amount


def _validated_label(value: str) -> str:
    label = value.strip()
    if not label:
        raise _error("label_required", "A label is required.", field="label")
    if len(label) > 120:
        raise _error(
            "label_too_long",
            "The label cannot exceed 120 characters.",
            field="label",
        )
    return label


def _validated_description(value: str | None) -> str | None:
    description = (value or "").strip() or None
    if description is not None and len(description) > _MAX_DESCRIPTION_LENGTH:
        raise _error(
            "description_too_long",
            f"The description cannot exceed {_MAX_DESCRIPTION_LENGTH} characters.",
            field="description",
        )
    return description


def _validated_sort_order(value: int) -> int:
    if isinstance(value, bool) or not _MIN_SORT_ORDER <= value <= _MAX_SORT_ORDER:
        raise _error(
            "invalid_sort_order",
            f"Sort order must be between {_MIN_SORT_ORDER} and {_MAX_SORT_ORDER}.",
            field="sort_order",
        )
    return value


def _validate_command_actor(
    command: CreateFiberCostItemCommand | UpdateFiberCostItemCommand,
) -> None:
    if command.context.scope != WRITE_SCOPE:
        raise _error(
            "invalid_scope",
            "The fiber-cost command has an invalid authorization scope.",
        )
    expected_actor = f"{command.actor_type.value}:{command.actor_id}"
    if command.context.actor != expected_actor:
        raise _error(
            "invalid_actor",
            "The command actor does not match its audit provenance.",
        )


def _active_items(db: Session) -> tuple[FiberCostItem, ...]:
    return tuple(
        db.scalars(
            select(FiberCostItem)
            .where(FiberCostItem.is_active.is_(True))
            .order_by(FiberCostItem.sort_order, FiberCostItem.label)
        )
    )


def _all_items(db: Session) -> tuple[FiberCostItem, ...]:
    return tuple(
        db.scalars(
            select(FiberCostItem).order_by(
                FiberCostItem.sort_order, FiberCostItem.label
            )
        )
    )


def _currency(db: Session) -> str:
    return (
        str(
            settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
            or ""
        ).strip()
        or "NGN"
    )


def estimate_for_distance(
    db: Session,
    distance_meters: Decimal,
) -> FiberCostEstimate:
    """Price one non-negative finite drop distance from committed components."""

    if not distance_meters.is_finite() or distance_meters < 0:
        raise _error(
            "invalid_distance",
            "Distance must be a finite non-negative number of metres.",
            field="distance_meters",
        )

    lines: list[EstimateLine] = []
    unpriced: list[FiberCostItemCode] = []
    for item in _active_items(db):
        code = FiberCostItemCode(item.code)
        if item.amount is None:
            unpriced.append(code)
            continue
        quantity = (
            distance_meters if item.unit is FiberCostUnit.PER_METER else Decimal(1)
        )
        lines.append(
            EstimateLine(
                code=code,
                label=item.label,
                unit=item.unit,
                amount=item.amount,
                quantity=quantity,
                total=(item.amount * quantity).quantize(Decimal("0.01")),
            )
        )

    return FiberCostEstimate(
        currency=_currency(db),
        lines=tuple(lines),
        total=sum((line.total for line in lines), Decimal("0.00")),
        unpriced=tuple(unpriced),
    )


def pricing_state(db: Session) -> FiberPricingState:
    """Describe whether the current committed configuration can estimate."""

    items = _active_items(db)
    return FiberPricingState(
        currency=_currency(db),
        item_count=len(items),
        unpriced=tuple(
            FiberCostItemCode(item.code) for item in items if item.amount is None
        ),
    )


def _outcome(item: FiberCostItem) -> FiberCostItemOutcome:
    return FiberCostItemOutcome(
        item_id=item.id,
        code=FiberCostItemCode(item.code),
        label=item.label,
        unit=item.unit,
        amount=item.amount,
        is_active=item.is_active,
        sort_order=item.sort_order,
        description=item.description,
        version=item.version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def list_state(db: Session) -> FiberCostItemListState:
    """Typed state for the CRUD screen."""

    return FiberCostItemListState(
        items=tuple(_outcome(item) for item in _all_items(db)),
        units=tuple(
            FiberCostUnitOption(
                value=member,
                label=member.name.replace("_", " ").title(),
            )
            for member in FiberCostUnit
        ),
        pricing=pricing_state(db),
    )


def _audit_values(outcome: FiberCostItemOutcome) -> dict[str, object]:
    return {
        "code": outcome.code.value,
        "label": outcome.label,
        "unit": outcome.unit.value,
        "amount": str(outcome.amount) if outcome.amount is not None else None,
        "is_active": outcome.is_active,
        "sort_order": outcome.sort_order,
        "description": outcome.description,
        "version": outcome.version,
    }


def _stage_audit(
    db: Session,
    *,
    command: CreateFiberCostItemCommand | UpdateFiberCostItemCommand,
    action: str,
    outcome: FiberCostItemOutcome,
    before: FiberCostItemOutcome | None,
) -> None:
    metadata: dict[str, object] = {
        "owner": OWNER,
        "before": _audit_values(before) if before is not None else None,
        "after": _audit_values(outcome),
        "command_id": str(command.context.command_id),
        "command_scope": command.context.scope,
        "command_reason": command.context.reason,
    }
    stage_audit_event(
        db,
        action=action,
        entity_type="fiber_cost_item",
        entity_id=str(outcome.item_id),
        actor_type=command.actor_type,
        actor_id=str(command.actor_id),
        request_id=str(command.context.correlation_id),
        metadata=metadata,
    )


def _announce(
    db: Session,
    *,
    context_actor: str,
    outcome: FiberCostItemOutcome,
    change: str,
) -> None:
    """Stage a non-price-bearing change signal in the owner transaction."""

    emit_event(
        db,
        EventType.fiber_cost_item_changed,
        {
            "code": outcome.code.value,
            "change": change,
            "unit": outcome.unit.value,
            "is_active": outcome.is_active,
            "is_priced": outcome.amount is not None,
            "version": outcome.version,
        },
        actor=context_actor,
    )


def _is_duplicate_code_error(exc: IntegrityError) -> bool:
    text = str(getattr(exc, "orig", exc)).lower()
    return "uq_fiber_cost_items_code" in text or (
        "fiber_cost_items" in text and "code" in text and "unique" in text
    )


def create_item(
    db: Session,
    command: CreateFiberCostItemCommand,
) -> FiberCostItemOutcome:
    """Create one component and its event/audit evidence atomically."""

    try:
        return execute_owner_command(
            db,
            definition=_CREATE,
            context=command.context,
            operation=lambda: _create_item(db, command),
        )
    except IntegrityError as exc:
        if _is_duplicate_code_error(exc):
            raise _error(
                "duplicate_code",
                f"A cost item with code {command.code.value!r} already exists.",
                field="code",
            ) from exc
        raise


def _create_item(
    db: Session,
    command: CreateFiberCostItemCommand,
) -> FiberCostItemOutcome:
    _validate_command_actor(command)
    code = parse_code(command.code.value)
    label = _validated_label(command.label)
    amount = _validated_amount(command.amount)
    description = _validated_description(command.description)
    sort_order = _validated_sort_order(command.sort_order)

    existing = db.scalar(
        select(FiberCostItem.id).where(FiberCostItem.code == code.value)
    )
    if existing is not None:
        raise _error(
            "duplicate_code",
            f"A cost item with code {code.value!r} already exists.",
            field="code",
        )

    item = FiberCostItem(
        code=code.value,
        label=label,
        unit=command.unit,
        amount=amount,
        sort_order=sort_order,
        description=description,
        version=1,
    )
    db.add(item)
    db.flush()
    outcome = _outcome(item)
    _stage_audit(
        db,
        command=command,
        action="fiber_cost_item.created",
        outcome=outcome,
        before=None,
    )
    _announce(
        db,
        context_actor=command.context.actor,
        outcome=outcome,
        change="created",
    )
    return outcome


def update_item(
    db: Session,
    command: UpdateFiberCostItemCommand,
) -> FiberCostItemOutcome:
    """Replace one reviewed item version, rejecting stale forms."""

    return execute_owner_command(
        db,
        definition=_UPDATE,
        context=command.context,
        operation=lambda: _update_item(db, command),
    )


def _update_item(
    db: Session,
    command: UpdateFiberCostItemCommand,
) -> FiberCostItemOutcome:
    _validate_command_actor(command)
    if command.expected_version < 1:
        raise _error(
            "invalid_version",
            "The expected item version must be positive.",
            field="expected_version",
        )

    item = db.scalar(
        select(FiberCostItem)
        .where(FiberCostItem.id == command.item_id)
        .with_for_update()
    )
    if item is None:
        raise _error("not_found", "Cost item not found.")
    if item.version != command.expected_version:
        raise _error(
            "stale_version",
            "This cost item changed after the form was opened; review the new values.",
            expected_version=command.expected_version,
            current_version=item.version,
        )

    before = _outcome(item)
    item.label = _validated_label(command.label)
    item.unit = command.unit
    item.amount = _validated_amount(command.amount)
    item.is_active = command.is_active
    item.sort_order = _validated_sort_order(command.sort_order)
    item.description = _validated_description(command.description)
    item.version += 1
    db.flush()

    outcome = _outcome(item)
    _stage_audit(
        db,
        command=command,
        action="fiber_cost_item.updated",
        outcome=outcome,
        before=before,
    )
    _announce(
        db,
        context_actor=command.context.actor,
        outcome=outcome,
        change="updated",
    )
    return outcome
