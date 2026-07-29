"""CRUD manager for OLT PON ports."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import (
    OltCard,
    OltCardPort,
    OLTDevice,
    OltPortType,
    OltShelf,
    OntAssignment,
    PonPort,
)
from app.schemas.network import PonPortCreate, PonPortUpdate
from app.services.common import coerce_uuid
from app.services.crud import CRUDManager
from app.services.network._common import _apply_ordering, _apply_pagination
from app.services.network.olt_crud_common import (
    canonical_pon_name_from_card_port,
    parse_canonical_pon_name,
)
from app.services.query_builders import apply_active_state, apply_optional_equals


class PonPorts(CRUDManager[PonPort]):
    model = PonPort
    not_found_detail = "PON port not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: PonPortCreate) -> PonPort:  # type: ignore[override]
        olt = db.get(OLTDevice, payload.olt_id)
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")
        card_port: OltCardPort | None = None
        if payload.olt_card_port_id:
            card_port = db.get(OltCardPort, payload.olt_card_port_id)
            if not card_port:
                raise HTTPException(status_code=404, detail="OLT card port not found")
            card = db.get(OltCard, card_port.card_id)
            shelf = db.get(OltShelf, card.shelf_id) if card else None
            if not shelf or shelf.olt_id != payload.olt_id:
                raise HTTPException(
                    status_code=400,
                    detail="OLT card port does not belong to the selected OLT",
                )
        elif payload.card_id:
            if payload.port_number is None:
                raise HTTPException(
                    status_code=400,
                    detail="port_number is required when card_id is provided",
                )
            card = db.get(OltCard, payload.card_id)
            if not card:
                raise HTTPException(status_code=404, detail="OLT card not found")
            shelf = db.get(OltShelf, card.shelf_id)
            if not shelf or shelf.olt_id != payload.olt_id:
                raise HTTPException(
                    status_code=400,
                    detail="OLT card does not belong to the selected OLT",
                )
            card_port = db.scalars(
                select(OltCardPort)
                .where(OltCardPort.card_id == payload.card_id)
                .where(OltCardPort.port_number == payload.port_number)
                .limit(1)
            ).first()
            if card_port is None:
                card_port = OltCardPort(
                    card_id=payload.card_id,
                    port_number=payload.port_number,
                    port_type=OltPortType.pon,
                    is_active=True,
                )
                db.add(card_port)
                db.flush()
        data = payload.model_dump()
        if not card_port:
            parsed_name = parse_canonical_pon_name(data.get("name"))
            if parsed_name is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Canonical frame/slot/port name is required when no OLT card "
                        "or card port is linked"
                    ),
                )
            _, parsed_port_number = parsed_name
            data["name"] = str(data["name"]).strip()
            data["port_number"] = parsed_port_number
            existing_port = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == payload.olt_id)
                .where(PonPort.name == data["name"])
                .limit(1)
            ).first()
            if existing_port is not None:
                existing_port.is_active = True
                existing_port.port_number = parsed_port_number
                if data.get("notes") is not None:
                    existing_port.notes = data["notes"]
                if data.get("max_ont_capacity") is not None:
                    existing_port.max_ont_capacity = data["max_ont_capacity"]
                db.commit()
                db.refresh(existing_port)
                return existing_port
        if payload.card_id and not payload.olt_card_port_id and card_port:
            data["olt_card_port_id"] = card_port.id
        if card_port:
            canonical_name = canonical_pon_name_from_card_port(db, card_port)
            card_port.name = canonical_name
            card_port.is_active = True
            data["port_number"] = card_port.port_number
            data["name"] = canonical_name
            existing_port = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == payload.olt_id)
                .where(PonPort.olt_card_port_id == card_port.id)
                .limit(1)
            ).first()
            if existing_port is None:
                existing_port = db.scalars(
                    select(PonPort)
                    .where(PonPort.olt_id == payload.olt_id)
                    .where(PonPort.name == canonical_name)
                    .limit(1)
                ).first()
            if existing_port is not None:
                existing_port.olt_card_port_id = card_port.id
                existing_port.port_number = card_port.port_number
                existing_port.name = canonical_name
                existing_port.is_active = True
                if data.get("notes") is not None:
                    existing_port.notes = data["notes"]
                if data.get("max_ont_capacity") is not None:
                    existing_port.max_ont_capacity = data["max_ont_capacity"]
                db.commit()
                db.refresh(existing_port)
                return existing_port
        port = PonPort(**data)
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @classmethod
    def get(cls, db: Session, port_id: str) -> PonPort:
        return super().get(db, port_id)

    @staticmethod
    def set_admin_enabled(db: Session, port: PonPort, *, enabled: bool) -> PonPort:
        """Persist device-confirmed administrative state without changing lifecycle."""
        port.admin_enabled = enabled
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def list(
        db: Session,
        olt_id: str | None = None,
        is_active: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 20,
        offset: int = 0,
        card_id: str | None = None,
    ) -> list[PonPort]:
        stmt = select(PonPort)
        if card_id:
            stmt = stmt.join(OltCardPort, PonPort.olt_card_port_id == OltCardPort.id)
            stmt = stmt.filter(OltCardPort.card_id == coerce_uuid(card_id))
        stmt = apply_optional_equals(stmt, {PonPort.olt_id: olt_id})
        stmt = apply_active_state(stmt, PonPort.is_active, is_active)
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": PonPort.created_at, "name": PonPort.name},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def update(db: Session, port_id: str, payload: PonPortUpdate) -> PonPort:  # type: ignore[override]
        port = PonPorts.get(db, port_id)
        data = payload.model_dump(exclude_unset=True)
        target_olt_id = data.get("olt_id", port.olt_id)
        if "olt_id" in data:
            olt = db.get(OLTDevice, target_olt_id)
            if not olt:
                raise HTTPException(status_code=404, detail="OLT device not found")
        target_card_port_id = data.get("olt_card_port_id", port.olt_card_port_id)
        if target_card_port_id:
            card_port = db.get(OltCardPort, target_card_port_id)
            if not card_port:
                raise HTTPException(status_code=404, detail="OLT card port not found")
            card = db.get(OltCard, card_port.card_id)
            shelf = db.get(OltShelf, card.shelf_id) if card else None
            if not shelf or shelf.olt_id != target_olt_id:
                raise HTTPException(
                    status_code=400,
                    detail="OLT card port does not belong to the selected OLT",
                )
            canonical_name = canonical_pon_name_from_card_port(db, card_port)
            card_port.name = canonical_name
            card_port.is_active = True
            data["port_number"] = card_port.port_number
            data["name"] = canonical_name
            duplicate_by_name = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == target_olt_id)
                .where(PonPort.name == canonical_name)
                .where(PonPort.id != port.id)
                .limit(1)
            ).first()
            if duplicate_by_name is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A PON port already exists for this OLT and name",
                )
            duplicate = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == target_olt_id)
                .where(PonPort.olt_card_port_id == target_card_port_id)
                .where(PonPort.id != port.id)
                .limit(1)
            ).first()
            if duplicate is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A PON port already exists for this OLT card port",
                )
        else:
            target_name = data.get("name", port.name)
            parsed_name = parse_canonical_pon_name(target_name)
            if parsed_name is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Canonical frame/slot/port name is required when no OLT card "
                        "or card port is linked"
                    ),
                )
            _, parsed_port_number = parsed_name
            data["name"] = str(target_name).strip()
            data["port_number"] = parsed_port_number
            duplicate = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == target_olt_id)
                .where(PonPort.name == data["name"])
                .where(PonPort.id != port.id)
                .limit(1)
            ).first()
            if duplicate is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A PON port already exists for this OLT and name",
                )
        for key, value in data.items():
            setattr(port, key, value)
        db.commit()
        db.refresh(port)
        return port

    @classmethod
    def delete(cls, db: Session, port_id: str) -> None:  # type: ignore[override]
        return super().delete(db, port_id)

    @staticmethod
    def utilization(db: Session, olt_id: str | None) -> dict[str, object]:
        total_stmt = select(func.count(PonPort.id)).where(PonPort.is_active.is_(True))
        if olt_id:
            total_stmt = total_stmt.where(PonPort.olt_id == olt_id)
        total_ports = db.scalar(total_stmt) or 0

        assigned_stmt = select(
            func.count(func.distinct(OntAssignment.pon_port_id))
        ).where(OntAssignment.active.is_(True))
        if olt_id:
            assigned_stmt = assigned_stmt.where(
                OntAssignment.pon_port_id.in_(
                    select(PonPort.id).where(PonPort.olt_id == olt_id)
                )
            )
        assigned_count = db.scalar(assigned_stmt) or 0

        return {
            "olt_id": olt_id,
            "total_ports": total_ports,
            "assigned_ports": assigned_count,
        }
