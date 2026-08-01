"""OLT autofind inventory helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import (
    GponChannel,
    OLTDevice,
    OntAuthorizationStatus,
    OntUnit,
    OnuOnlineStatus,
    PonType,
)
from app.models.ont_autofind import OltAutofindCandidate
from app.services import tr069 as tr069_service
from app.services.network._common import (
    is_identifying_mac_address,
    normalize_mac_address,
)
from app.services.network.ont_assignment_alignment import (
    project_ont_topology_from_fsp_observation,
)
from app.services.network.ont_status import set_authorization_status
from app.services.web_network_ont_autofind import _find_ont_by_serial


def parse_fsp_parts(fsp: str) -> tuple[str | None, str | None]:
    """Split an F/S/P string into board and port fragments."""
    parts = [part.strip() for part in str(fsp or "").split("/") if part.strip()]
    if len(parts) != 3:
        return None, None
    return f"{parts[0]}/{parts[1]}", parts[2]


def persist_authorized_ont_inventory(
    db: Session,
    *,
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
    ont_id: int,
) -> None:
    """Persist observed ONT inventory without inferring customer assignment."""
    normalized_serial = str(serial_number or "").strip()
    board, port = parse_fsp_parts(fsp)
    if not normalized_serial or not board or not port:
        return

    now = datetime.now(UTC)
    candidate = db.scalars(
        select(OltAutofindCandidate)
        .where(
            OltAutofindCandidate.olt_id == olt.id,
            OltAutofindCandidate.fsp == fsp,
            OltAutofindCandidate.serial_number == normalized_serial,
        )
        .order_by(
            OltAutofindCandidate.updated_at.desc(),
            OltAutofindCandidate.created_at.desc(),
        )
    ).first()

    ont = _find_ont_by_serial(db, normalized_serial)
    created_ont = ont is None
    if created_ont:
        ont = OntUnit(
            serial_number=normalized_serial,
            is_active=True,
        )
        db.add(ont)
        db.flush()
    else:
        # Lock existing ONT before modification to prevent concurrent updates
        ont = db.scalars(
            select(OntUnit).where(OntUnit.id == ont.id).with_for_update()
        ).first()
        if ont is None:
            return  # ONT was deleted concurrently

    ont.is_active = True
    ont.pon_type = PonType.gpon
    ont.gpon_channel = GponChannel.gpon
    ont.external_id = str(ont_id)
    ont.olt_status = OnuOnlineStatus.offline
    set_authorization_status(ont, OntAuthorizationStatus.authorized, strict=False)
    acs_server_id = tr069_service.resolve_acs_server_for_ont(
        db, ont=ont, olt_id=str(olt.id)
    )
    ont.last_sync_source = "olt_ssh_authorize"
    ont.last_sync_at = now
    tr069_service.sync_ont_acs_server(db, ont, acs_server_id)

    if candidate is not None:
        if candidate.vendor_id:
            ont.vendor = candidate.vendor_id
        if candidate.model:
            ont.model = candidate.model
        if candidate.software_version:
            ont.software_version = candidate.software_version
        # An OLT will happily report a locally administered placeholder for an
        # ONT that has not presented a real address -- 227 units shared one such
        # value -- and because device lookups match on MAC with a limit of one,
        # storing it makes an arbitrary unit answer to it. Leave the column NULL
        # instead: absent identity is recoverable, wrong identity is not.
        if candidate.mac and is_identifying_mac_address(candidate.mac):
            ont.mac_address = normalize_mac_address(candidate.mac)

    project_ont_topology_from_fsp_observation(
        db,
        ont=ont,
        olt_id=olt.id,
        fsp=fsp,
    )

    if candidate is not None:
        candidate.ont_unit = ont
        candidate.is_active = False
        candidate.resolution_reason = "authorized"
        candidate.resolved_at = now

    db.flush()


def pending_candidate_count(db: Session) -> int:
    """Count active autofind candidates awaiting configuration.

    Canonical owner of the overview's "unconfigured ONTs" figure.
    """
    return int(
        db.scalar(
            select(func.count(OltAutofindCandidate.id)).where(
                OltAutofindCandidate.is_active.is_(True)
            )
        )
        or 0
    )
