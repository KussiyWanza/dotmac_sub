from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.network import OntAssignment, OntUnit
from app.services.genieacs_client import normalize_tr069_serial


def onts_by_normalized_serial(
    db: Session,
    normalized_serials: Iterable[str],
) -> dict[str, OntUnit]:
    serials = {str(value or "").strip() for value in normalized_serials if value}
    if not serials:
        return {}
    onts = list(
        db.scalars(
            select(OntUnit).options(
                joinedload(OntUnit.olt_device),
                joinedload(OntUnit.assignments).joinedload(OntAssignment.pon_port),
            )
        )
        .unique()
        .all()
    )
    return {
        serial: ont
        for ont in onts
        for serial in [normalize_tr069_serial(ont.serial_number or "")]
        if serial in serials
    }
