"""Shared GenieACS device resolution for ONT and CPE services."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import CPEDevice, OLTDevice, OntUnit
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import settings_spec
from app.services.genieacs_client import (
    GenieACSClient,
    GenieACSError,
    create_genieacs_client,
    normalize_tr069_serial,
)
from app.services.network.serial_utils import search_candidates

logger = logging.getLogger(__name__)


def _normalized_serial_expr(column):
    """Build a SQL expression that strips common serial formatting."""
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


def _serial_search_candidates(serial_number: str | None) -> list[str]:
    """Build likely serial variants for GenieACS/TR-069 lookup.

    Supports raw inventory serials like ``HWTC7D4701C3`` and Huawei hex-style
    serials like ``485754437D4701C3`` that GenieACS may report instead.
    """
    return search_candidates(serial_number)


def _normalized_serial_candidates(serial_number: str | None) -> list[str]:
    values = [
        normalize_tr069_serial(value) for value in search_candidates(serial_number)
    ]
    return [value for value in dict.fromkeys(values) if value]


def _resolve_unlinked_tr069_match(
    db: Session,
    ont: OntUnit,
    server: Tr069AcsServer | None,
    *,
    require_unlinked: bool = True,
) -> Tr069CpeDevice | None:
    """Find and link an observed TR-069 row that already matches this ONT."""
    serial_candidates = _normalized_serial_candidates(
        getattr(ont, "serial_number", None)
    )
    if not serial_candidates:
        return None

    conditions = [
        _normalized_serial_expr(Tr069CpeDevice.serial_number).in_(serial_candidates)
    ]
    for candidate in serial_candidates:
        conditions.append(Tr069CpeDevice.genieacs_device_id.ilike(f"%-{candidate}"))

    stmt = (
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.genieacs_device_id.is_not(None))
        .where(
            (Tr069CpeDevice.acs_server_id == server.id)
            if server is not None
            else Tr069CpeDevice.acs_server_id.is_not(None)
        )
        .where(or_(*conditions))
        .limit(1)
    )
    if require_unlinked:
        stmt = stmt.where(Tr069CpeDevice.ont_unit_id.is_(None))
    device = db.scalars(stmt).first()
    if device is None:
        return None

    from app.services.tr069 import link_tr069_device_to_ont

    link_tr069_device_to_ont(
        db,
        device,
        ont,
        acs_server_id=server.id if server is not None else device.acs_server_id,
    )
    logger.info(
        "Linked ONT %s to existing TR-069 device %s while resolving GenieACS identity",
        ont.id,
        device.id,
    )
    return device


def _is_genieacs_device_not_found(exc: Exception, device_id: str | None = None) -> bool:
    text = str(exc)
    if "Device not found:" not in text:
        return False
    return not device_id or str(device_id) in text


def _verified_genieacs_device_id(
    client: GenieACSClient,
    device_id: str | None,
) -> str | None:
    """Return the live GenieACS device id only if GenieACS still has it."""
    clean_id = str(device_id or "").strip()
    if not clean_id:
        return None
    device = client.get_device(clean_id)
    if not isinstance(device, dict):
        return clean_id
    live_id = str(device.get("_id") or "").strip()
    return live_id or clean_id


def _genieacs_device_id_candidates_from_identity(
    device: Tr069CpeDevice | None,
) -> list[str]:
    """Build exact GenieACS IDs from the locally recorded ACS identity parts."""
    if device is None:
        return []
    oui = str(getattr(device, "oui", "") or "").strip()
    product_class = str(getattr(device, "product_class", "") or "").strip()
    serial_number = str(getattr(device, "serial_number", "") or "").strip()
    if not oui or not product_class or not serial_number:
        return []
    normalized_serials = _normalized_serial_candidates(serial_number)
    serials = [serial_number, *normalized_serials]
    candidates: list[str] = []
    for serial in serials:
        candidate = f"{oui}-{product_class}-{serial}"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _resolve_linked_genieacs_identity_candidate(
    client: GenieACSClient,
    linked: Tr069CpeDevice,
) -> str | None:
    for candidate in _genieacs_device_id_candidates_from_identity(linked):
        try:
            live_device_id = _verified_genieacs_device_id(client, candidate)
        except GenieACSError:
            logger.debug(
                "GenieACS exact identity candidate did not resolve for "
                "TR-069 device %s: %s",
                linked.id,
                candidate,
            )
            continue
        if live_device_id:
            return live_device_id
    return None


def clear_stale_genieacs_device_id(
    db: Session,
    ont: OntUnit,
    stale_device_id: str,
) -> bool:
    """Clear a stale linked GenieACS ID so serial search can rediscover it."""
    clean_id = str(stale_device_id or "").strip()
    if not clean_id:
        return False
    stmt = (
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.ont_unit_id == ont.id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.genieacs_device_id == clean_id)
        .limit(1)
    )
    device = db.scalars(stmt).first()
    if device is None:
        from app.metrics import GENIEACS_IDENTITY_RECOVERY_EVENTS

        GENIEACS_IDENTITY_RECOVERY_EVENTS.labels(
            event="clear_stale_id",
            result="not_found",
        ).inc()
        return False
    device.genieacs_device_id = None
    device.connection_request_url = None
    try:
        db.flush()
    except Exception:
        db.rollback()
        from app.metrics import GENIEACS_IDENTITY_RECOVERY_EVENTS

        GENIEACS_IDENTITY_RECOVERY_EVENTS.labels(
            event="clear_stale_id",
            result="error",
        ).inc()
        logger.debug(
            "Failed to clear stale GenieACS device id %s for ONT %s",
            clean_id,
            ont.id,
            exc_info=True,
        )
        return False
    from app.metrics import GENIEACS_IDENTITY_RECOVERY_EVENTS

    GENIEACS_IDENTITY_RECOVERY_EVENTS.labels(
        event="clear_stale_id",
        result="success",
    ).inc()
    logger.warning(
        "Cleared stale GenieACS device id %s for ONT %s; serial rediscovery required",
        clean_id,
        ont.id,
    )
    return True


def _resolve_olt_via_assignment(db: Session, ont: OntUnit) -> OLTDevice | None:
    """Fall back to active assignment → PON port → OLT resolution."""
    from app.models.network import PonPort

    for assignment in getattr(ont, "assignments", []):
        if not assignment.active or not assignment.pon_port_id:
            continue
        pon_port = db.get(PonPort, str(assignment.pon_port_id))
        if pon_port and pon_port.olt_id:
            olt = db.get(OLTDevice, str(pon_port.olt_id))
            if olt:
                return olt
    return None


def _resolve_device_id_from_server(
    client: GenieACSClient, serial_number: str
) -> str | None:
    for candidate in _serial_search_candidates(serial_number):
        escaped_candidate = re.escape(candidate)
        devices = client.list_devices(
            query={
                "$or": [
                    {"_id": {"$regex": f".*-{escaped_candidate}$"}},
                    {"_deviceId._SerialNumber": candidate},
                    {"_deviceId.SerialNumber": candidate},
                    {"Device.DeviceInfo.SerialNumber._value": candidate},
                    {
                        "InternetGatewayDevice.DeviceInfo.SerialNumber._value": (
                            candidate
                        )
                    },
                ]
            }
        )
        if not devices:
            continue
        device_id = str(devices[0].get("_id") or "").strip()
        if device_id:
            return device_id
    return None


def _parse_genieacs_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def reconcile_ont_tr069_device(
    db: Session,
    ont: OntUnit,
) -> tuple[Tr069CpeDevice | None, str]:
    """Targeted post-authorization TR-069 reconciliation for one ONT.

    Full GenieACS sync can be slow and is scheduled independently. Authorization
    needs a narrow, deterministic pass that links the just-authorized ONT to an
    already-known or newly-observed ACS row by serial variants.
    """
    if not getattr(ont, "serial_number", None):
        return None, "ONT serial number is missing."

    from app.services.network.acs_resolution import resolve_acs_for_ont
    from app.services.tr069 import CpeDevices, link_tr069_device_to_ont

    acs_resolution = resolve_acs_for_ont(db, ont)
    server = acs_resolution.server
    if server is None:
        return None, "No ACS server configured for ONT."

    matched = _resolve_unlinked_tr069_match(
        db,
        ont,
        server,
        require_unlinked=False,
    )
    if matched is not None:
        return matched, "linked_existing_local_tr069_device"

    client = create_genieacs_client(server.base_url)
    serial = str(getattr(ont, "serial_number", "") or "").strip()
    try:
        device_id = _resolve_device_id_from_server(client, serial)
        if not device_id:
            return (
                None,
                f"No TR-069 device found in GenieACS for ONT serial '{serial}'.",
            )
        device_data = client.get_device(device_id)
    except GenieACSError as exc:
        return None, f"GenieACS lookup failed: {exc}"

    genieacs_device_id = str(device_data.get("_id") or device_id or "").strip()
    if not genieacs_device_id:
        return None, "GenieACS returned a device without an ID."

    oui, product_class, serial_number = CpeDevices._extract_identity(
        client, device_data
    )
    serial_number = serial_number or serial
    connection_url = client.extract_parameter_value(
        device_data, "Device.ManagementServer.ConnectionRequestURL"
    ) or client.extract_parameter_value(
        device_data,
        "InternetGatewayDevice.ManagementServer.ConnectionRequestURL",
    )
    connection_url = CpeDevices._clip_text(connection_url, 255)
    last_inform_at = _parse_genieacs_timestamp(device_data.get("_lastInform"))

    normalized_serial = normalize_tr069_serial(serial_number)
    existing = (
        db.query(Tr069CpeDevice)
        .filter(Tr069CpeDevice.acs_server_id == server.id)
        .filter(Tr069CpeDevice.genieacs_device_id == genieacs_device_id)
        .first()
    )
    if existing is None and serial_number:
        existing = (
            db.query(Tr069CpeDevice)
            .filter(Tr069CpeDevice.acs_server_id == server.id)
            .filter(Tr069CpeDevice.serial_number == serial_number)
            .first()
        )
    if existing is None and normalized_serial:
        existing = (
            db.query(Tr069CpeDevice)
            .filter(Tr069CpeDevice.acs_server_id == server.id)
            .filter(
                _normalized_serial_expr(Tr069CpeDevice.serial_number)
                == normalized_serial
            )
            .first()
        )
    if existing is None and connection_url:
        existing = (
            db.query(Tr069CpeDevice)
            .filter(Tr069CpeDevice.acs_server_id == server.id)
            .filter(Tr069CpeDevice.connection_request_url == connection_url)
            .first()
        )

    if existing is None:
        existing = Tr069CpeDevice(
            acs_server_id=server.id,
            genieacs_device_id=genieacs_device_id,
            serial_number=serial_number[:120] if serial_number else None,
            oui=oui[:8] if oui else None,
            product_class=product_class[:120] if product_class else None,
            connection_request_url=connection_url,
            last_inform_at=last_inform_at,
            is_active=True,
        )
        db.add(existing)
        reason = "created_and_linked_tr069_device"
    else:
        existing.genieacs_device_id = genieacs_device_id
        if serial_number:
            existing.serial_number = serial_number[:120]
        existing.oui = oui[:8] if oui else existing.oui
        existing.product_class = (
            product_class[:120] if product_class else existing.product_class
        )
        existing.connection_request_url = connection_url
        existing.last_inform_at = last_inform_at
        existing.is_active = True
        reason = "updated_and_linked_tr069_device"

    link_tr069_device_to_ont(db, existing, ont, acs_server_id=server.id)
    logger.info(
        "Post-authorization ACS reconciliation linked ONT %s to TR-069 device %s (%s)",
        ont.id,
        existing.id,
        reason,
    )
    return existing, reason


def _resolve_server_by_id(
    db: Session,
    server_id: str | None,
) -> Tr069AcsServer | None:
    if not server_id:
        return None
    server = db.get(Tr069AcsServer, str(server_id))
    if not server or not server.base_url:
        return None
    return server


def _cache_genieacs_device_id(
    db: Session,
    device: Tr069CpeDevice | None,
    device_id: str | None,
) -> None:
    if not device or not device_id:
        return
    clean_id = str(device_id).strip()
    if not clean_id or device.genieacs_device_id == clean_id:
        return

    owner = db.scalars(
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.genieacs_device_id == clean_id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.id != device.id)
        .limit(1)
    ).first()
    if owner:
        if device.ont_unit_id and not owner.ont_unit_id:
            owner_id = owner.id
            placeholder_id = device.id
            ont_unit_id = device.ont_unit_id
            cpe_device_id = device.cpe_device_id
            device.ont_unit_id = None
            device.is_active = False
            try:
                db.flush()
                owner.ont_unit_id = ont_unit_id
                if owner.cpe_device_id is None and cpe_device_id is not None:
                    owner.cpe_device_id = cpe_device_id
                db.flush()
            except Exception:
                db.rollback()
                logger.debug(
                    "Failed to move ONT link from TR-069 placeholder %s to "
                    "GenieACS device %s",
                    placeholder_id,
                    owner_id,
                    exc_info=True,
                )
            return
        logger.info(
            "Skipping GenieACS device id cache for TR-069 device %s because %s "
            "is already assigned to active TR-069 device %s",
            device.id,
            clean_id,
            owner.id,
        )
        return

    device.genieacs_device_id = clean_id[:255]
    try:
        db.flush()
    except Exception:
        db.rollback()
        logger.debug(
            "Failed to cache GenieACS device id %s on TR-069 device %s",
            clean_id,
            device.id,
            exc_info=True,
        )
        from app.metrics import GENIEACS_IDENTITY_RECOVERY_EVENTS

        GENIEACS_IDENTITY_RECOVERY_EVENTS.labels(
            event="cache_device_id",
            result="error",
        ).inc()
        return
    from app.metrics import GENIEACS_IDENTITY_RECOVERY_EVENTS

    GENIEACS_IDENTITY_RECOVERY_EVENTS.labels(
        event="cache_device_id",
        result="success",
    ).inc()


def resolve_genieacs(db: Session, ont: OntUnit) -> tuple[GenieACSClient, str] | None:
    resolved, _reason = resolve_genieacs_with_reason(db, ont)
    return resolved


def resolve_genieacs_with_reason(
    db: Session, ont: OntUnit
) -> tuple[tuple[GenieACSClient, str] | None, str]:
    """Resolve GenieACS client and device ID for an ONT.

    Resolution priority:
    1. Linked TR-069 device with genieacs_device_id (authoritative)
    2. Linked TR-069 ACS identity parts (OUI, product class, ACS serial)
    3. Search GenieACS via OLT's ACS server
    4. Search GenieACS via default ACS server

    Returns:
        Tuple of (client, device_id) or None if not resolvable.
    """
    if not getattr(ont, "serial_number", None):
        return None, "ONT serial number is missing."

    from app.services.network.acs_resolution import resolve_acs_for_ont

    acs_resolution = resolve_acs_for_ont(db, ont)

    # 1) Primary: Linked TR-069 device with genieacs_device_id
    linked_stmt = (
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.ont_unit_id == ont.id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .limit(1)
    )
    linked = db.scalars(linked_stmt).first()
    if linked and linked.acs_server_id:
        server = _resolve_server_by_id(db, str(linked.acs_server_id))
        if server:
            desired_server = acs_resolution.server
            if desired_server is None or linked.acs_server_id == desired_server.id:
                client = create_genieacs_client(server.base_url)
                live_device_id = None
                if linked.genieacs_device_id:
                    linked_device_id = str(linked.genieacs_device_id)
                    try:
                        live_device_id = _verified_genieacs_device_id(
                            client,
                            linked_device_id,
                        )
                    except GenieACSError as exc:
                        if _is_genieacs_device_not_found(exc, linked_device_id):
                            clear_stale_genieacs_device_id(db, ont, linked_device_id)
                        else:
                            logger.debug(
                                "GenieACS verification failed for linked ONT %s device %s",
                                ont.id,
                                linked_device_id,
                                exc_info=True,
                            )
                        live_device_id = None
                else:
                    live_device_id = _resolve_linked_genieacs_identity_candidate(
                        client,
                        linked,
                    )
                if live_device_id:
                    _cache_genieacs_device_id(db, linked, live_device_id)
                    return (
                        client,
                        live_device_id,
                    ), "resolved_via_linked_tr069_device"
            if desired_server is not None and linked.acs_server_id != desired_server.id:
                logger.info(
                    "Ignoring stale linked TR-069 device for ONT %s: linked ACS %s, desired ACS %s",
                    ont.id,
                    linked.acs_server_id,
                    desired_server.id,
                )
    if linked and linked.acs_server_id:
        logger.debug(
            "Linked TR-069 placeholder for ONT %s has no GenieACS device id yet",
            ont.id,
        )

    servers_to_try = []
    if acs_resolution.server is not None:
        reason = {
            "ont_override": "resolved_via_ont_acs",
            "olt_default": "resolved_via_olt_acs",
            "system_default": "resolved_via_default_acs",
        }.get(acs_resolution.source, f"resolved_via_{acs_resolution.source}")
        servers_to_try.append((acs_resolution.server, reason))

    serial = str(getattr(ont, "serial_number", "") or "").strip()
    for server, _reason in servers_to_try:
        matched = _resolve_unlinked_tr069_match(
            db,
            ont,
            server,
            require_unlinked=False,
        )
        if matched and matched.genieacs_device_id:
            client = create_genieacs_client(server.base_url)
            matched_device_id = str(matched.genieacs_device_id)
            try:
                live_device_id = _verified_genieacs_device_id(
                    client,
                    matched_device_id,
                )
            except GenieACSError as exc:
                if _is_genieacs_device_not_found(exc, matched_device_id):
                    clear_stale_genieacs_device_id(db, ont, matched_device_id)
                else:
                    logger.debug(
                        "GenieACS verification failed for serial-matched ONT %s device %s",
                        ont.id,
                        matched_device_id,
                        exc_info=True,
                    )
                live_device_id = None
            if live_device_id:
                _cache_genieacs_device_id(db, matched, live_device_id)
                return (
                    client,
                    live_device_id,
                ), "resolved_via_unlinked_tr069_serial_match"

    for server, reason in servers_to_try:
        client = create_genieacs_client(server.base_url)
        try:
            found_device_id = (
                _resolve_device_id_from_server(client, serial) if serial else None
            )
            if found_device_id:
                _cache_genieacs_device_id(db, linked, found_device_id)
                return (client, found_device_id), reason
        except GenieACSError:
            logger.debug(
                "GenieACS search failed for ONT %s on %s",
                ont.serial_number,
                server.name,
            )

    if not servers_to_try:
        return (
            None,
            "No ACS server configured. Set TR-069 ACS on OLT or configure default ACS server.",
        )
    return None, f"No TR-069 device found in GenieACS for ONT serial '{serial}'."


def resolve_genieacs_for_cpe(
    db: Session, cpe: CPEDevice
) -> tuple[GenieACSClient, str] | None:
    """Resolve GenieACS client and device ID for a CPE device."""
    resolved, _reason = resolve_genieacs_for_cpe_with_reason(db, cpe)
    return resolved


def resolve_genieacs_for_cpe_with_reason(
    db: Session, cpe: CPEDevice
) -> tuple[tuple[GenieACSClient, str] | None, str]:
    """Resolve GenieACS client and device ID for a CPE device.

    Resolution tiers (simpler than ONT — no OLT hierarchy):
    1. Linked Tr069CpeDevice by cpe_device_id FK
    2. Linked Tr069CpeDevice by normalized serial number match
    3. Default ACS server from settings

    Returns:
        Tuple of (client, device_id) or None if not resolvable.
    """
    serial = str(getattr(cpe, "serial_number", None) or "").strip()
    if not serial:
        return None, "CPE serial number is missing."

    cpe_id = str(cpe.id) if cpe.id else ""

    # 1) Linked Tr069CpeDevice by cpe_device_id FK
    if cpe_id:
        stmt = (
            select(Tr069CpeDevice)
            .where(Tr069CpeDevice.cpe_device_id == cpe.id)
            .where(Tr069CpeDevice.is_active.is_(True))
            .limit(1)
        )
        linked = db.scalars(stmt).first()
        if linked and linked.acs_server_id:
            server = _resolve_server_by_id(db, str(linked.acs_server_id))
            if server:
                client = create_genieacs_client(server.base_url)
                if linked.genieacs_device_id:
                    return (
                        client,
                        str(linked.genieacs_device_id),
                    ), "resolved_via_cpe_device_fk"
                try:
                    device_id = _resolve_device_id_from_server(client, serial)
                except GenieACSError:
                    device_id = None
                if device_id:
                    _cache_genieacs_device_id(db, linked, device_id)
                    return (client, device_id), "resolved_via_cpe_device_fk"

    # 2) Linked Tr069CpeDevice by normalized serial number match
    normalized_candidates = [
        normalize_tr069_serial(value) for value in _serial_search_candidates(serial)
    ]
    normalized_candidates = [value for value in normalized_candidates if value]
    if normalized_candidates:
        stmt = (
            select(Tr069CpeDevice)
            .where(
                _normalized_serial_expr(Tr069CpeDevice.serial_number).in_(
                    normalized_candidates
                )
            )
            .where(Tr069CpeDevice.is_active.is_(True))
            .limit(1)
        )
        cpe_tr069 = db.scalars(stmt).first()

        if cpe_tr069 and cpe_tr069.acs_server_id:
            server = _resolve_server_by_id(db, str(cpe_tr069.acs_server_id))
            if server:
                client = create_genieacs_client(server.base_url)
                if cpe_tr069.genieacs_device_id:
                    return (
                        client,
                        str(cpe_tr069.genieacs_device_id),
                    ), "resolved_via_tr069_serial_match"
                try:
                    device_id = _resolve_device_id_from_server(client, serial)
                except GenieACSError:
                    device_id = None
                if device_id:
                    _cache_genieacs_device_id(db, cpe_tr069, device_id)
                    return (client, device_id), "resolved_via_tr069_serial_match"

    # 3) Default ACS server from settings
    default_server_id = settings_spec.resolve_value(
        db,
        SettingDomain.tr069,
        "default_acs_server_id",
    )
    if default_server_id:
        server = _resolve_server_by_id(db, str(default_server_id))
        if server:
            client = create_genieacs_client(server.base_url)
            try:
                device_id = _resolve_device_id_from_server(client, serial)
                if device_id:
                    return (client, device_id), "resolved_via_default_acs"
            except GenieACSError:
                logger.warning("Failed to search GenieACS for CPE %s", serial)

    if not default_server_id:
        return (
            None,
            "No ACS server configured on linked TR-069 device or default settings.",
        )
    return None, f"No matching GenieACS device found for CPE serial '{serial}'."
