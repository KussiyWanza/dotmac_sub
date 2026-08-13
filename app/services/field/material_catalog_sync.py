"""ERP transport adapter for the typed field-material catalogue projection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.services.db_session_adapter import db_session_adapter
from app.services.field.material_catalog import (
    ApplyMaterialCatalogProjection,
    ErpMaterialItemObservation,
    ErpWarehouseObservation,
    apply_material_catalog_projection,
)
from app.services.integrations.erp_capability import capability_client
from app.services.owner_commands import CommandContext


def _text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _item(row: dict[str, Any]) -> ErpMaterialItemObservation:
    source_id = _text(row, "item_id", "id")
    sku = _text(row, "item_code", "sku")
    name = _text(row, "item_name", "name")
    if not source_id or not sku or not name:
        raise ValueError(
            "ERP material item is missing item_id, item_code, or item_name"
        )
    return ErpMaterialItemObservation(
        source_item_id=source_id,
        sku=sku,
        name=name,
        description=_text(row, "description"),
        unit=_text(row, "base_uom", "stock_uom", "uom"),
        category_code=_text(row, "category_code", "item_group"),
        category_name=_text(row, "category_name", "item_group"),
        source_is_active=bool(row.get("is_active", True)),
        track_serial_numbers=bool(row.get("track_serial_numbers", False)),
        source_updated_at=_date(row.get("updated_at")),
    )


def _warehouse(row: dict[str, Any]) -> ErpWarehouseObservation:
    source_id = _text(row, "warehouse_id", "id", "code", "warehouse_code")
    code = _text(row, "code", "warehouse_code", "warehouse_id")
    name = _text(row, "name", "warehouse_name", "code")
    if not source_id or not code or not name:
        raise ValueError("ERP warehouse is missing identity, code, or name")
    return ErpWarehouseObservation(
        source_id, code, name, bool(row.get("is_active", True))
    )


def run_erp_material_catalog_sync() -> dict[str, int]:
    observed_at = datetime.now(UTC)
    item_rows: list[dict[str, Any]] = []
    warehouses: list[dict[str, Any]] = []
    with db_session_adapter.session() as read_db:
        with capability_client(read_db) as client:
            offset = 0
            while True:
                page = client.list_inventory(
                    include_zero_stock=True, limit=500, offset=offset
                )
                rows = list(page.get("items") or [])
                item_rows.extend(dict(row) for row in rows if isinstance(row, dict))
                if not page.get("has_more") and len(rows) < 500:
                    break
                offset += len(rows)
                if not rows:
                    break
            warehouses = [dict(row) for row in client.list_inventory_warehouses()]
    command_id = uuid4()
    context = CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="scheduler:erp-material-catalog",
        scope="integration:erp_inventory:read",
        reason="Project ERP item and warehouse catalogues into Sub",
        idempotency_key=f"erp-material-catalog:{observed_at.isoformat()}",
    )
    with db_session_adapter.owner_command_session() as write_db:
        outcome = apply_material_catalog_projection(
            write_db,
            ApplyMaterialCatalogProjection(
                context=context,
                observed_at=observed_at,
                items=tuple(_item(row) for row in item_rows),
                warehouses=tuple(_warehouse(row) for row in warehouses),
                complete_scan=True,
            ),
        )
    return {
        "items_created": outcome.items_created,
        "items_updated": outcome.items_updated,
        "items_deactivated": outcome.items_deactivated,
        "warehouses_created": outcome.warehouses_created,
        "warehouses_updated": outcome.warehouses_updated,
        "warehouses_deactivated": outcome.warehouses_deactivated,
    }
