"""Typed ERP catalogue projection and Sub field-eligibility owner."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.field_material import FieldInventoryItem, FieldInventoryWarehouse
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

SOURCE_SYSTEM = "dotmac_erp"


@dataclass(frozen=True, slots=True)
class ErpMaterialItemObservation:
    source_item_id: str
    sku: str
    name: str
    description: str | None
    unit: str | None
    category_code: str | None
    category_name: str | None
    source_is_active: bool
    track_serial_numbers: bool
    source_updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ErpWarehouseObservation:
    source_warehouse_id: str
    code: str
    name: str
    source_is_active: bool


@dataclass(frozen=True, slots=True)
class ApplyMaterialCatalogProjection:
    context: CommandContext
    observed_at: datetime
    items: tuple[ErpMaterialItemObservation, ...]
    warehouses: tuple[ErpWarehouseObservation, ...]
    complete_scan: bool


@dataclass(frozen=True, slots=True)
class MaterialCatalogProjectionOutcome:
    items_created: int
    items_updated: int
    items_deactivated: int
    warehouses_created: int
    warehouses_updated: int
    warehouses_deactivated: int


@dataclass(frozen=True, slots=True)
class MaterialCatalogItemView:
    id: UUID
    sku: str
    name: str
    category_name: str | None
    unit: str | None
    source_is_active: bool
    field_request_eligible: bool
    track_serial_numbers: bool
    last_synced_at: datetime | None


@dataclass(frozen=True, slots=True)
class MaterialWarehouseView:
    code: str
    name: str
    source_is_active: bool
    last_synced_at: datetime


@dataclass(frozen=True, slots=True)
class SearchEligibleMaterialItems:
    query: str
    limit: int = 20


@dataclass(frozen=True, slots=True)
class EligibleMaterialItemMatch:
    id: UUID
    label: str
    sku: str | None
    name: str
    category_name: str | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class SetMaterialEligibility:
    context: CommandContext
    item_id: UUID
    eligible: bool
    reason: str


class MaterialCatalogError(DomainError):
    pass


_PROJECT = OwnerCommandDefinition(
    owner="operations.material_catalog",
    concern="ERP material catalogue and warehouse projection",
    name="apply_material_catalog_projection",
)
_ELIGIBILITY = OwnerCommandDefinition(
    owner="operations.material_catalog",
    concern="field material request eligibility",
    name="set_material_eligibility",
)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _validate_snapshot(command: ApplyMaterialCatalogProjection) -> None:
    item_ids = [item.source_item_id for item in command.items]
    skus = [item.sku.casefold() for item in command.items]
    warehouse_ids = [row.source_warehouse_id for row in command.warehouses]
    codes = [row.code.casefold() for row in command.warehouses]
    if not command.observed_at.tzinfo:
        raise MaterialCatalogError(
            code="operations.material_catalog.naive_observation",
            message="ERP catalogue observation time must include a timezone.",
        )
    if any(not value.strip() for value in (*item_ids, *skus, *warehouse_ids, *codes)):
        raise MaterialCatalogError(
            code="operations.material_catalog.invalid_identity",
            message="ERP catalogue identities and codes must not be blank.",
        )
    if len(item_ids) != len(set(item_ids)) or len(skus) != len(set(skus)):
        raise MaterialCatalogError(
            code="operations.material_catalog.duplicate_item",
            message="ERP catalogue snapshot contains duplicate item identity or SKU.",
        )
    if len(warehouse_ids) != len(set(warehouse_ids)) or len(codes) != len(set(codes)):
        raise MaterialCatalogError(
            code="operations.material_catalog.duplicate_warehouse",
            message="ERP warehouse snapshot contains duplicate identity or code.",
        )


def apply_material_catalog_projection(
    db: Session, command: ApplyMaterialCatalogProjection
) -> MaterialCatalogProjectionOutcome:
    _validate_snapshot(command)

    def operation() -> MaterialCatalogProjectionOutcome:
        existing_items = {
            row.source_item_id: row
            for row in db.scalars(
                select(FieldInventoryItem)
                .where(FieldInventoryItem.source_system == SOURCE_SYSTEM)
                .with_for_update()
            ).all()
            if row.source_item_id
        }
        existing_warehouses = {
            row.source_warehouse_id: row
            for row in db.scalars(
                select(FieldInventoryWarehouse)
                .where(FieldInventoryWarehouse.source_system == SOURCE_SYSTEM)
                .with_for_update()
            ).all()
        }
        if (
            command.complete_scan
            and existing_items
            and len(command.items) < max(1, len(existing_items) // 2)
        ):
            raise MaterialCatalogError(
                code="operations.material_catalog.suspicious_shrink",
                message="ERP catalogue scan is too small to deactivate missing items safely.",
                details={
                    "existing": len(existing_items),
                    "observed": len(command.items),
                },
            )
        created = updated = deactivated = 0
        seen_items: set[str] = set()
        for item in command.items:
            seen_items.add(item.source_item_id)
            row = existing_items.get(item.source_item_id)
            payload_hash = _digest(item)
            if row is None:
                row = FieldInventoryItem(
                    source_system=SOURCE_SYSTEM,
                    source_item_id=item.source_item_id,
                    sku=item.sku.strip(),
                    name=item.name.strip(),
                    field_request_eligible=False,
                )
                db.add(row)
                created += 1
            else:
                updated += 1
            row.sku = item.sku.strip()
            row.name = item.name.strip()
            row.description = item.description
            row.unit = item.unit
            row.category_code = item.category_code
            row.category_name = item.category_name
            row.source_is_active = item.source_is_active
            row.is_active = item.source_is_active
            row.track_serial_numbers = item.track_serial_numbers
            row.source_updated_at = item.source_updated_at
            row.last_synced_at = command.observed_at
            row.source_payload_hash = payload_hash
        if command.complete_scan:
            for source_id, row in existing_items.items():
                if source_id not in seen_items and row.source_is_active:
                    row.source_is_active = False
                    row.is_active = False
                    row.last_synced_at = command.observed_at
                    deactivated += 1

        wh_created = wh_updated = wh_deactivated = 0
        seen_warehouses: set[str] = set()
        for warehouse in command.warehouses:
            seen_warehouses.add(warehouse.source_warehouse_id)
            warehouse_row = existing_warehouses.get(warehouse.source_warehouse_id)
            if warehouse_row is None:
                warehouse_row = FieldInventoryWarehouse(
                    source_system=SOURCE_SYSTEM,
                    source_warehouse_id=warehouse.source_warehouse_id,
                    code=warehouse.code.strip(),
                    name=warehouse.name.strip(),
                    last_synced_at=command.observed_at,
                )
                db.add(warehouse_row)
                wh_created += 1
            else:
                wh_updated += 1
            warehouse_row.code = warehouse.code.strip()
            warehouse_row.name = warehouse.name.strip()
            warehouse_row.source_is_active = warehouse.source_is_active
            warehouse_row.is_active = warehouse.source_is_active
            warehouse_row.last_synced_at = command.observed_at
            warehouse_row.source_payload_hash = _digest(warehouse)
        if command.complete_scan:
            for source_id, warehouse_row in existing_warehouses.items():
                if source_id not in seen_warehouses and warehouse_row.source_is_active:
                    warehouse_row.source_is_active = False
                    warehouse_row.is_active = False
                    warehouse_row.last_synced_at = command.observed_at
                    wh_deactivated += 1
        db.flush()
        return MaterialCatalogProjectionOutcome(
            created, updated, deactivated, wh_created, wh_updated, wh_deactivated
        )

    return execute_owner_command(
        db, definition=_PROJECT, context=command.context, operation=operation
    )


def set_material_eligibility(
    db: Session, command: SetMaterialEligibility
) -> FieldInventoryItem:
    def operation() -> FieldInventoryItem:
        row = db.scalar(
            select(FieldInventoryItem)
            .where(FieldInventoryItem.id == command.item_id)
            .with_for_update()
        )
        if row is None or row.source_system != SOURCE_SYSTEM:
            raise MaterialCatalogError(
                code="operations.material_catalog.item_not_found",
                message="ERP material catalogue item was not found.",
            )
        if command.eligible and not row.source_is_active:
            raise MaterialCatalogError(
                code="operations.material_catalog.inactive_item",
                message="An inactive ERP item cannot be enabled for field requests.",
            )
        row.field_request_eligible = command.eligible
        metadata = dict(row.metadata_ or {})
        metadata["eligibility"] = {
            "reason": command.reason.strip(),
            "actor": command.context.actor,
            "changed_at": datetime.now(UTC).isoformat(),
        }
        row.metadata_ = metadata
        db.flush()
        return row

    return execute_owner_command(
        db, definition=_ELIGIBILITY, context=command.context, operation=operation
    )


def list_material_catalog(
    db: Session, *, limit: int = 200
) -> tuple[MaterialCatalogItemView, ...]:
    rows = db.scalars(
        select(FieldInventoryItem)
        .where(FieldInventoryItem.source_system == SOURCE_SYSTEM)
        .order_by(
            FieldInventoryItem.field_request_eligible.desc(),
            FieldInventoryItem.name.asc(),
        )
        .limit(max(1, min(limit, 500)))
    ).all()
    return tuple(
        MaterialCatalogItemView(
            id=row.id,
            sku=row.sku or "",
            name=row.name,
            category_name=row.category_name,
            unit=row.unit,
            source_is_active=row.source_is_active,
            field_request_eligible=row.field_request_eligible,
            track_serial_numbers=row.track_serial_numbers,
            last_synced_at=row.last_synced_at,
        )
        for row in rows
    )


def list_material_warehouses(db: Session) -> tuple[MaterialWarehouseView, ...]:
    rows = db.scalars(
        select(FieldInventoryWarehouse)
        .where(FieldInventoryWarehouse.source_system == SOURCE_SYSTEM)
        .order_by(FieldInventoryWarehouse.name.asc())
    ).all()
    return tuple(
        MaterialWarehouseView(
            code=row.code,
            name=row.name,
            source_is_active=row.source_is_active,
            last_synced_at=row.last_synced_at,
        )
        for row in rows
    )


def search_eligible_material_items(
    db: Session, query: SearchEligibleMaterialItems
) -> tuple[EligibleMaterialItemMatch, ...]:
    """Search the active Sub-approved ERP item projection for form selection."""

    term = query.query.strip()
    if len(term) < 2:
        return ()
    limit = max(1, min(query.limit, 50))
    pattern = f"%{term}%"
    rows = db.scalars(
        select(FieldInventoryItem)
        .where(
            FieldInventoryItem.source_system == SOURCE_SYSTEM,
            FieldInventoryItem.is_active.is_(True),
            FieldInventoryItem.source_is_active.is_(True),
            FieldInventoryItem.field_request_eligible.is_(True),
            or_(
                FieldInventoryItem.name.ilike(pattern),
                FieldInventoryItem.sku.ilike(pattern),
                FieldInventoryItem.category_name.ilike(pattern),
            ),
        )
        .order_by(FieldInventoryItem.name.asc(), FieldInventoryItem.id.asc())
        .limit(limit)
    ).all()
    return tuple(
        EligibleMaterialItemMatch(
            id=row.id,
            label=f"{row.name} ({row.sku})" if row.sku else row.name,
            sku=row.sku,
            name=row.name,
            category_name=row.category_name,
            unit=row.unit,
        )
        for row in rows
    )
