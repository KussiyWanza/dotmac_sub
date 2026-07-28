"""Native field material allocation and consumption."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.field_material import FieldWorkOrderMaterial
from app.models.work_order import WorkOrder
from app.services.common import coerce_uuid
from app.services.field.jobs import _profile_from_principal, _scoped_query
from app.services.field.source import (
    mark_sub_authoritative as _mark_source_authoritative,
)


def serialize_material(material: FieldWorkOrderMaterial) -> dict:
    item = material.item
    return {
        "id": material.id,
        "crm_work_order_id": material.work_order_mirror.public_id,
        "crm_material_id": material.crm_material_id,
        "item_id": material.item_id,
        "sku": item.sku if item else None,
        "name": item.name if item else None,
        "unit": item.unit if item else None,
        "allocated_quantity": material.allocated_quantity,
        "consumed_quantity": material.consumed_quantity,
        "remaining_quantity": max(
            0, material.allocated_quantity - material.consumed_quantity
        ),
        "status": material.status,
        "notes": material.notes,
        "created_at": material.created_at,
        "updated_at": material.updated_at,
    }


class FieldMaterials:
    @staticmethod
    def list_for_job(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
    ) -> list[dict]:
        row = _scoped_work_order(db, principal, crm_work_order_id)
        materials = (
            db.query(FieldWorkOrderMaterial)
            .options(selectinload(FieldWorkOrderMaterial.item))
            .filter(FieldWorkOrderMaterial.work_order_mirror_id == row.id)
            .filter(FieldWorkOrderMaterial.is_active.is_(True))
            .order_by(FieldWorkOrderMaterial.created_at.asc())
            .all()
        )
        return [serialize_material(material) for material in materials]

    @staticmethod
    def consume(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        items: list[dict[str, Any]],
    ) -> list[dict]:
        row = _scoped_work_order(db, principal, crm_work_order_id)
        if not items:
            raise HTTPException(status_code=422, detail="No materials to consume")

        planned: list[tuple[FieldWorkOrderMaterial, int, str | None]] = []
        seen: set[UUID] = set()
        for item in items:
            material_id = coerce_uuid(item.get("material_id"))
            if material_id in seen:
                raise HTTPException(
                    status_code=422, detail="Duplicate material_id in request"
                )
            seen.add(material_id)
            material = (
                db.query(FieldWorkOrderMaterial)
                .options(selectinload(FieldWorkOrderMaterial.item))
                .filter(FieldWorkOrderMaterial.id == material_id)
                .filter(FieldWorkOrderMaterial.work_order_mirror_id == row.id)
                .filter(FieldWorkOrderMaterial.is_active.is_(True))
                .with_for_update()
                .one_or_none()
            )
            if material is None:
                raise HTTPException(
                    status_code=404, detail="Material not found on this job"
                )
            consumed = _quantity(item.get("consumed_quantity"))
            if consumed > material.allocated_quantity:
                raise HTTPException(
                    status_code=422,
                    detail=f"Cannot consume {consumed} of {material.allocated_quantity} allocated",
                )
            planned.append((material, consumed, item.get("leftover_note")))

        updated: list[FieldWorkOrderMaterial] = []
        for material, consumed, leftover_note in planned:
            if consumed > material.consumed_quantity:
                material.consumed_quantity = consumed
            if leftover_note:
                material.notes = (
                    (material.notes or "") + f"\nLeftover: {leftover_note}"
                ).strip()
            if material.consumed_quantity == material.allocated_quantity:
                material.status = "used"
            elif material.status == "used":
                material.status = "reserved"
            updated.append(material)

        _mark_sub_authoritative(row)
        # Consumption evidence is this owner's committed output, staged
        # atomically with the allocation writes for downstream reconcilers.
        from app.services.events import EventType, emit_event

        emit_event(
            db,
            EventType.field_material_consumption_recorded,
            {
                "work_order_mirror_id": str(row.id),
                "work_order_public_id": row.public_id,
                "items": [
                    {
                        "material_id": str(material.id),
                        "allocated_quantity": material.allocated_quantity,
                        "consumed_quantity": material.consumed_quantity,
                        "status": material.status,
                    }
                    for material in updated
                ],
            },
            actor="operations.material_consumption",
            subscriber_id=row.subscriber_id,
        )
        db.commit()
        for material in updated:
            db.refresh(material)
        return [serialize_material(material) for material in updated]


def _scoped_work_order(
    db: Session,
    principal: dict[str, Any],
    crm_work_order_id: str,
) -> WorkOrder:
    profile = _profile_from_principal(db, principal)
    row = (
        _scoped_query(db, profile)
        .filter(WorkOrder.public_id == crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def _quantity(value) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="consumed_quantity must be an integer"
        ) from exc
    if quantity < 0:
        raise HTTPException(
            status_code=422, detail="consumed_quantity cannot be negative"
        )
    return quantity


def _mark_sub_authoritative(row: WorkOrder) -> None:
    _mark_source_authoritative(row, "materials")


field_materials = FieldMaterials()
